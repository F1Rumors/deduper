"""
media.py — Domain model for media files.

Classes
-------
MediaFile
    Abstract base representing a single media file on disk.  Provides lazy,
    cached access to ``size``, ``hash``, ``dated``, and ``misdated``.

ImageFile / VideoFile
    Concrete subclasses that know which EXIF backend to use and what the
    appropriate destination root is.

MediaRegistry
    Factory + cache that maps absolute paths to ``MediaFile`` instances.
    Instance-level (not class-level) cache eliminates inter-test contamination.

Design choices
--------------
* All configuration is passed via a ``Config`` instance — no global state.
* The ``_key`` tuple used for sorting duplicates is unchanged from the
  original (preserving the battle-tested preference ordering).
* File moves update the registry atomically within the same object.
* ``_testDate`` / ``_dated`` race avoided by using a ``_UNSET`` sentinel.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import date
from functools import total_ordering
from pathlib import Path
from typing import Optional

# Windows Explorer "file - Copy.ext" / "file - Copy (2).ext" pattern.
# Space (ASCII 32) sorts before period (ASCII 46), which would make Copy files
# sort ahead of originals alphabetically; this regex lets us detect and
# penalise them explicitly in the _key tuple.
_COPY_RE = re.compile(r"\s*-\s*[Cc]opy(?:\s*\(\d+\))?\.\w+$")

from .config import Config
from .dates import parse_date, split_path_on_date, date_to_str
from .exif import ImageExifReader, ExifToolReader, MediaInfoReader
from .filesystem import DirCache, safe_filename
from .hashing import hash_file

logger = logging.getLogger(__name__)

_UNSET = object()  # Sentinel for uninitialised cached properties


# ── Supported extensions ───────────────────────────────────────────────────

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    ["cr2", "exr", "gif", "jbf", "jpg", "jpeg", "png", "spp", "tif", "tiff", "xmp"]
)
VIDEO_EXTENSIONS: frozenset[str] = frozenset(["avi", "mp4", "mov", "3gp", "3gpp", "mkv"])
AUDIO_EXTENSIONS: frozenset[str] = frozenset(["aac", "m4a", "mp3", "wav"])
IGNORED_EXTENSIONS: frozenset[str] = AUDIO_EXTENSIONS | frozenset(["thm", "tmp", "m", "info"])


# ── Base class ─────────────────────────────────────────────────────────────

@total_ordering
class MediaFile:
    """Represents a single media file.  Lazy-loads size, hash and date."""

    MEDIA_TYPE: str = "Unknown"

    def __init__(self, directory: Path, filename: str, config: Config) -> None:
        self._dir = directory.resolve()
        self._filename = filename
        self._config = config
        # Cached values
        self._size: Optional[int] = None
        self._hash: Optional[str] = None
        self._dated = _UNSET          # cache for .dated property
        self._seq: Optional[int] = None
        self._original: Optional[bool] = None
        self._deleted: bool = False
        self._error: bool = False

    # ── Path accessors ─────────────────────────────────────────────────────

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def filename(self) -> str:
        return self._filename

    @property
    def path(self) -> Path:
        """Full absolute path including filename."""
        return self._dir / self._filename

    # ── File metadata ──────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        """File size in bytes; -1 on error."""
        if self._size is None:
            try:
                self._size = self.path.stat().st_size
            except OSError:
                self._size = -1
                self._error = True
        return self._size

    @property
    def hash(self) -> str:
        """Partial content hash (see ``hashing.py``)."""
        if self._hash is None:
            self._hash = hash_file(self.path, self.size)
        return self._hash

    @property
    def sequence(self) -> int:
        """Sequence number embedded in filename (e.g. ``_001``), or 0."""
        if self._seq is None:
            m = re.search(r"[\-_]+(\d{2,3})\.\w+$", self._filename)
            self._seq = int(m.group(1)) if m else 0
        return self._seq

    @property
    def _is_copy(self) -> bool:
        """True if the filename follows Windows 'file - Copy.ext' naming."""
        return bool(_COPY_RE.search(self._filename))

    @property
    def original(self) -> bool:
        """True if the file resides under a directory named 'originals'."""
        if self._original is None:
            self._original = "originals" in str(self._dir).lower()
        return self._original

    @property
    def deleted(self) -> bool:
        return self._deleted

    @property
    def error(self) -> bool:
        return self._error

    # ── Date logic ─────────────────────────────────────────────────────────

    @property
    def dated(self) -> Optional[date]:
        """Best-guess date for this file (EXIF → filename → directory path)."""
        if self._dated is _UNSET:
            self._dated = (
                self._exif_date()
                or parse_date(self._filename)
                or parse_date(str(self._dir))
            )
        return self._dated  # type: ignore[return-value]

    def _exif_date(self) -> Optional[date]:
        raise NotImplementedError  # pragma: no cover

    @property
    def misdated(self) -> bool:
        """True if the directory path encodes a date that differs from the
        file's own date.  Files with no path date are not considered misdated.
        """
        path_date = parse_date(str(self._dir))
        return bool(path_date and self.dated and self.dated != path_date)

    @property
    def _in_correct_location(self) -> bool:
        """True if the file's current directory is a valid home for its date."""
        if not self.dated or not self.default_root:
            return False
        return self._dir in self._valid_directories(self.default_root)

    # ── Destination path ───────────────────────────────────────────────────

    @property
    def default_root(self) -> Optional[Path]:
        """Root directory under which this media type should live."""
        raise NotImplementedError  # pragma: no cover

    @property
    def _normalised_directory(self) -> Optional[Path]:
        """The directory this file *should* be in, or None if undetermined.

        Always resolved so that comparisons against ``self._dir`` (also
        resolved) work correctly even when the path passes through symlinks
        (e.g. DSM's /opt → /volume1/@Entware/opt).
        """
        if not self.dated or not self.default_root:
            return None
        return (self.default_root / date_to_str(self.dated, self._config.default_sep)).resolve()

    # ── Sorting key (preserves original preference ordering) ──────────────

    @property
    def _key(self) -> tuple:
        return (
            not self._deleted,
            not self.original,
            not self._in_correct_location,  # correctly-placed files sort first
            self._is_copy,    # Windows "- Copy" files sort after originals
            self.sequence,
            self._filename,
            str(self.path),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MediaFile):
            return NotImplemented
        return self._key == other._key

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, MediaFile):
            return NotImplemented
        return self._key < other._key

    def __hash__(self) -> int:
        # Hash on path so instances can live in sets/dicts keyed by location
        return hash(self.path)

    def __str__(self) -> str:
        return self._filename

    def __repr__(self) -> str:
        parts = [str(self.path)]
        if self._config.debug and self._dated is not _UNSET and self.misdated:
            parts.append(date_to_str(self.dated, self._config.default_sep))
        if self._deleted:
            parts.append("DELETED")
        return "->".join(parts)

    # ── Mutating operations ────────────────────────────────────────────────

    def delete(self, registry: "MediaRegistry") -> "MediaFile":
        """Delete the underlying file.  No-op if already deleted.

        In dryrun mode the file is not removed but the object is marked
        deleted so downstream logic behaves as if it were.
        """
        if self._deleted:
            return self
        if self._config.dryrun:
            logger.info("dryrun: would delete %s", self.path)
        else:
            self.path.unlink()
        self._deleted = True
        registry.evict(self.path)
        return self

    def fix_date(
        self,
        registry: "MediaRegistry",
        dir_cache: DirCache,
        target_root: Optional[Path] = None,
        overflow_path: Optional[Path] = None,
    ) -> Optional[str]:
        """Move this file to its normalised location.

        :param registry:     The active ``MediaRegistry`` (updated on move).
        :param dir_cache:    ``DirCache`` instance for directory creation.
        :param target_root:  Override the default root for this media type.
        :param overflow_path: Where to put files that clash with an existing
                              non-duplicate.  Falls back to
                              ``config.misdated_path``.
        :returns: A human-readable message describing what happened, or
                  ``None`` on success.
        """
        if self._deleted:
            return f"{self}: cannot relocate — already deleted"
        if not self.dated:
            return f"{self}: cannot relocate — no date found"

        target_dir = (
            (target_root / date_to_str(self.dated, self._config.default_sep)).resolve()
            if target_root
            else self._normalised_directory
        )
        if target_dir is None:
            return f"{self}: cannot relocate — no target root configured"

        if target_dir == self._dir:
            return None  # Already in the right place

        alt = overflow_path or self._config.misdated_path
        return self._relocate(target_dir, registry, dir_cache, alt)

    def _valid_directories(self, root: Path) -> frozenset[Path]:
        """All directory paths that are valid homes for this file.

        Both separator conventions (``yyyy/mm/dd`` and ``yyyy-mm-dd``) are
        accepted so that a library originally organised with one format is not
        treated as entirely misplaced when ``default_sep`` changes.
        """
        return frozenset(
            (root / date_to_str(self.dated, sep)).resolve()
            for sep in ("/", "-")
        )

    def validate(self, target_root: Optional[Path] = None) -> Optional[str]:
        """Return a problem description if the file is misplaced, else None."""
        if self._deleted:
            return None
        if not self.dated:
            return f"{self}: no date found — cannot validate placement"
        root = target_root or self.default_root
        if root is None:
            return f"{self}: no target root configured"
        if self._dir in self._valid_directories(root):
            return None
        expected = self._normalised_directory
        return f"{self}: expected in {expected}, found in {self._dir}"

    # ── Internal helpers ───────────────────────────────────────────────────

    def _relocate(
        self,
        target_dir: Path,
        registry: "MediaRegistry",
        dir_cache: DirCache,
        overflow: Optional[Path],
    ) -> Optional[str]:
        """Move self to *target_dir*, handling collisions."""
        if target_dir.resolve() == self._dir:
            # Guard against self-deletion: this arises when the overflow path
            # equals the source directory (misconfigured misdated_path).  The
            # file would appear to already exist at the target, hash-match
            # itself, and be deleted as a "duplicate".
            return f"{self}: target directory is the same as source — skipping"
        if not os.access(self._dir, os.W_OK):  # pragma: no cover
            return f"Cannot write to source directory {self._dir}"
        if not self.path.exists():
            return f"Source file missing: {self.path}"

        no_changes = self._config.dryrun

        if not no_changes:
            dir_cache.ensure(target_dir)
            if not os.access(target_dir, os.W_OK):  # pragma: no cover
                return f"Cannot write to target directory {target_dir}"

        target_path = target_dir / self._filename

        if target_path.exists():
            existing = registry.get_or_create(target_dir, self._filename)
            if existing and self.hash == existing.hash:
                # Exact duplicate — delete self
                if no_changes:
                    self._deleted = True
                    return f"dryrun: {self} is a duplicate of {target_path} — would delete"
                self.delete(registry)
                return f"Removed duplicate: {self}"

            # Collision with a different file
            if overflow:
                return self._relocate(overflow, registry, dir_cache, None)
            if self._config.do_force:
                new_name = safe_filename(target_dir, self._filename)
                target_path = target_dir / new_name
            else:
                return (
                    f"Collision: {self._filename} already exists in {target_dir} "
                    f"(use --force to rename)"
                )

        if no_changes:
            return f"dryrun: would move {self.path} → {target_path}"

        registry.move(self, target_dir, target_path.name)
        return None


# ── Concrete subclasses ────────────────────────────────────────────────────

class ImageFile(MediaFile):
    """A still image file (JPEG, PNG, RAW, etc.)."""

    MEDIA_TYPE = "Image"

    def __init__(
        self,
        directory: Path,
        filename: str,
        config: Config,
        exif_reader: Optional[ImageExifReader] = None,
    ) -> None:
        super().__init__(directory, filename, config)
        self._exif_reader = exif_reader or ImageExifReader(debug=config.debug)

    def _exif_date(self) -> Optional[date]:
        return self._exif_reader.get_date(self.path)

    @property
    def default_root(self) -> Optional[Path]:
        return self._config.photos_path


class VideoFile(MediaFile):
    """A video file (MP4, AVI, MOV, etc.)."""

    MEDIA_TYPE = "Video"

    def __init__(
        self,
        directory: Path,
        filename: str,
        config: Config,
        exif_reader: Optional[ExifToolReader] = None,
        mediainfo_reader: Optional[MediaInfoReader] = None,
    ) -> None:
        super().__init__(directory, filename, config)
        self._exif_reader = exif_reader or ExifToolReader(debug=config.debug)
        self._mediainfo_reader = mediainfo_reader  # None → MediaInfo not used

    def _exif_date(self) -> Optional[date]:
        d = self._exif_reader.get_date(self.path)
        if d is None and self._mediainfo_reader is not None:
            d = self._mediainfo_reader.get_date(self.path)
        return d

    @property
    def default_root(self) -> Optional[Path]:
        return self._config.videos_path


# ── Registry / factory ─────────────────────────────────────────────────────

class MediaRegistry:
    """Creates and caches ``MediaFile`` instances by absolute path.

    Shared EXIF readers are created once and reused across all files of the
    same type — important for ``ExifToolReader`` which manages a subprocess.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._cache: dict[Path, Optional[MediaFile]] = {}
        self._image_reader = ImageExifReader(debug=config.debug)
        self._video_reader = ExifToolReader(debug=config.debug, executable=config.exiftool_executable)
        self._mediainfo_reader = MediaInfoReader() if MediaInfoReader.available() else None
        if self._mediainfo_reader:
            logger.debug("MediaInfo available — will use as video fallback")
        self._unknown_extensions: dict[str, int] = {}  # ext → file count

    def get_or_create(self, directory: Path, filename: str) -> Optional[MediaFile]:
        """Return the ``MediaFile`` for ``directory/filename``, creating it on
        first access.  Returns ``None`` for files with unsupported or ignored
        extensions.
        """
        key = (directory / filename).resolve()
        if key in self._cache:
            return self._cache[key]

        media = self._build(directory, filename)
        self._cache[key] = media
        return media

    def _build(self, directory: Path, filename: str) -> Optional[MediaFile]:
        if "." not in filename:
            return None
        ext = filename.rsplit(".", 1)[1].lower()

        if ext in IMAGE_EXTENSIONS:
            return ImageFile(directory, filename, self._config, self._image_reader)
        if ext in VIDEO_EXTENSIONS:
            return VideoFile(directory, filename, self._config, self._video_reader, self._mediainfo_reader)
        if ext not in IGNORED_EXTENSIONS:
            if ext not in self._unknown_extensions:
                logger.info("Unsupported extension ignored: .%s (%s)", ext, filename)
            self._unknown_extensions[ext] = self._unknown_extensions.get(ext, 0) + 1
        return None

    def evict(self, path: Path) -> None:
        """Remove *path* from the cache (called after deletion)."""
        self._cache.pop(path.resolve(), None)

    def move(self, media: MediaFile, new_dir: Path, new_name: str) -> None:
        """Rename the file on disk and update the cache and object state."""
        old_path = media.path.resolve()
        new_path = (new_dir / new_name).resolve()

        if new_path in self._cache:
            raise RuntimeError(
                f"Cannot move {old_path} to {new_path} — target already in registry"
            )

        shutil.move(str(old_path), str(new_path))
        self._cache.pop(old_path, None)
        media._dir = new_dir.resolve()
        media._filename = new_name
        media._hash = None  # Invalidate cached hash (path changed)
        self._cache[new_path] = media
