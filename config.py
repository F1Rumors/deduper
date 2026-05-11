"""
config.py — Runtime configuration for deduper.

Replaces the global _RUNTIME singleton with an explicit, injectable Config
dataclass. All components receive a Config instance rather than reaching into
module-level state, making them independently testable.
"""

from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Config:
    """All runtime settings in one place.  Constructed by ``Config.from_args``
    or ``Config.from_defaults``; injected into every component that needs it.
    """

    # ── Directory locations ────────────────────────────────────────────────
    photos_path: Optional[Path] = None     # Destination for dated images
    videos_path: Optional[Path] = None     # Destination for dated videos
    misdated_path: Optional[Path] = None   # Overflow for name-clash relocations
    import_path: Optional[Path] = None     # Source tree for --load

    # ── Actions ────────────────────────────────────────────────────────────
    do_load: bool = False       # Import media from import_path
    do_dupes: bool = False      # Detect / remove duplicates
    do_validate: bool = False   # Detect / fix misplaced files
    do_prune: bool = False      # Remove empty directories from library trees
    do_force: bool = False      # Rename on collision rather than skip
    do_compare_exif: bool = False  # Report EXIF differences between backends

    # ── Behaviour flags ────────────────────────────────────────────────────
    debug: bool = False
    dryrun: bool = True          # False only when --fix is explicitly passed
    parallel: bool = False

    # ── Parallelism ────────────────────────────────────────────────────────
    pool_size: int = 4
    pool_chunksize: int = 100

    # ── Date formatting ────────────────────────────────────────────────────
    default_sep: str = "/"          # '/' gives yyyy/mm/dd; '-' gives yyyy-mm-dd

    # ── Reporting ─────────────────────────────────────────────────────────
    report_to: Optional[str] = None  # Email address (future)

    # ── ExifTool ──────────────────────────────────────────────────────────
    # Full path to the exiftool binary, or None to rely on PATH.
    # Set via INI [GLOBALS] exiftool_path or CLI --exiftool.
    exiftool_executable: Optional[str] = None

    # ── Watchdog ──────────────────────────────────────────────────────────
    # Maximum seconds allowed for a single NAS file read (EXIF, hash, classify).
    # If any per-file operation exceeds this the process hard-exits via os._exit.
    hash_timeout: float = 10.0
    # Extensions that get a longer classify timeout (e.g. large TIFFs with
    # IFD0 at end of file requiring a slow NAS seek).
    slow_extensions: frozenset = frozenset({"tif", "tiff"})
    slow_ext_timeout: float = 120.0

    # ── Exclusions ────────────────────────────────────────────────────────
    # Directory names pruned from tree walks.  Configurable via INI
    # ``exclude_dirs`` or CLI ``--exclude-dir``.
    exclude_dirs: frozenset = frozenset(["@SynoEAStream", "@eaDir", "Misc", ".picasaoriginals"])
    # Regex matched against filenames to skip.  Configurable via INI
    # ``exclude_re`` or CLI ``--exclude-re``.
    exclude_files_re: str = (
        r"(?:\.ini$|Thumbs\.db$|\.Picasa3Temp|\b@SynoEAStream\b|\b@eaDir\b)"
    )

    # ── Diagnostic mode ────────────────────────────────────────────────────
    # Paths supplied as positional arguments; non-empty activates diagnostic mode.
    diagnostic_paths: list[Path] = field(default_factory=list)
    # Flags controlling what diagnostic output to produce
    do_exif: bool = False      # dump raw EXIF
    do_getdate: bool = False   # show date determination

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_args(cls, args) -> "Config":
        """Build a Config from a parsed ``argparse.Namespace``.

        Config-file values are loaded first; command-line arguments override
        them, matching the documented behaviour of the original tool.
        """
        cfg = cls()
        if args.config:
            cfg._apply_config_file(args.config)

        # Locations — command line overrides config file
        if args.photos:
            cfg.photos_path = Path(args.photos)
        if args.videos:
            cfg.videos_path = Path(args.videos)
        if args.misdated:
            cfg.misdated_path = Path(args.misdated)
        if args.loadpath:
            cfg.import_path = Path(args.loadpath)

        # Debug/dryrun — command line only (config file variant applied above)
        if args.debug:
            cfg.debug = True
            cfg.dryrun = True  # safe default for debug without --fix
            logging.getLogger().setLevel(logging.DEBUG)
            logging.getLogger("PIL").setLevel(logging.INFO)
        if args.dryrun:
            cfg.dryrun = True
        # --fix is applied last so it wins over --debug and config-file DRYRUN.
        # This lets you run "deduper --debug --fix" to get verbose output while
        # actually performing changes.
        if args.fix:
            cfg.dryrun = False

        # Actions — BooleanOptionalAction gives True/False/None.
        # None means "not specified on CLI" → keep the config-file value.
        # --loadpath always implies --load regardless of --no-load.
        if args.loadpath:
            cfg.do_load = True
        elif args.load is not None:
            cfg.do_load = bool(args.load)
        if args.dupes is not None:
            cfg.do_dupes = bool(args.dupes)
        if args.validate is not None:
            cfg.do_validate = bool(args.validate)
        if args.prune is not None:
            cfg.do_prune = bool(args.prune)
        cfg.do_force = args.force
        cfg.do_compare_exif = args.compareExif
        cfg.do_exif = args.exif
        cfg.do_getdate = args.getDate
        cfg.parallel = args.parallel
        cfg.report_to = args.report
        cfg.diagnostic_paths = [Path(p) for p in args.paths] if args.paths else []

        if args.exiftool:
            if not Path(args.exiftool).is_file():
                raise ValueError(f"--exiftool: not a regular file: {args.exiftool!r}")
            cfg.exiftool_executable = args.exiftool

        # Exclusions — CLI completely replaces the config-file / default value
        if args.exclude_dir:
            cfg.exclude_dirs = frozenset(args.exclude_dir)
        if args.exclude_re:
            try:
                re.compile(args.exclude_re)
            except re.error as exc:
                raise ValueError(f"--exclude-re: invalid regex: {exc}") from exc
            cfg.exclude_files_re = args.exclude_re

        return cfg

    @classmethod
    def from_defaults(cls, **overrides) -> "Config":
        """Convenience factory for tests — create a minimal Config."""
        return cls(**overrides)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_config_file(self, path: str) -> None:
        parser = configparser.RawConfigParser()
        parser.read(path)

        if "LOCATIONS" in parser:
            loc = parser["LOCATIONS"]
            if "photos" in loc:
                self.photos_path = Path(loc["photos"])
            if "videos" in loc:
                self.videos_path = Path(loc["videos"])
            if "misdated" in loc:
                self.misdated_path = Path(loc["misdated"])
            if "import" in loc:
                self.import_path = Path(loc["import"])
            if "exclude_dirs" in loc:
                self.exclude_dirs = frozenset(
                    d.strip() for d in loc["exclude_dirs"].split(",") if d.strip()
                )
            if "exclude_re" in loc:
                self.exclude_files_re = loc["exclude_re"]

        if "GLOBALS" in parser:
            g = parser["GLOBALS"]
            if "DEBUG" in g:
                self.debug = g.getboolean("DEBUG")
            if "DRYRUN" in g:
                self.dryrun = g.getboolean("DRYRUN")
            if "DEFAULT_SEP" in g:
                self.default_sep = g["DEFAULT_SEP"]
            if "POOL_SIZE" in g:
                self.pool_size = g.getint("POOL_SIZE")
            if "POOL_CHUNKSIZE" in g:
                self.pool_chunksize = g.getint("POOL_CHUNKSIZE")
            if "exiftool_path" in g:
                self.exiftool_executable = g["exiftool_path"]
            if "hash_timeout" in g:
                self.hash_timeout = g.getfloat("hash_timeout")
            if "slow_extensions" in g:
                self.slow_extensions = frozenset(
                    e.strip().lstrip(".").lower()
                    for e in g["slow_extensions"].split(",")
                    if e.strip()
                )
            if "slow_ext_timeout" in g:
                self.slow_ext_timeout = g.getfloat("slow_ext_timeout")

        if "ACTIONS" in parser:
            a = parser["ACTIONS"]
            if "import" in a:
                self.do_load = a.getboolean("import")
            if "dupes" in a:
                self.do_dupes = a.getboolean("dupes")
            if "validate" in a:
                self.do_validate = a.getboolean("validate")
            if "prune" in a:
                self.do_prune = a.getboolean("prune")

    # ------------------------------------------------------------------
    # Convenience accessors used throughout the codebase
    # ------------------------------------------------------------------

    def date_str(self, dt) -> str:
        """Format a ``datetime.date`` as a directory-safe string using
        ``default_sep`` as the separator between year, month and day.

        Returns ``'0000/00/00'`` (or equivalent) when *dt* is ``None``.
        """
        sep = self.default_sep
        if dt is None:
            return f"0000{sep}00{sep}00"
        return f"{dt.year:04d}{sep}{dt.month:02d}{sep}{dt.day:02d}"

    def validate_for_action(self) -> None:
        """Raise ``ValueError`` if the configuration cannot run any action."""
        if not (self.do_load or self.do_dupes or self.do_validate or self.do_prune or self.diagnostic_paths):
            raise ValueError(
                "No action specified. Use --load, --dupes, --validate, or supply file paths."
            )
        # Overlap check is scoped to do_load intentionally: validate/dupes only
        # scan photos_path / videos_path (not import_path), so a misconfigured
        # import_path that sits inside the library is harmless for those actions.
        # If import_path were ever used outside of load, this guard must be widened.
        if self.do_load and self.import_path:
            for lib_path in filter(None, [self.photos_path, self.videos_path]):
                if (self.import_path.is_relative_to(lib_path)
                        or lib_path.is_relative_to(self.import_path)):
                    raise ValueError(
                        f"import_path {str(self.import_path)!r} overlaps with library path "
                        f"{str(lib_path)!r} — use a separate inbox directory outside the library"
                    )
