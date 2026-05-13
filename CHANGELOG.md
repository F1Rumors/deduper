# Changelog

All meaningful changes to the deduper codebase since the initial refactor from
`deduper3.py`.  Most recent changes at the top of each section.

---

## [Unreleased] — ongoing improvements

### Fixed — prune misses `@eaDir` and dryrun cascade
- `scanner.py`: `run_prune()` now filters excluded directory names (e.g.
  `@eaDir`, the Synology metadata directory) and excluded file patterns when
  deciding whether a directory is empty.  Previously a directory containing
  only `@eaDir` was treated as non-empty and not pruned.
- `scanner.py`: in dryrun mode a `virtually_removed` set tracks nominated
  directories so that parent directories that would become empty after their
  children are pruned are also nominated in the same pass.  Previously parents
  were skipped because the children still existed on disk.

### Changed — parameter cleanup and simplification
- Renamed `--debug` to `--verbose` throughout (Config field, CLI flag, INI key,
  ExifReader constructor parameter `debug=` → `verbose=`).
- Removed `--dryrun` flag and `DRYRUN` INI key.  Dry-run is now unconditionally
  the default; `--fix` is the only way to enable writes.  The `Config.dryrun`
  field remains (used internally by scanner/media) and is always `True` unless
  `--fix` is given.
- Removed `--verbose` implication of `--dryrun` (the two are now independent).
- Removed `_Tee` class and `--stdout` flag; output goes to stdout only and
  callers can use shell redirection (`deduper ... > run.log`).
- Added startup validation: `--photos`/`--videos` paths ending in a
  `YYYY/MM/DD` or `YYYY-MM-DD` date component are rejected with a clear error
  message (prevents accidentally using a dated subdirectory as the library root).

### Fixed — classify watchdog timeout on NAS under load
- `config.py`: added `_resolved_photos_path` / `_resolved_videos_path`
  `cached_property` accessors; `photos_path` and `videos_path` are now resolved
  once at startup instead of per file per classify call.
- `media.py`: removed `.resolve()` calls from `_valid_directories()`,
  `_normalised_directory`, and `_subdir_below_date()` — the root is already
  resolved via `Config._resolved_*_path`, eliminating ~28 lstat/readlink
  syscalls per file in the hot classify loop.
- `scanner.py`: when `_prefetch_image_dates()` worker returns `None` (no EXIF
  date), the filename/path fallback date is now computed and cached immediately
  so the classify loop never triggers a second Pillow read under NAS load.

### Added — MediaInfo fallback for video dating
- `exif.py`: new `MediaInfoReader` class using `mediainfo --Output=JSON`.
  Tries `Encoded_Date`, `Tagged_Date`, `Mastered_Date`, `File_Modified_Date`
  from the container's General track.
- `media.py`: `VideoFile` now accepts an optional `mediainfo_reader`; calls it
  when `ExifToolReader.get_date()` returns `None`.
- `media.py`: `MediaRegistry` auto-detects mediainfo at startup
  (`shutil.which`) and wires it in to all `VideoFile` instances when available.
- Motivation: 12 old Nokia `.3gp` files were undatable via ExifTool; MediaInfo
  can often recover creation dates from these containers.

### Added — batch video EXIF prefetch in parallel mode
- `scanner.py`: `_prefetch_video_dates()` passes all video paths to ExifTool in
  a single batch call (`ExifToolReader.get_date_batch`) before Phase 3 classify.
  Called automatically after `_prefetch_image_dates()` when `--parallel` is set.
- Previously, video dates were resolved one file at a time (lazily) during the
  classify loop.  Batch mode is substantially faster for large video collections
  because ExifTool's subprocess startup cost is paid only once.
- ExifTool is deliberately kept out of the Pool (not fork-safe); batch mode
  gives equivalent throughput gains without forking.

### Improved — validate report now breaks down by media type
- `scanner.py`: `run_validate()` summary table now shows image and video counts
  separately per date group, plus a grand total by type.
- Adds a 20-file sample of dateable misplaced files showing current directory
  and expected directory, so the cause of misplacement is immediately visible.
- Undated file sample already present; now also shows `[Image]`/`[Video]` tag.

### Added — `--getDate` diagnostic mode
- `cli.py`: passing file paths with `--getDate` shows a three-line breakdown per
  file: EXIF date, date parsed from the filename, date parsed from the directory
  path, and the final assigned date.
- Mode flags (`--exif`, `--getDate`, `--compareExif`) are now independent
  boolean flags; file paths are supplied as positional arguments.  Multiple
  flags can be combined (output is shown in that order, separated by blank
  lines).
- `--compareExif` now also works on specific paths (not just as a scan-time
  modifier for `--exif`).

### Changed — unified path list for diagnostic modes
- `cli.py`/`config.py`: replaced the two separate path-consuming arguments
  (`--exif PATH…` and `--getDate PATH…`) with a single positional `paths`
  argument and boolean mode flags.
- `config.py`: `exif_paths` / `getdate_paths` → `diagnostic_paths`; new
  boolean fields `do_exif` and `do_getdate`.

### Fixed — directory path used as date fallback
- `media.py`: `MediaFile.dated` now tries three sources in order:
  1. EXIF date
  2. Date pattern in the filename
  3. Date pattern in the directory path (e.g. `.../2013/09/29/...`)
- Benefit 1 (stability): files already correctly placed but lacking EXIF and
  filename dates are no longer reported as misplaced — they inherit the date
  implied by their location.
- Benefit 2 (import): files being imported from a dated inbox tree carry an
  implied date even when EXIF is absent.

---

## Initial porting session — making the code runnable on Synology DSM

### Fixed — CWD shadowing of installed `exiftool` package
- `deduper.py` entry point: strips `''`, `'.'`, and the current working
  directory from `sys.path` before importing.  Prevents a local `exiftool.py`
  file in the working directory from shadowing the installed `pyexiftool`
  package.

### Fixed — sys.path pointed at package directory instead of its parent
- `deduper.py`: `sys.path.insert(0, ...)` now uses
  `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` (the `claude/`
  directory) so that `from deduper.cli import main` resolves correctly.

### Fixed — missing `__init__.py`
- Created `deduper/__init__.py` (empty) so the directory is recognised as a
  Python package.

### Fixed — pyexiftool 0.5.6 API changes
- `exif.py`: replaced `exiftool.ExifTool` with `exiftool.ExifToolHelper`;
  removed explicit `.start()` / `.run()` calls (auto-start is the default).
  `get_metadata()` returns a list; unwrapped with `[0]` for single-file calls.
  Batch path accepts a plain list.

### Fixed — Pillow SubIFD not read, causing 6 664 images to be undatable
- `exif.py` `ImageExifReader.get_raw()`: Pillow's `getexif()` iterator only
  covers IFD 0 (the main image IFD).  `DateTimeOriginal` lives in the Exif
  SubIFD (tag 0x8769); `GPSDateStamp` lives in the GPS IFD (tag 0x8825).
  Both are now explicitly read via `info.get_ifd()` and merged into the raw
  dict.  Result: 6 664 previously undatable images acquired correct dates.

### Fixed — DecompressionBombWarning not suppressed
- `exif.py`: set `PIL.Image.MAX_IMAGE_PIXELS = None` at module level rather
  than using `warnings.filterwarnings()` (which was unreliable when Pillow is
  imported across multiple processes).

### Fixed — Pool created but imap never called
- `scanner.py`: the original parallel path created a `Pool` but iterated the
  plain file list instead of calling `pool.imap`.  Restructured `_collect()`
  into three explicit phases: Walk → EXIF prefetch (parallel) → Classify.

### Fixed — worker code duplicated date-parsing logic
- `scanner.py` `_image_exif_worker()`: removed inline date parsing; now
  delegates entirely to `ImageExifReader().get_date()` so field lists, SubIFD
  traversal, and date parsing are identical whether running in the pool or
  serially.

### Fixed — `run_validate()` crash when dates include `None`
- `scanner.py`: `sorted(dirs.items())` raised `TypeError` because `None` and
  `datetime.date` cannot be compared with `<`.  Fixed with
  `key=lambda kv: kv[0] or date.min`.

### Fixed — `_relocate()` created directories during dryrun
- `media.py`: `dir_cache.ensure()` and the write-access check were called
  before the dryrun guard.  Both are now skipped when `config.dryrun` is `True`.

### Fixed — symlink path mismatch on Synology DSM
- DSM symlinks `/opt` → `/volume1/@Entware/opt`.  Walking returns the
  unresolved path; `_normalised_directory` returned the resolved path.
  Comparisons always failed.
- `media.py`: `__init__` resolves `self._dir`; `_normalised_directory`,
  `validate()`, and `fix_date()` all resolve `target_root` before comparing.

### Added — configurable exclusions
- `config.py`: `exclude_dirs` (frozenset) and `exclude_files_re` (str) fields,
  readable from INI `[LOCATIONS]` and overridable via `--exclude-dir` (repeatable)
  and `--exclude-re` CLI flags.
- `filesystem.py`: `walk_media()` accepts these as parameters with the previous
  hard-coded defaults as defaults.

### Added — ExifTool executable path configuration
- `config.py`: `exiftool_executable` field, set via INI `[GLOBALS] exiftool_path`
  or CLI `--exiftool PATH`.  Passed through to `ExifToolReader` and
  `MediaRegistry`.

### Added — parallel image EXIF prefetch
- `scanner.py`: when `--parallel` is set, image EXIF dates are read across a
  `multiprocessing.Pool` using `pool.imap()` with configurable `pool_size` and
  `pool_chunksize` (INI: `POOL_SIZE`, `POOL_CHUNKSIZE`; defaults 4 / 100).
  Pillow is fork-safe; ExifTool (subprocess) is not and remains serial.

### Added — `--exif` diagnostic mode (initial version)
- `cli.py`: `--exif PATH…` dumps raw ExifTool metadata for the listed files and
  exits.  Combined with `--compareExif` it reports differences between Pillow
  and ExifTool date readings.

### Added — `requirements.txt`
- Lists `pillow` and `pyexiftool>=0.5.6`.

### Added — undatable file sample in validate report
- `scanner.py`: when undatable files are present (`dated = None`), up to 20
  file paths are shown so the root cause can be investigated without running
  `--getDate` separately.

### Fixed — Python environment on DSM
- Synology Entware installs Python 3.11 at `/opt/bin/python3.11`.  The `env/`
  virtualenv was linked to the system Python 3.8 and did not have `pyexiftool`.
  Resolution: invoke `python3.11` directly rather than through the venv.

---

*This file should be updated whenever a bug is fixed, a feature is added, or
behaviour changes in a non-trivial way.  Keep most recent changes at the top.*
