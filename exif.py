"""
exif.py — EXIF date extraction with two interchangeable backends.

Both ``ImageExifReader`` (uses Pillow) and ``ExifToolReader`` (uses the
exiftool subprocess) expose the same interface:

    reader.get_date(path: Path) -> Optional[date]
    reader.get_raw(path: Path) -> dict

``ExifToolReader`` additionally supports batch loading for efficiency.

Design notes
------------
* No global state; all configuration is passed via constructor arguments.
* ``ExifToolReader`` lazily starts the exiftool subprocess and registers an
  ``atexit`` handler to shut it down cleanly — but the instance can also be
  used as a context manager for explicit lifecycle control.
* The ``recommended`` debug set from the original code is moved to an instance
  variable so it doesn't leak between test runs.
"""

from __future__ import annotations

import atexit
import logging
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .dates import parse_date

logger = logging.getLogger(__name__)

# Disable PIL's decompression-bomb check.  This check exists to protect
# against hostile input; for a personal photo library it only produces noise.
try:
    from PIL import Image as _PILImage
    _PILImage.MAX_IMAGE_PIXELS = None  # pragma: no cover
except ImportError:
    pass

# ── Field priority lists ───────────────────────────────────────────────────

# Pillow / IFD tags (human-readable names after TAGS mapping)
# Priority: original capture time first, then digitised time, then GPS.
# "DateTime" (EXIF tag 0x0132) is intentionally omitted — Pillow maps it
# to the last-modified date, not the original capture date.  Including it
# caused files to be dated by their edit/modify date rather than when they
# were shot.
IMAGE_DATE_FIELDS: list[str] = [
    "DateTimeOriginal",
    "GPS Date Stamp",
    "Image Generated",
    "CreateDate",
    "Image Digitized",
    "DateTimeDigitized",
]

IMAGE_DATE_FIELDS_IGNORE: frozenset[str] = frozenset(["Camera Date", "DateTime"])

# ExifTool namespaced tags — ordered strictly from most- to least-authoritative
# capture/creation date.  Modify-date fields are explicitly excluded.
#
# Rationale for omissions:
#   EXIF:DateTime      → ExifTool exposes tag 0x0132 as EXIF:ModifyDate, not
#                        EXIF:DateTime, so that entry was always a dead no-op.
#   EXIF:ModifyDate    → explicitly ignored below; must never be used as a
#                        date source because it reflects the last file edit.
_EXIFTOOL_DATE_FIELDS_BASE: dict[str, list[str]] = {
    "EXIF": [
        "DateTimeOriginal",   # when shutter fired — most authoritative
        "GPSDateStamp",       # GPS-recorded date
        "ImageGenerated",
        "CreateDate",         # when image was digitised (tag 0x9004)
        "ImageDigitized",
        "DateTimeDigitized",
    ],
    "XMP": [
        "DateTimeOriginal",   # XMP copy of original capture time
        "CreateDate",         # XMP create date
    ],
    "QuickTime": ["CreateDate", "TrackCreateDate"],
    "Composite": [
        "SubSecDateTimeOriginal",  # sub-second precision original
        "SubSecCreateDate",        # sub-second precision create
        "DateTimeCreated",         # XMP-derived composite (often most complete)
    ],
}

EXIFTOOL_DATE_FIELDS: list[str] = [
    f"{ns}:{field}"
    for ns, fields in _EXIFTOOL_DATE_FIELDS_BASE.items()
    for field in fields
]

EXIFTOOL_DATE_FIELDS_IGNORE: frozenset[str] = frozenset(
    [
        # Modify / access dates — must never drive file placement
        "EXIF:ModifyDate",
        "XMP:ModifyDate",
        "File:FileModifyDate",
        "File:FileAccessDate",
        "File:FileInodeChangeDate",
        # ExifTool bookkeeping fields
        "RIFF:DateTimeOriginal",
        "SourceFile",
        "File:Directory",
        "File:FileName",
    ]
)


# ── Base class ─────────────────────────────────────────────────────────────

class ExifReaderBase:
    """Abstract base for EXIF readers.  Subclasses implement ``get_raw``."""

    DATE_FIELDS: list[str] = []
    DATE_FIELDS_IGNORE: frozenset[str] = frozenset()

    def __init__(self, debug: bool = False) -> None:
        self._debug = debug
        self._recommended: set[str] = set()

    def get_raw(self, path: Path) -> dict:
        """Return raw EXIF dict for *path*.  Keys vary by backend."""
        raise NotImplementedError  # pragma: no cover

    def get_date(self, path: Path) -> Optional[date]:
        """Extract the best candidate date from *path*'s EXIF metadata."""
        raw = self.get_raw(path)
        return self._date_from_raw(raw, path)

    def _date_from_raw(self, raw: dict, path: Optional[Path] = None) -> Optional[date]:
        for field in self.DATE_FIELDS:
            if field in raw:
                d = parse_date(str(raw[field]))
                if d:
                    return d
        if self._debug and path:
            self._log_candidates(raw, path)
        return None

    def _log_candidates(self, raw: dict, path: Path) -> None:
        for k, v in raw.items():
            if (
                k not in self.DATE_FIELDS
                and k not in self.DATE_FIELDS_IGNORE
                and k not in self._recommended
                and parse_date(str(v))
            ):
                logger.debug('Consider EXIF field "%s" = "%s" in %s', k, v, path)
                self._recommended.add(k)


# ── Pillow backend ─────────────────────────────────────────────────────────

class ImageExifReader(ExifReaderBase):
    """Read EXIF from image files using Pillow."""

    DATE_FIELDS = IMAGE_DATE_FIELDS
    DATE_FIELDS_IGNORE = IMAGE_DATE_FIELDS_IGNORE

    def get_raw(self, path: Path) -> dict:
        try:
            from PIL import Image
            from PIL.ExifTags import TAGS

            with Image.open(path) as image:
                info = image.getexif()
            if not info:
                return {}

            # Main IFD (IFD 0) — contains DateTime, Make, Model, etc.
            raw = {TAGS.get(tag, tag): value for tag, value in info.items()}

            # Exif SubIFD (tag 34665) — contains DateTimeOriginal,
            # DateTimeDigitized, etc.  Pillow does NOT include these when
            # iterating getexif() directly; get_ifd() is required.
            exif_ifd = info.get_ifd(0x8769)  # 0x8769 == 34665
            raw.update({TAGS.get(tag, tag): value for tag, value in exif_ifd.items()})

            # GPS IFD (tag 34853) — contains GPSDateStamp
            gps_ifd = info.get_ifd(0x8825)   # 0x8825 == 34853
            raw.update({TAGS.get(tag, tag): value for tag, value in gps_ifd.items()})

            return raw
        except OSError:
            pass
        except Exception:
            logger.debug("Failed to read EXIF from image: %s", path)
        return {}


# ── ExifTool backend ──────────────────────────────────────────────────────

class ExifToolReader(ExifReaderBase):
    """Read EXIF via the ``exiftool`` subprocess, with batch support.

    The subprocess is started lazily on first use.  Use as a context manager
    for explicit lifecycle control::

        with ExifToolReader() as reader:
            date = reader.get_date(path)
    """

    DATE_FIELDS = EXIFTOOL_DATE_FIELDS
    DATE_FIELDS_IGNORE = EXIFTOOL_DATE_FIELDS_IGNORE

    def __init__(self, debug: bool = False, executable: Optional[str] = None) -> None:
        super().__init__(debug)
        self._tool = None
        self._executable = executable  # None → ExifToolHelper uses shutil.which

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        if self._tool is None:
            import exiftool
            # pyexiftool >= 0.5.x: ExifToolHelper auto-starts on first command
            # (auto_start=True by default); no explicit .start() or .run() needed.
            kwargs = {}
            if self._executable:
                kwargs["executable"] = self._executable
            self._tool = exiftool.ExifToolHelper(**kwargs)
            atexit.register(self._terminate)

    def _terminate(self) -> None:
        if self._tool is not None:
            try:
                self._tool.terminate()
            except Exception:
                pass
            self._tool = None

    def __enter__(self) -> "ExifToolReader":
        self._ensure_started()
        return self

    def __exit__(self, *_) -> None:
        self._terminate()

    # ── Public API ─────────────────────────────────────────────────────────

    def get_raw(self, path: Path) -> dict:
        self._ensure_started()
        try:
            # ExifToolHelper.get_metadata() returns a list of dicts (one per file)
            result = self._tool.get_metadata(str(path))
            return result[0] if result else {}
        except Exception as exc:
            logger.error("Failed to read EXIF via exiftool from %s: %s", path, exc)
            return {}

    def get_date_batch(self, paths: Iterable[Path]) -> Iterator[tuple[Path, Optional[date]]]:
        """Yield ``(path, date)`` pairs, using exiftool's batch API for speed."""
        self._ensure_started()
        path_list = list(paths)
        str_paths = [str(p) for p in path_list]
        try:
            # ExifToolHelper.get_metadata() accepts a list and returns a list
            metadata_list = self._tool.get_metadata(str_paths)
        except Exception as exc:
            logger.error("Batch EXIF read failed: %s", exc)
            metadata_list = [{} for _ in path_list]
        # ExifTool includes a SourceFile key in every result dict; use it to
        # match each metadata entry back to the correct Path so the pairing is
        # correct even when ExifTool returns results in a different order than
        # submitted.  Fall back to zip position when SourceFile is absent.
        str_to_path = {str(p): p for p in path_list}
        for path, raw in zip(path_list, metadata_list):
            raw = raw or {}
            source = raw.get("SourceFile")
            matched_path = str_to_path.get(source, path)
            yield matched_path, self._date_from_raw(raw, matched_path)


# ── MediaInfo backend ─────────────────────────────────────────────────────

class MediaInfoReader:
    """Read creation date from video containers via the ``mediainfo`` CLI.

    Used as a fallback for formats that ExifTool cannot date (e.g. old Nokia
    .3gp files).  Silently unavailable when ``mediainfo`` is not installed.
    """

    # Fields in the General track, tried in priority order.
    # Values from mediainfo are typically "UTC 2013-09-29 12:00:00" or just
    # "2013-09-29 12:00:00".
    _DATE_FIELDS = [
        "Encoded_Date",
        "Tagged_Date",
        "Mastered_Date",
        "File_Modified_Date",
    ]

    @classmethod
    def available(cls) -> bool:
        """Return True if the mediainfo binary is on PATH."""
        import shutil
        return shutil.which("mediainfo") is not None

    def get_raw(self, path: Path) -> dict:
        """Return the General track dict from mediainfo's JSON output."""
        import subprocess, json
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=JSON", str(path)],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout)
            for track in data.get("media", {}).get("track", []):
                if track.get("@type") == "General":
                    return track
        except FileNotFoundError:
            pass  # mediainfo not installed
        except Exception as exc:
            logger.debug("MediaInfo failed for %s: %s", path, exc)
        return {}

    def get_date(self, path: Path) -> Optional[date]:
        """Return the best creation date from mediainfo, or None."""
        raw = self.get_raw(path)
        for field in self._DATE_FIELDS:
            v = raw.get(field, "")
            if v:
                d = parse_date(str(v))
                if d:
                    return d
        return None
