"""
scanner.py — High-level scanning and deduplication orchestration.

``MediaScanner`` coordinates the three main operations:

* **load**     — import media from a source tree into the dated hierarchy
* **dupes**    — find and optionally remove duplicate files
* **validate** — find and optionally relocate misplaced files

It emits human-readable report lines via a ``Report`` object and writes
progress to stdout (identical to the original tool's behaviour).
"""

from __future__ import annotations

import itertools
import logging
import sys
import time
from collections import defaultdict
from datetime import date
from multiprocessing import Pool
from pathlib import Path
from typing import Callable, Iterator, Optional

from .config import Config
from .dates import parse_date
from .filesystem import DirCache, walk_media
from .hashing import hash_file
from .media import ImageFile, VideoFile, MediaFile, MediaRegistry

logger = logging.getLogger(__name__)


# ── Parallel worker ────────────────────────────────────────────────────────
# Must be module-level (not a method) so multiprocessing can pickle it.

def _image_exif_worker(path_str: str) -> tuple[str, Optional[date]]:
    """Read EXIF date from a single image file; returns ``(path_str, date_or_None)``.

    Called in worker processes via ``Pool.imap_unordered``.  Returning the
    path alongside the result lets the main process match results back to
    ``MediaFile`` objects without relying on submission order.
    """
    try:
        from deduper.exif import ImageExifReader
        from pathlib import Path
        return path_str, ImageExifReader().get_date(Path(path_str))
    except Exception:
        return path_str, None


def _hash_worker(args: tuple[str, int]) -> tuple[str, Optional[str]]:
    """Compute content hash for one file; returns ``(path_str, hex_or_None)``.

    Receives a ``(path_str, size)`` tuple so the size — already known from
    the stat performed during the walk phase — is not re-fetched by the worker.
    """
    path_str, size = args
    try:
        from deduper.hashing import hash_file
        from pathlib import Path
        return path_str, hash_file(Path(path_str), size)
    except OSError:
        return path_str, None


# ── Report ─────────────────────────────────────────────────────────────────

class Report:
    """Collects report lines.  Call the instance to append; call ``render``
    to get the complete text.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._start = time.monotonic()

    def __call__(self, *lines: object) -> None:
        self._lines.extend(str(l) for l in lines)

    def render(self) -> str:
        return "\n".join(self._lines)

    def send(self, address: Optional[str]) -> str:
        """Return the report text, appending an elapsed-time footer.

        Email delivery is a future enhancement.
        """
        elapsed = time.monotonic() - self._start
        self._lines.append(
            f"Completed in {int(elapsed // 60)}m{int(elapsed % 60):02d}s"
        )
        if address:
            logger.warning("Email reporting is not yet implemented (would send to %s)", address)
        return self.render()


# ── Scanner ────────────────────────────────────────────────────────────────

class MediaScanner:
    """Orchestrates all three scan modes.

    :param config:   Runtime configuration.
    :param report:   ``Report`` instance to write human-readable output to.
    :param registry: ``MediaRegistry`` — injectable for testing.
    :param dir_cache: ``DirCache`` — injectable for testing.
    """

    def __init__(
        self,
        config: Config,
        report: Optional[Report] = None,
        registry: Optional[MediaRegistry] = None,
        dir_cache: Optional[DirCache] = None,
    ) -> None:
        self._cfg = config
        self._report = report or Report()
        self._registry = registry or MediaRegistry(config)
        self._dir_cache = dir_cache or DirCache()

    @property
    def report(self) -> Report:
        return self._report

    # ── Entrypoints ────────────────────────────────────────────────────────

    def run_load(self) -> None:
        """Import media from ``config.import_path`` into the dated hierarchy."""
        if not self._cfg.import_path:
            self._report("Load skipped: no import path configured")
            return
        dated = self._collect([self._cfg.import_path], size_matters=False, only_invalid=False)
        n = sum(len(v) for v in dated.values())
        self._report(f"Importing {n} files")
        for dt, files in dated.items():
            self._report(f"  {dt}: {len(files)} file(s)")
            for mf in files:
                msg = mf.fix_date(self._registry, self._dir_cache)
                if msg:
                    self._report(f"    {msg}")

    def run_dupes(self) -> None:
        """Find (and optionally remove) duplicate files."""
        roots = [r for r in (self._cfg.photos_path, self._cfg.videos_path) if r]
        by_size = self._collect(roots, size_matters=True, only_invalid=False)
        candidates = list(itertools.chain.from_iterable(
            files for files in by_size.values() if len(files) > 1
        ))
        if not candidates:
            self._report("No duplicates found")
            return

        dupes = self._find_dupes(candidates)
        self._report_and_remove_dupes(dupes)

    def run_validate(self) -> None:
        """Find (and optionally fix) misplaced files."""
        roots = [r for r in (self._cfg.photos_path, self._cfg.videos_path) if r]
        misplaced = self._collect(roots, size_matters=False, only_invalid=True)
        if not misplaced:
            self._report("No misplaced files found")
            return

        # ── Summary by date + type ─────────────────────────────────────────
        sorted_dates = sorted(misplaced.keys(), key=lambda d: d or date.min)
        total_img = total_vid = 0
        rows: list[tuple] = []
        for dt in sorted_dates:
            files = misplaced[dt]
            n_img = sum(1 for f in files if isinstance(f, ImageFile))
            n_vid = len(files) - n_img
            total_img += n_img
            total_vid += n_vid
            rows.append((dt, n_img, n_vid))

        total = total_img + total_vid
        self._report("Misplaced files by date (images / videos):")
        for dt, n_img, n_vid in rows:
            label = str(dt) if dt else "(undatable)"
            self._report(f"  {label:12s}  {n_img:5d} img  {n_vid:5d} vid")
        self._report(
            f"** {total:,d} misplaced "
            f"({total_img:,d} images, {total_vid:,d} videos) "
            f"in {len(misplaced)} date group(s)"
        )

        # ── Undatable sample ───────────────────────────────────────────────
        if None in misplaced:
            undated = misplaced[None]
            sample_size = min(20, len(undated))
            self._report(
                f"\nUndatable files ({len(undated):,d} total) — sample of {sample_size}:"
            )
            for mf in undated[:sample_size]:
                self._report(f"  [{mf.MEDIA_TYPE}] {mf.path}")

        # ── Misplaced sample with current→expected + duplicate detection ──────
        dateable = [
            mf for dt, files in misplaced.items() if dt is not None
            for mf in files
        ]

        # Cheap existence check across all dateable misplaced files so the
        # summary can mention how many have something already at the target.
        n_has_target = sum(
            1 for mf in dateable
            if mf._normalised_directory
            and (mf._normalised_directory / mf.filename).exists()
        )
        if n_has_target:
            self._report(
                f"  ({n_has_target:,d} of the above already have a file at the "
                f"target location — likely duplicates; run --fix to resolve)"
            )

        sample_size = min(20, len(dateable))
        if dateable:
            self._report(
                f"\nMisplaced dateable files — sample of {sample_size} "
                f"(current directory → should be in):"
            )
            for mf in dateable[:sample_size]:
                expected = mf._normalised_directory
                path_date = parse_date(str(mf.path.parent))

                # Check whether the target already holds this file.
                # Hash is only computed when the target path exists (cheap first).
                target_note = ""
                if expected:
                    target_path = expected / mf.filename
                    if target_path.exists():
                        existing = self._registry.get_or_create(expected, mf.filename)
                        if existing and mf.hash == existing.hash:
                            target_note = "  [DUPLICATE at target — would be removed on --fix]"
                        else:
                            target_note = "  [COLLISION at target — different content]"

                self._report(
                    f"  [{mf.MEDIA_TYPE}] {mf.path.name}"
                    + (f"  (path date: {path_date})" if path_date and path_date != mf.dated else "")
                    + target_note
                )
                self._report(f"    currently: {mf.path.parent}")
                self._report(f"    should be: {expected}")

        if not self._cfg.do_fix:
            return
        for mf in itertools.chain.from_iterable(misplaced.values()):
            try:
                msg = mf.fix_date(self._registry, self._dir_cache)
            except Exception as exc:
                self._report(f"Error relocating {mf.path}: {exc}")
                logger.error("Unexpected error relocating %s: %s", mf.path, exc)
                continue
            if msg:
                self._report(msg)

    # ── Collection ─────────────────────────────────────────────────────────

    def _collect(
        self,
        roots: list[Path],
        size_matters: bool,
        only_invalid: bool,
    ) -> dict:
        """Walk *roots* and accumulate ``MediaFile`` objects.

        Three phases:

        1. **Walk** — discover files and build ``MediaFile`` objects (serial;
           directory I/O does not benefit from parallelism on a NAS).
        2. **EXIF prefetch** — if ``parallel`` is set, read image EXIF dates
           across a worker pool.  Video EXIF (ExifTool) is deliberately kept
           serial: the subprocess is CPU-heavy and does not survive fork.
        3. **Classify** — group results by size or date.

        :param size_matters:  Key by file size (for dupe pre-filter).
        :param only_invalid:  Key by date, include only misplaced files.
        :returns:             ``{key: [MediaFile, ...]}``
        """
        # Phase 1 — walk
        print(f"Scanning {roots} ...")
        sys.stdout.flush()
        all_files: list[MediaFile] = []
        walked = 0
        for directory, filename in walk_media(
            roots, self._cfg.exclude_dirs, self._cfg.exclude_files_re
        ):
            mf = self._registry.get_or_create(directory, filename)
            if mf:
                all_files.append(mf)
            walked += 1
            if walked % 5000 == 0:  # pragma: no cover
                print(f"\r  Walked {walked:,d} files...", end="", flush=True)
        print(f"\r  Walked {walked:,d} files, {len(all_files):,d} recognised media", flush=True)

        # Phase 2 — EXIF prefetch
        if self._cfg.parallel:
            self._prefetch_image_dates(all_files)   # fork-safe (Pillow), uses Pool
            self._prefetch_video_dates(all_files)   # batch ExifTool (one subprocess call)

        # Phase 3 — classify (video EXIF already warm if parallel, otherwise lazy)
        result: dict = defaultdict(list)
        n_all = len(all_files)
        for i, mf in enumerate(all_files):
            if size_matters:
                result[mf.size].append(mf)
            elif only_invalid:
                if mf.validate():
                    result[mf.dated].append(mf)
            else:
                result[mf.dated].append(mf)
            if (i + 1) % 1000 == 0:  # pragma: no cover
                print(f"\r  Classifying {i + 1:,d}/{n_all:,d}...", end="", flush=True)

        total = sum(len(v) for v in result.values())
        print(f"\r  Resolved {total:,d} relevant files from {n_all:,d} recognised")
        sys.stdout.flush()
        return result

    def _prefetch_image_dates(self, files: list[MediaFile]) -> None:
        """Pre-populate the date cache for image files using a worker pool.

        Only ``ImageFile`` objects are processed here — they use Pillow which
        is fork-safe.  ``VideoFile`` objects are left for lazy serial
        evaluation via ExifTool.

        Workers return a ``datetime.date`` or ``None``.  When ``None`` is
        returned the cache is left at ``_UNSET`` so that the normal lazy path
        (including the filename-date fallback) runs on first ``.dated`` access.
        """
        from .media import _UNSET

        image_files = [mf for mf in files if isinstance(mf, ImageFile)]
        if not image_files:
            return

        n = len(image_files)
        print(
            f"  Pre-fetching EXIF for {n:,d} images "
            f"(pool_size={self._cfg.pool_size}, chunksize={self._cfg.pool_chunksize})...",
            flush=True,
        )
        paths = [str(mf.path) for mf in image_files]
        path_to_mf = {str(mf.path): mf for mf in image_files}

        with Pool(self._cfg.pool_size) as pool:
            done = 0
            for path_str, d in pool.imap_unordered(
                _image_exif_worker, paths, chunksize=self._cfg.pool_chunksize
            ):
                if d is not None:
                    mf = path_to_mf.get(path_str)
                    if mf:
                        mf._dated = d  # Populate cache; skip filename fallback
                # else: leave as _UNSET → filename fallback runs on first access
                done += 1
                if done % 1000 == 0:  # pragma: no cover
                    print(f"\r    {done:,d}/{n:,d}...", end="", flush=True)

        print(f"\r  EXIF pre-fetch complete ({n:,d} images)", flush=True)

    def _prefetch_video_dates(self, files: list[MediaFile]) -> None:
        """Pre-populate the date cache for video files using ExifTool's batch API.

        ExifTool is not fork-safe so cannot go through the Pool, but its batch
        mode (passing a list of paths in a single subprocess call) is far faster
        than the per-file lazy path used during classify.

        MediaInfo is NOT called here — it is per-file and invoked lazily inside
        ``VideoFile._exif_date()`` only when ExifTool returns ``None``.
        """
        from .media import _UNSET
        from .exif import ExifToolReader

        video_files = [mf for mf in files if isinstance(mf, VideoFile)]
        if not video_files:
            return

        n = len(video_files)
        print(f"  Pre-fetching EXIF for {n:,d} videos (ExifTool batch)...", flush=True)

        reader = ExifToolReader(
            debug=self._cfg.debug, executable=self._cfg.exiftool_executable
        )
        try:
            path_to_mf = {mf.path: mf for mf in video_files}
            done = 0
            for path, d in reader.get_date_batch(path_to_mf.keys()):
                if d is not None:
                    path_to_mf[path]._dated = d
                done += 1
                if done % 500 == 0:  # pragma: no cover
                    print(f"\r    {done:,d}/{n:,d}...", end="", flush=True)
        finally:
            reader._terminate()

        print(f"\r  Video EXIF pre-fetch complete ({n:,d} videos)", flush=True)

    # ── Deduplication ──────────────────────────────────────────────────────

    def _find_dupes(
        self, candidates: list[MediaFile]
    ) -> dict[tuple, list[MediaFile]]:
        """Hash all *candidates* and return groups with identical hashes.

        When ``config.parallel`` is set, hashing is distributed across a
        worker pool using ``imap_unordered`` (order-independent, maximises
        throughput on NAS storage where per-file I/O latency varies).  The
        file size — already known from the walk-phase stat — is passed into
        each worker to avoid a redundant second ``stat`` call.
        """
        n = len(candidates)
        print(f"Hashing {n:,d} candidate files...")
        sys.stdout.flush()

        path_to_mf = {str(mf.path): mf for mf in candidates}
        groups: dict[tuple, list[MediaFile]] = defaultdict(list)

        if self._cfg.parallel:
            hash_args = [(str(mf.path), mf.size) for mf in candidates]
            with Pool(self._cfg.pool_size) as pool:
                done = 0
                for path_str, hex_hash in pool.imap_unordered(
                    _hash_worker, hash_args, chunksize=self._cfg.pool_chunksize
                ):
                    if hex_hash is not None:
                        mf = path_to_mf[path_str]
                        groups[(mf.size, hex_hash)].append(mf)
                    done += 1
                    if done % 1000 == 0:  # pragma: no cover
                        print(f"\r  Hashed {done:,d}/{n:,d}...", end="", flush=True)
        else:
            for i, mf in enumerate(candidates):
                if i % 1000 == 0 and i:  # pragma: no cover
                    print(f"\r  Hashed {i:,d}...", end="")
                    sys.stdout.flush()
                try:
                    groups[(mf.size, mf.hash)].append(mf)
                except OSError as exc:
                    logger.error("Could not hash %s: %s", mf.path, exc)

        print(f"\r  Hashed {n:,d} files")
        return {k: sorted(v) for k, v in groups.items() if len(v) > 1}

    def _report_and_remove_dupes(
        self, dupes: dict[tuple, list[MediaFile]]
    ) -> None:
        if not dupes:
            self._report("No exact duplicates found")
            return

        self._report(f"Found {len(dupes)} duplicate group(s):")
        dryrun_or_report_only = self._cfg.dryrun or not self._cfg.do_fix

        for key, group in dupes.items():
            keeper = group[0]
            redundant = group[1:]
            self._report(f"  Keep: {keeper}")
            for mf in redundant:
                if dryrun_or_report_only:
                    self._report(f"    Would remove: {mf}")
                else:
                    self._report(f"    Removing: {mf}")
                    mf.delete(self._registry)
