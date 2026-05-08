"""
filesystem.py — Filesystem helpers used throughout deduper.

All functions are stateless and independently testable.  Directory-creation
caching (the original ``mkdir.made`` function-attribute trick) is replaced by
an explicit ``DirCache`` class so it can be reset between test runs.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Generator, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

# ── Patterns ───────────────────────────────────────────────────────────────

# Default exclusion constants — used when no config-level overrides are given.
# These are also imported directly by tests, so the names are stable exports.
_DEFAULT_BAD_FILES_PATTERN: str = (
    r"(?:\.ini$|Thumbs\.db$|\.Picasa3Temp|\b@SynoEAStream\b|\b@eaDir\b)"
)
_BAD_FILES_RE = re.compile(_DEFAULT_BAD_FILES_PATTERN, re.IGNORECASE)

# Note: the original list contained '@eaDir$' with a stray regex '$' — this
# is corrected to the literal directory name '@eaDir'.
BAD_DIRS: frozenset[str] = frozenset(["@SynoEAStream", "@eaDir", "Misc"])

_SEQ_SUFFIX_RE = re.compile(r"[\-_]+(\d{2,3})\.\w+$")
_SEQ_STEM_RE = re.compile(r"^(.+?)[\-_]+(\d{2,3})$")


# ── Directory creation ─────────────────────────────────────────────────────

class DirCache:
    """Memoised ``makedirs`` — avoids redundant ``stat`` calls on hot paths."""

    def __init__(self) -> None:
        self._made: set[Path] = set()

    def ensure(self, target: Path) -> None:
        """Create *target* (and all parents) if it does not already exist."""
        if target in self._made:
            return
        target.mkdir(parents=True, exist_ok=True)
        self._made.add(target)

    def reset(self) -> None:
        """Clear the cache (used in tests)."""
        self._made.clear()


# ── Safe filename generation ───────────────────────────────────────────────

def safe_filename(directory: Path, filename: str) -> str:
    """Return a filename that does not collide with any existing file in
    *directory*, appending or incrementing a ``_NNN`` suffix as needed.

    Examples::

        img123.jpg       →  img123_001.jpg   (if img123.jpg already exists)
        img123_001.jpg   →  img123_002.jpg   (if img123_001.jpg already exists)

    :raises RuntimeError: If no free slot is found within 999 attempts.
    """
    if "." not in filename:
        raise ValueError(f"Filename has no extension: {filename!r}")

    stem, ext = filename.rsplit(".", 1)
    seq_match = _SEQ_STEM_RE.match(stem)
    if seq_match:
        base, current = seq_match.groups()
        i = int(current) + 1   # Start *after* the existing sequence number
    else:
        base = stem
        i = 1

    for i in range(i, 1000):
        candidate = f"{base}_{i:03d}.{ext}"
        if not (directory / candidate).exists():
            return candidate

    raise RuntimeError(  # pragma: no cover
        f"Could not find a free filename for {filename!r} in {directory}"
    )


# ── Tree walk ─────────────────────────────────────────────────────────────

def walk_media(
    roots: Iterable[Path | str],
    exclude_dirs: Optional[frozenset] = None,
    exclude_files_re: Optional[str] = None,
) -> Iterator[tuple[Path, str]]:
    """Yield ``(directory_path, filename)`` for every acceptable media file
    found under each root in *roots*.

    :param roots:            One or more root directories to scan.
    :param exclude_dirs:     Set of directory names to prune.  Defaults to
                             ``BAD_DIRS`` when ``None``.
    :param exclude_files_re: Regex pattern matched against filenames to skip
                             (case-insensitive).  Defaults to the built-in
                             pattern when ``None``.
    """
    _dirs = BAD_DIRS if exclude_dirs is None else exclude_dirs
    _files_re = (
        _BAD_FILES_RE if exclude_files_re is None
        else re.compile(exclude_files_re, re.IGNORECASE)
    )
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            logger.warning("Scan root does not exist or is not a directory: %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune in-place so os.walk skips the subtrees
            dirnames[:] = [d for d in dirnames if d not in _dirs]
            dp = Path(dirpath)
            for fn in filenames:
                if isinstance(fn, str) and not _files_re.search(fn):
                    yield dp, fn
