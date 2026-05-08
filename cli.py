"""
cli.py — Command-line interface for deduper.

Entry points
------------
``main(argv=None)``
    Called by the ``deduper`` console-script entry point and by ``__main__``.

``build_parser()``
    Returns the ``argparse.ArgumentParser`` (used by tests).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from pprint import pprint
from typing import Optional, Sequence

from .config import Config
from .exif import ExifToolReader
from .media import MediaRegistry
from .scanner import MediaScanner, Report

logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ── Argument parser ────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deduper",
        description=(
            "Identify and remove duplicate media files; relocate misplaced "
            "files in a dated directory hierarchy (yyyy/mm/dd or yyyy-mm-dd)."
        ),
    )
    loc = p.add_argument_group("locations")
    loc.add_argument("--photos", metavar="PATH", help="Root for dated images")
    loc.add_argument("--videos", metavar="PATH", help="Root for dated videos")
    loc.add_argument(
        "--misdated", metavar="PATH",
        help="Overflow directory for name-clash relocations",
    )
    loc.add_argument("--loadpath", metavar="PATH", help="Source tree for --load")

    act = p.add_argument_group("actions (at least one required)")
    act.add_argument("--load", action="store_true", help="Import media from --loadpath")
    act.add_argument("--dupes", action="store_true", help="Detect duplicate media")
    act.add_argument("--validate", action="store_true", help="Detect misplaced media")
    act.add_argument(
        "--exif", action="store_true",
        help="Dump raw EXIF metadata for the given paths",
    )
    act.add_argument(
        "--getDate", action="store_true",
        help="Show date determination (EXIF / filename / assigned) for the given paths",
    )
    p.add_argument(
        "paths", nargs="*", metavar="PATH",
        help=(
            "File paths for diagnostic mode (--exif / --getDate / --compareExif). "
            "When supplied, diagnostic output is produced and no scan is run."
        ),
    )

    opt = p.add_argument_group("options")
    opt.add_argument(
        "--fix", action="store_true",
        help="Actually perform deletions / relocations (default: report only)",
    )
    opt.add_argument(
        "--force", action="store_true",
        help="Rename files on collision rather than skipping",
    )
    opt.add_argument("--parallel", action="store_true", help="Scan in parallel")
    opt.add_argument(
        "--debug", action="store_true",
        help="Enable verbose logging and imply --dryrun",
    )
    opt.add_argument("--dryrun", action="store_true", help="Simulate without changes")
    opt.add_argument(
        "--compareExif", action="store_true",
        help="Report files where Pillow and ExifTool disagree on date",
    )
    opt.add_argument("--config", metavar="PATH", help="INI config file")
    opt.add_argument("--report", metavar="EMAIL", help="Email the report (future)")
    opt.add_argument(
        "--exiftool", metavar="PATH",
        help="Full path to the exiftool binary (default: search PATH)",
    )
    opt.add_argument(
        "--exclude-dir", metavar="NAME", action="append", dest="exclude_dir",
        help=(
            "Directory name to prune from tree walks (repeatable). "
            "When given, replaces the default set entirely."
        ),
    )
    opt.add_argument(
        "--exclude-re", metavar="PATTERN", dest="exclude_re",
        help=(
            "Regex matched against filenames to skip (case-insensitive). "
            "When given, replaces the default pattern entirely."
        ),
    )
    return p


# ── EXIF debug commands ────────────────────────────────────────────────────

def cmd_exif(paths: list[Path], config: Config) -> None:
    """Print raw ExifTool metadata for each path."""
    with ExifToolReader(debug=config.debug, executable=config.exiftool_executable) as reader:
        for path in paths:
            if not path.exists():
                print(f"{path}: file not found")
                continue
            raw = reader.get_raw(path)
            pprint({"file": str(path), "exif": raw})


def cmd_compare_exif(paths: list[Path], config: Config) -> None:
    """Report files where Pillow and ExifTool report different dates."""
    from .exif import ImageExifReader

    img_reader = ImageExifReader(debug=config.debug)
    with ExifToolReader(debug=config.debug, executable=config.exiftool_executable) as et_reader:
        for path in paths:
            if not path.exists():
                print(f"{path}: file not found")
                continue
            d_img = img_reader.get_date(path)
            d_et = et_reader.get_date(path)
            if d_img != d_et:
                print(f"{path}: Pillow={d_img}  ExifTool={d_et}")


def cmd_get_date(paths: list[Path], config: Config) -> None:
    """Show date determination breakdown for each path.

    For each file prints:
    * The EXIF date returned by Pillow (image files) or ExifTool (video files)
    * The date parseable from the filename alone
    * The final date that would be assigned
    """
    from .exif import ImageExifReader
    from .dates import parse_date
    from .media import MediaRegistry, ImageFile, VideoFile

    img_reader = ImageExifReader(debug=config.debug)
    registry = MediaRegistry(config)

    for path in paths:
        path = path.resolve()
        print(f"{path}:")

        if not path.exists():
            print("  file not found")
            continue

        # Obtain the MediaFile from the registry so we reuse the same logic
        mf = registry.get_or_create(path.parent, path.name)
        if mf is None:
            print("  (not a recognised media file)")
            continue

        # EXIF date — use the appropriate reader depending on file type
        if isinstance(mf, ImageFile):
            exif_date = img_reader.get_date(path)
        else:
            # VideoFile — use ExifToolReader (lazy, one file at a time)
            with ExifToolReader(debug=config.debug, executable=config.exiftool_executable) as et:
                exif_date = et.get_date(path)

        # Filename date — parse just the bare filename
        filename_date = parse_date(path.name)
        # Directory date — parse the parent path (e.g. .../2013/09/29/...)
        dir_date = parse_date(str(path.parent))

        # Final assigned date — let the MediaFile compute it the normal way
        final_date = mf.dated

        print(f"  EXIF date      : {exif_date}")
        print(f"  Filename date  : {filename_date}")
        print(f"  Directory date : {dir_date}")
        print(f"  → Assigned     : {final_date}")


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.from_args(args)

    start = time.monotonic()

    # ── Diagnostic mode (paths supplied — exits early) ─────────────────────
    if config.diagnostic_paths:
        shown = False
        if config.do_exif:
            cmd_exif(config.diagnostic_paths, config)
            shown = True
        if config.do_compare_exif:
            if shown:
                print()
            cmd_compare_exif(config.diagnostic_paths, config)
            shown = True
        if config.do_getdate:
            if shown:
                print()
            cmd_get_date(config.diagnostic_paths, config)
            shown = True
        if not shown:
            # Default when paths given with no specific flag: dump raw EXIF
            cmd_exif(config.diagnostic_paths, config)
        return 0

    # ── Validate config ────────────────────────────────────────────────────
    try:
        config.validate_for_action()
    except ValueError as exc:
        parser.error(str(exc))

    # ── Run ────────────────────────────────────────────────────────────────
    report = Report()
    scanner = MediaScanner(config=config, report=report)

    try:
        if config.do_load:
            scanner.run_load()
        if config.do_dupes:
            scanner.run_dupes()
        if config.do_validate:
            scanner.run_validate()
    except KeyboardInterrupt:
        report("Interrupted by user")
        return 1
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return 2

    elapsed = time.monotonic() - start
    report(f"\nCompleted in {int(elapsed)//60}m{int(elapsed)%60:02d}s")
    output = report.send(config.report_to)
    print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
