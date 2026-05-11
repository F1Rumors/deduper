"""
hashing.py — File content hashing for duplicate detection.

Strategy
--------
For small files (< 4 KiB) the entire file is read.  For larger files three
1 KiB samples are taken at the start, one-third, and two-thirds of the file,
plus a final 1 KiB sample at the end.  The four-sample strategy reduces false
positives versus the original three-sample approach for files that share
headers (e.g. same-camera JPEG files with identical SOI/APP0 markers).

The hash function is MD5 — not cryptographically secure, but fast and
sufficient for duplicate detection on a trusted local filesystem.

Design note — partial hash false-positive risk (intentional trade-off)
----------------------------------------------------------------------
Two files that differ *only* in the regions between the four sample points
will produce identical hashes and be treated as duplicates.  For a personal
photo library the probability is negligible: camera-produced files share
headers/footers but diverge in the image payload, which spans the sampled
positions.  The trade-off (speed vs. certainty) is accepted.  If this
ever becomes a concern, a ``--full-hash`` flag that reads the complete file
for matched pairs before deletion would eliminate false positives entirely.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_SMALL_FILE_THRESHOLD = 4096   # bytes
_SAMPLE_SIZE = 1024            # bytes per sample chunk


def hash_file(path: Path, size: int) -> str:
    """Return a hex-digest string that identifies the content of *path*.

    :param path:  File to hash.
    :param size:  Known file size in bytes (avoids a second ``stat`` call).
    :raises OSError: If the file cannot be opened.
    """
    with open(path, "rb") as fh:
        if size < _SMALL_FILE_THRESHOLD:
            data = fh.read()
        else:
            data = fh.read(_SAMPLE_SIZE)

            fh.seek(size // 3)
            data += fh.read(_SAMPLE_SIZE)

            fh.seek(2 * size // 3)
            data += fh.read(_SAMPLE_SIZE)

            # Tail sample — catches files that diverge only at the end
            fh.seek(max(0, size - _SAMPLE_SIZE))
            data += fh.read(_SAMPLE_SIZE)

    return hashlib.md5(data).hexdigest()
