"""Tests for deduper.hashing — file content hashing."""

import contextlib
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.hashing import hash_file, _SMALL_FILE_THRESHOLD, _SAMPLE_SIZE


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


class TestHashFile(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    # ── Small files ────────────────────────────────────────────────────────

    def test_small_file_full_read(self):
        data = b"hello world"
        f = _write(self.tmp / "small.dat", data)
        expected = hashlib.md5(data).hexdigest()
        self.assertEqual(hash_file(f, len(data)), expected)

    def test_empty_file(self):
        f = _write(self.tmp / "empty.dat", b"")
        result = hash_file(f, 0)
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 32)  # MD5 hex length

    def test_small_file_boundary(self):
        # File exactly at threshold — should use full read path
        data = b"x" * _SMALL_FILE_THRESHOLD
        f = _write(self.tmp / "boundary.dat", data)
        result = hash_file(f, len(data))
        self.assertIsInstance(result, str)

    # ── Large files ────────────────────────────────────────────────────────

    def test_large_file_returns_string(self):
        data = b"A" * (_SMALL_FILE_THRESHOLD * 10)
        f = _write(self.tmp / "large.dat", data)
        result = hash_file(f, len(data))
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 32)

    def test_identical_large_files_same_hash(self):
        data = b"B" * 50_000
        f1 = _write(self.tmp / "a.dat", data)
        f2 = _write(self.tmp / "b.dat", data)
        self.assertEqual(hash_file(f1, len(data)), hash_file(f2, len(data)))

    def test_different_content_different_hash(self):
        data1 = b"A" * 50_000
        data2 = b"B" * 50_000
        f1 = _write(self.tmp / "a.dat", data1)
        f2 = _write(self.tmp / "b.dat", data2)
        self.assertNotEqual(hash_file(f1, len(data1)), hash_file(f2, len(data2)))

    def test_tail_difference_detected(self):
        """Files that differ only in their last bytes must have different hashes."""
        size = _SMALL_FILE_THRESHOLD * 20
        base = b"Z" * size
        f1 = _write(self.tmp / "a.dat", base)
        # Modify the very last byte
        modified = base[:-1] + b"X"
        f2 = _write(self.tmp / "b.dat", modified)
        self.assertNotEqual(hash_file(f1, size), hash_file(f2, size))

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            hash_file(self.tmp / "nonexistent.dat", 0)

    def test_deterministic(self):
        data = b"deterministic" * 1000
        f = _write(self.tmp / "det.dat", data)
        h1 = hash_file(f, len(data))
        h2 = hash_file(f, len(data))
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
