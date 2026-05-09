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
import os
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
        # Keyed by frozenset of root paths.  Each entry holds the walked file
        # list plus both pre-classified groupings so subsequent actions on the
        # same roots skip the walk and the classify pass entirely.
        # Deleted files are evicted via _evict_from_scan rather than disabling
        # the cache for --fix runs.
        self._scan_cache: dict[frozenset, dict] = {}

    @property
    def report(self) -> Report:
        return self._report

    # ── Entrypoints ────────────────────────────────────────────────────────

    def run_load(self) -> None:
        """Import media from ``config.import_path`` into the dated hierarchy."""
        if not self._cfg.import_path:
            self._report("Load skipped: no import path configured")
            return
        dated = self._collect([self._cfg.import_path])
        n = sum(len(v) for v in dated.values())
        self._report(f"Importing {n} files")
        n_imported = n_errors = 0
        for dt, files in dated.items():
            self._report(f"  {dt}: {len(files)} file(s)")
            for mf in files:
                msg = mf.fix_date(self._registry, self._dir_cache)
                if msg:
                    self._report(f"    {msg}")
                    n_errors += 1
                else:
                    n_imported += 1
        if self._cfg.debug:
            self._report(
                f"\nLoad summary: {n_imported:,d} imported, {n_errors:,d} error(s)/skipped"
            )

    def run_dupes(self) -> None:
        """Find (and optionally remove) duplicate files."""
        roots = [r for r in (self._cfg.photos_path, self._cfg.videos_path) if r]
        entry = self._scan(roots)
        candidates = list(itertools.chain.from_iterable(
            files for files in entry['by_size'].values() if len(files) > 1
        ))
        if not candidates:
            self._report("No duplicates found")
            return

        cache_key = frozenset(roots)
        dupes = self._find_dupes(candidates)
        self._report_and_remove_dupes(dupes, cache_key)

    def run_validate(self) -> None:
        """Find (and optionally fix) misplaced files."""
        roots = [r for r in (self._cfg.photos_path, self._cfg.videos_path) if r]
        misplaced = self._scan(roots)['by_misplaced']
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

        if dateable:
            self._report(
                f"\nMisplaced dateable files — {len(dateable):,d} total "
                f"(current directory → should be in):"
            )
            for mf in dateable:
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

        # ── Debug: expected fix outcomes (computed without touching disk) ────
        if self._cfg.debug:
            outcomes = self._count_expected_outcomes(dateable)
            parts = []
            if outcomes['relocate']:
                parts.append(f"{outcomes['relocate']:,d} would relocate")
            if outcomes['remove_dup']:
                parts.append(f"{outcomes['remove_dup']:,d} would remove as duplicate")
            if outcomes['overflow']:
                parts.append(f"{outcomes['overflow']:,d} would send to overflow dir")
            if outcomes['skip']:
                parts.append(f"{outcomes['skip']:,d} would skip (collision/no root)")
            self._report("\nExpected fix outcomes: " + (", ".join(parts) if parts else "none"))

        if self._cfg.dryrun:
            return

        # ── Fix loop with outcome tracking ──────────────────────────────────
        n_relocated = n_to_overflow = n_removed_dup = n_errors = 0
        for mf in itertools.chain.from_iterable(misplaced.values()):
            expected_dir = mf._normalised_directory
            try:
                msg = mf.fix_date(self._registry, self._dir_cache)
            except Exception as exc:
                self._report(f"Error relocating {mf.path}: {exc}")
                logger.error("Unexpected error relocating %s: %s", mf.path, exc)
                n_errors += 1
                continue
            if msg:
                self._report(msg)
                msg_l = msg.lower()
                if "removed duplicate" in msg_l:
                    n_removed_dup += 1
                elif any(kw in msg_l for kw in (
                    "error", "cannot", "missing", "collision", "same as source", "skipping"
                )):
                    n_errors += 1
                # dryrun and other informational messages → not counted as fix outcomes
            else:
                # None → actual move occurred (impossible in dryrun mode)
                if expected_dir and mf.directory.resolve() == expected_dir.resolve():
                    n_relocated += 1
                else:
                    n_to_overflow += 1

        self._report(
            f"\nFix summary: {n_relocated:,d} relocated, "
            f"{n_to_overflow:,d} sent to overflow dir, "
            f"{n_removed_dup:,d} removed as duplicate, "
            f"{n_errors:,d} error(s)"
        )

    def run_prune(self) -> None:
        """Remove empty directories from the photos and videos trees (bottom-up).

        Skips the root directories themselves.  Respects ``config.dryrun``.
        """
        roots = [r.resolve() for r in (self._cfg.photos_path, self._cfg.videos_path) if r]
        if not roots:
            self._report("Prune skipped: no library paths configured")
            return

        n_pruned = n_would_prune = 0
        for root in roots:
            if not root.is_dir():
                continue
            for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
                d = Path(dirpath).resolve()
                if d == root:
                    continue
                try:
                    if any(d.iterdir()):
                        continue
                except OSError:
                    continue
                if self._cfg.dryrun:
                    self._report(f"  Would prune: {d}")
                    n_would_prune += 1
                else:
                    try:
                        d.rmdir()
                        self._report(f"  Pruned: {d}")
                        n_pruned += 1
                    except OSError as exc:
                        self._report(f"  Could not prune {d}: {exc}")

        count = n_would_prune if self._cfg.dryrun else n_pruned
        noun = "directory" if count == 1 else "directories"
        verb = "would be removed" if self._cfg.dryrun else "removed"
        self._report(f"\nPrune: {count:,d} empty {noun} {verb}")

    # ── Walk ───────────────────────────────────────────────────────────────

    def _do_walk(self, roots: list[Path]) -> list[MediaFile]:
        """Walk *roots* and return all recognised ``MediaFile`` objects."""
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
        if self._cfg.debug and self._registry._unknown_extensions:
            parts = sorted(
                f".{ext} ({n:,d})"
                for ext, n in self._registry._unknown_extensions.items()
            )
            self._report(f"\nIgnored files with unrecognised extensions: {', '.join(parts)}")
        return all_files

    # ── Shared scan (dupes + validate) ────────────────────────────────────

    def _scan(self, roots: list[Path]) -> dict:
        """Walk *roots* once and build groupings for whichever actions are active.

        ``by_size`` is always built (needed for dupes pre-filter).
        ``by_misplaced`` is only built when ``do_validate`` is True — this
        avoids reading EXIF dates for every file during a dupes-only run.

        Results are cached by root set.  If ``run_validate`` is called after
        ``run_dupes`` on the same roots, the misplaced grouping is built
        lazily from the already-walked ``all_files`` list without re-walking.
        """
        cache_key = frozenset(roots)
        if cache_key in self._scan_cache:
            entry = self._scan_cache[cache_key]
            if self._cfg.do_validate and not entry['misplaced_classified']:
                self._classify_misplaced(entry)
            print(f"Reusing cached scan ({len(entry['all_files']):,d} files)")
            sys.stdout.flush()
            return entry

        all_files = self._do_walk(roots)

        if self._cfg.parallel:
            self._prefetch_image_dates(all_files)   # fork-safe (Pillow), uses Pool
            self._prefetch_video_dates(all_files)   # batch ExifTool (one subprocess call)

        by_size: dict[int, list[MediaFile]] = defaultdict(list)
        n_all = len(all_files)
        for i, mf in enumerate(all_files):
            by_size[mf.size].append(mf)
            if (i + 1) % 1000 == 0:  # pragma: no cover
                print(f"\r  Sizing {i + 1:,d}/{n_all:,d}...", end="", flush=True)
        print(f"\r  Sized {n_all:,d} files")
        sys.stdout.flush()

        entry = {
            'all_files': all_files,
            'by_size': dict(by_size),
            'by_misplaced': {},
            'misplaced_classified': False,
        }
        self._scan_cache[cache_key] = entry

        if self._cfg.do_validate:
            self._classify_misplaced(entry)

        return entry

    def _classify_misplaced(self, entry: dict) -> None:
        """Build the ``by_misplaced`` grouping from ``all_files`` in *entry*.

        Reads EXIF dates for every file; called only when ``do_validate`` is
        True.  Safe to call on a cache entry that was populated by a
        dupes-only scan — no re-walk is needed.
        """
        by_misplaced: dict = defaultdict(list)
        all_files = entry['all_files']
        n_all = len(all_files)
        for i, mf in enumerate(all_files):
            if mf.validate():
                by_misplaced[mf.dated].append(mf)
            if (i + 1) % 1000 == 0:  # pragma: no cover
                print(f"\r  Classifying {i + 1:,d}/{n_all:,d}...", end="", flush=True)
        n_misplaced = sum(len(v) for v in by_misplaced.values())
        print(f"\r  Classified {n_all:,d} files — {n_misplaced:,d} misplaced")
        sys.stdout.flush()
        entry['by_misplaced'] = dict(by_misplaced)
        entry['misplaced_classified'] = True

    def _count_expected_outcomes(self, files: list[MediaFile]) -> dict[str, int]:
        """Predict what fix_date would do for each file without making changes.

        Returns counts keyed by outcome:
        * ``relocate``   — target path is free; file would move there.
        * ``remove_dup`` — identical file already at target; source would be deleted.
        * ``overflow``   — collision at target with different content; file would go
                           to ``misdated_path`` (if configured).
        * ``skip``       — collision with no overflow, or no target root; no action.
        """
        counts: dict[str, int] = {'relocate': 0, 'remove_dup': 0, 'overflow': 0, 'skip': 0}
        for mf in files:
            expected = mf._normalised_directory
            if expected is None:
                counts['skip'] += 1
                continue
            target = expected / mf.filename
            if target.exists():
                existing = self._registry.get_or_create(expected, mf.filename)
                if existing and mf.hash == existing.hash:
                    counts['remove_dup'] += 1
                elif self._cfg.misdated_path:
                    counts['overflow'] += 1
                else:
                    counts['skip'] += 1
            else:
                counts['relocate'] += 1
        return counts

    def _evict_from_scan(self, cache_key: frozenset, mf: MediaFile) -> None:
        """Remove *mf* from all cached structures after it has been deleted."""
        entry = self._scan_cache.get(cache_key)
        if not entry:
            return
        try:
            entry['all_files'].remove(mf)
        except ValueError:
            pass
        size_group = entry['by_size'].get(mf.size)
        if size_group:
            try:
                size_group.remove(mf)
            except ValueError:
                pass
        misplaced_group = entry['by_misplaced'].get(mf.dated)
        if misplaced_group:
            try:
                misplaced_group.remove(mf)
            except ValueError:
                pass

    # ── Collection (run_load only) ─────────────────────────────────────────

    def _collect(self, roots: list[Path]) -> dict:
        """Walk *roots* and group by date.

        Used by ``run_load`` for import-path walks (not cached, different roots
        and semantics from the photos/videos scan).

        :returns: ``{date_or_None: [MediaFile, ...]}``
        """
        all_files = self._do_walk(roots)

        if self._cfg.parallel:
            self._prefetch_image_dates(all_files)
            self._prefetch_video_dates(all_files)

        result: dict = defaultdict(list)
        n_all = len(all_files)
        for i, mf in enumerate(all_files):
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
        self,
        dupes: dict[tuple, list[MediaFile]],
        cache_key: frozenset = frozenset(),
    ) -> None:
        if not dupes:
            self._report("No exact duplicates found")
            return

        n_redundant = sum(len(group) - 1 for group in dupes.values())
        self._report(f"Found {len(dupes)} duplicate group(s), {n_redundant:,d} redundant file(s):")
        dryrun_or_report_only = self._cfg.dryrun
        n_removed = 0

        for key, group in dupes.items():
            keeper = group[0]
            redundant = group[1:]
            self._report(f"  Keep: {keeper.path}")
            for mf in redundant:
                if dryrun_or_report_only:
                    self._report(f"    Would remove: {mf.path}")
                else:
                    self._report(f"    Removing: {mf.path}")
                    mf.delete(self._registry)
                    self._evict_from_scan(cache_key, mf)
                    n_removed += 1

        if self._cfg.debug:
            if dryrun_or_report_only:
                self._report(
                    f"\nDupes summary: {len(dupes)} group(s), "
                    f"{n_redundant:,d} redundant — not removed (dryrun/report-only)"
                )
            else:
                self._report(
                    f"\nDupes summary: {len(dupes)} group(s), "
                    f"{n_removed:,d} of {n_redundant:,d} redundant file(s) removed"
                )
