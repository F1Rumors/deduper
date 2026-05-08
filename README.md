# deduper

A command-line tool for organising a media collection stored in a dated
directory hierarchy (`yyyy/mm/dd` or `yyyy-mm-dd`).  It can:

- **Import** (`--load`) new media into the correct dated subdirectory
- **Deduplicate** (`--dupes`) files with identical content
- **Validate** (`--validate`) that each file lives in the directory that
  matches its own date

---

## Quick start

```bash
# Report duplicates (no changes made)
./deduper.py --dupes --photos /mnt/photos --videos /mnt/videos

# Remove duplicates
./deduper.py --dupes --fix --photos /mnt/photos --videos /mnt/videos

# Import from an inbox
./deduper.py --load --loadpath /inbox --photos /mnt/photos --videos /mnt/videos

# Validate placement (report only)
./deduper.py --validate --photos /mnt/photos --videos /mnt/videos

# Validate and fix, with a config file
./deduper.py --validate --fix --config ./photos.ini

# Diagnostic: dump raw EXIF for specific files
./deduper.py --exif /path/to/photo.jpg /path/to/video.mp4

# Diagnostic: show date determination for specific files
./deduper.py --getDate /path/to/file.jpg

# Diagnostic: EXIF dump + date determination together
./deduper.py --exif --getDate /path/to/file.jpg

# Diagnostic: compare Pillow vs ExifTool date for specific files
./deduper.py --compareExif /path/to/file.jpg
```

---

## Installation

Requires Python ≥ 3.10.

```bash
pip install pillow pyexiftool   # Runtime dependencies
```

[ExifTool](https://exiftool.org/) must be installed and on `$PATH` for video
date extraction.

---

## Configuration file

An INI file can supply default values for all paths and flags.  Command-line
arguments always take precedence.

```ini
[LOCATIONS]
photos   = /mnt/nas/photos
videos   = /mnt/nas/videos
misdated = /mnt/nas/misc
import   = /inbox

[GLOBALS]
DEFAULT_SEP   = /       ; Use yyyy/mm/dd directories (default).  Set to - for yyyy-mm-dd.
DRYRUN        = false
DEBUG         = false
POOL_SIZE     = 4       ; Worker processes for parallel scan
POOL_CHUNKSIZE = 20

[ACTIONS]
; Pre-select actions so you don't need to specify them every time
import   = false
dupes    = false
validate = true
```

---

## How dates are determined

The tool looks for a date in this order:

| Priority | Media type | Method |
|----------|------------|--------|
| 1 | Image (JPEG, PNG, RAW, …) | EXIF tags via Pillow — reads main IFD, Exif SubIFD (0x8769), and GPS IFD (0x8825); field order: DateTimeOriginal → GPS DateStamp → CreateDate → DateTime |
| 2 | Image (fallback) | Date pattern in filename (`IMG_2023-08-14.jpg`) |
| 3 | Image (fallback) | Date pattern in directory path (`.../2023/08/14/...`) |
| 1 | Video (MP4, AVI, MOV, …) | ExifTool (EXIF:DateTimeOriginal → QuickTime:CreateDate → …) |
| 2 | Video (fallback) | MediaInfo — reads container metadata (`Encoded_Date`, `Tagged_Date`, …) when installed |
| 3 | Video (fallback) | Date pattern in filename |
| 4 | Video (fallback) | Date pattern in directory path |

A date pattern is any `yyyy`, `mm`, `dd` triple separated by `-`, `.`, `/`,
or nothing, anywhere in the string — e.g. all of these are recognised:

```
IMG_2023-08-14.jpg
20230814_holiday.mp4
backup.2022.06.15.tar
/photos/2023/08/14/img.jpg
```

---

## Duplicate detection

Files are considered candidates when they have **identical sizes**.  Candidates
are then compared by a partial content hash (MD5 of four 1 KiB samples: start,
one-third, two-thirds, and end of the file).  Exact duplicates share both size
and hash.

When duplicates are found, the tool **keeps the first file** in the sorted
group (sorted by: not-deleted → not-in-originals-dir → not-misdated → sequence
number → filename) and removes the rest.

> **Note:** The partial hash trades a small probability of false-positives for
> speed on large collections.  For maximum safety, review the duplicate report
> (`--dupes` without `--fix`) before running with `--fix`.

---

## Collision handling

When importing or validating, if a file with the same name already exists at
the destination:

1. If the content is **identical** → the source file is deleted (it's a dupe).
2. If the content is **different** and `--misdated` is set → retry at the
   misdated overflow directory.
3. If the content is **different** and `--force` is set → rename the source
   file with a `_NNN` suffix (`photo.jpg` → `photo_001.jpg`).
4. Otherwise → skip and report.

---

## Command-line reference

```
usage: deduper [-h] [--photos PATH] [--videos PATH] [--misdated PATH]
               [--loadpath PATH] [--load] [--dupes] [--validate]
               [--exif] [--getDate] [--fix] [--force] [--parallel]
               [--debug] [--dryrun] [--compareExif] [--config PATH]
               [--report EMAIL] [--exiftool PATH]
               [--exclude-dir NAME] [--exclude-re PATTERN]
               [PATH ...]

locations:
  --photos PATH     Root for dated images
  --videos PATH     Root for dated videos
  --misdated PATH   Overflow directory for name-clash relocations
  --loadpath PATH   Source tree for --load

actions:
  --load            Import media from --loadpath
  --dupes           Detect duplicate media
  --validate        Detect misplaced media

diagnostic (supply file paths as positional arguments):
  --exif            Dump raw ExifTool metadata for given paths
  --getDate         Show EXIF / filename / directory / assigned date breakdown
  --compareExif     Report where Pillow and ExifTool disagree on date
  PATH ...          One or more file paths (activates diagnostic mode)

options:
  --fix             Actually perform deletions / relocations (default: report only)
  --force           Rename files on collision rather than skipping
  --parallel        Scan in parallel; prefetches image EXIF via Pool and
                    video EXIF via ExifTool batch mode
  --debug           Enable verbose logging (implies --dryrun)
  --dryrun          Simulate without making changes
  --config PATH     INI config file
  --report EMAIL    Email the report (future enhancement)
  --exiftool PATH   Full path to the exiftool binary (default: search PATH)
  --exclude-dir NAME     Directory name to prune from tree walks (repeatable)
  --exclude-re PATTERN   Regex matched against filenames to skip
```

---

## Project structure

```
deduper/
├── __init__.py
├── cli.py          Command-line entry point; EXIF dump commands
├── config.py       Config dataclass; built from args or INI file
├── dates.py        Pure date parsing/formatting functions
├── exif.py         EXIF backends: ImageExifReader (Pillow), ExifToolReader, MediaInfoReader
├── filesystem.py   DirCache, safe_filename, walk_media tree walker
├── hashing.py      Partial file hashing for duplicate detection
├── media.py        MediaFile, ImageFile, VideoFile, MediaRegistry
├── scanner.py      MediaScanner orchestration; Report accumulator
└── tests/
    ├── test_cli.py
    ├── test_config.py
    ├── test_dates.py
    ├── test_exif.py
    ├── test_filesystem.py
    ├── test_hashing.py
    ├── test_media.py
    └── test_scanner.py
```

---

## Running the tests

No external test runner is required — the standard library `unittest` is used
throughout.

```bash
# Run all tests
python -m unittest discover -s deduper/tests -p "test_*.py" -v

# Run a single module
python -m unittest deduper.tests.test_dates -v
```

---

## Design notes

### What changed from v3

| Issue | Fix |
|---|---|
| Global `_RUNTIME` singleton | `Config` dataclass injected explicitly |
| Class-level mutable `locations` dict on `Runtime` | Instance attribute on `Config` |
| Class-level `_testDate` on `ImageInfoMixin` | `_UNSET` sentinel per-instance |
| Class-level `cache` on `MediaManager` | Instance attribute on `MediaRegistry` |
| `Runtime._config()` method shadows `self._config` attribute | Renamed to `_apply_config_file()` |
| `doMisplaced` uses bare `force` (NameError) | Uses `self._cfg.do_force` |
| `_ensureHashed` uses undefined `POOL_SIZE`/`POOL_CHUNKSIZE` | Uses `config.pool_size` |
| `imageInfoCompare` calls `ImageInfo.factory()` (doesn't exist) | Uses `MediaRegistry` |
| `RuntimeError('msg %r', obj)` (wrong signature) | f-string used |
| `tree_walk` references undefined `stem` in closure | Removed |
| Partial hash (3 samples) misses tail differences | 4-sample strategy |
| `processMedia` validate logic inverted | Fixed: collects files where `validate()` returns a problem |
| `safeFilename` starts at current sequence number, not next | Starts at `current + 1` |
| Pool never closed in `imageInfos` | Context manager used |
| `BadDirs` set intersection does exact match, but list contains `'@eaDir$'` with stray `$` | Corrected to `'@eaDir'` |
| `__hash__` / `__eq__` contract violation | Both based on same key |
| `logger.info("Importing {len(what)} files")` missing f-prefix | Fixed |
| `ComparisonMixin.__cmp__` calls Python 2 `cmp()` | Replaced by `@total_ordering` |
| Date regex `\b` fails after `_` (e.g. `IMG_2023-08-14.jpg`) | Replaced with `(?<!\d)` / `(?!\d)` |
| `ExifFromImage` silently swallows all exceptions | Logs at DEBUG level |
| Email reporting stub (`if to: pass`) | Logs a warning; documented as future work |
