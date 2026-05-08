"""Tests for deduper.filesystem — directory helpers and tree walking."""

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.filesystem import DirCache, safe_filename, walk_media, BAD_DIRS


class TestDirCache(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = DirCache()

    def test_creates_directory(self):
        target = self.tmp / "new" / "nested" / "dir"
        self.assertFalse(target.exists())
        self.cache.ensure(target)
        self.assertTrue(target.is_dir())

    def test_does_not_raise_if_already_exists(self):
        target = self.tmp / "exists"
        target.mkdir()
        self.cache.ensure(target)  # Should not raise

    def test_second_call_skips_stat(self):
        target = self.tmp / "cached"
        self.cache.ensure(target)
        # Delete the dir — second call should not raise because it's cached
        target.rmdir()
        self.cache.ensure(target)  # Cached — no mkdir attempted

    def test_reset_clears_cache(self):
        target = self.tmp / "will_be_reset"
        self.cache.ensure(target)
        self.assertIn(target, self.cache._made)
        self.cache.reset()
        self.assertEqual(len(self.cache._made), 0)


class TestSafeFilename(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_no_collision_returns_001(self):
        result = safe_filename(self.tmp, "photo.jpg")
        self.assertEqual(result, "photo_001.jpg")

    def test_collision_increments(self):
        (self.tmp / "photo_001.jpg").touch()
        result = safe_filename(self.tmp, "photo.jpg")
        self.assertEqual(result, "photo_002.jpg")

    def test_already_sequenced_increments_from_next(self):
        # photo_003.jpg exists — should return _004
        (self.tmp / "photo_004.jpg").touch()
        result = safe_filename(self.tmp, "photo_003.jpg")
        # Starts from 004 (003+1), but _004 exists, so should be _005
        self.assertEqual(result, "photo_005.jpg")

    def test_free_slot_found(self):
        # Create _001 and _002 but not _003
        (self.tmp / "img_001.jpg").touch()
        (self.tmp / "img_002.jpg").touch()
        result = safe_filename(self.tmp, "img.jpg")
        self.assertEqual(result, "img_003.jpg")

    def test_no_extension_raises(self):
        with self.assertRaises(ValueError):
            safe_filename(self.tmp, "noextension")

    def test_forward_slash_in_filename_raises(self):
        with self.assertRaises(ValueError):
            safe_filename(self.tmp, "subdir/photo.jpg")

    def test_backslash_in_filename_raises(self):
        with self.assertRaises(ValueError):
            safe_filename(self.tmp, "subdir\\photo.jpg")

    def test_preserves_extension_case(self):
        result = safe_filename(self.tmp, "photo.JPG")
        self.assertIn(".JPG", result)


class TestWalkMedia(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def _make(self, *rel_parts) -> Path:
        p = self.tmp.joinpath(*rel_parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p

    def test_finds_files_recursively(self):
        self._make("2023", "01", "01", "photo.jpg")
        self._make("2023", "02", "14", "video.mp4")
        results = list(walk_media([self.tmp]))
        filenames = [fn for _, fn in results]
        self.assertIn("photo.jpg", filenames)
        self.assertIn("video.mp4", filenames)

    def test_skips_thumbs_db(self):
        self._make("Thumbs.db")
        self._make("real.jpg")
        results = list(walk_media([self.tmp]))
        filenames = [fn for _, fn in results]
        self.assertNotIn("Thumbs.db", filenames)
        self.assertIn("real.jpg", filenames)

    def test_skips_ini_files(self):
        self._make("settings.ini")
        results = list(walk_media([self.tmp]))
        filenames = [fn for _, fn in results]
        self.assertNotIn("settings.ini", filenames)

    def test_skips_bad_directories(self):
        for bad_dir in BAD_DIRS:
            self._make(bad_dir, "hidden.jpg")
        self._make("real", "visible.jpg")
        results = list(walk_media([self.tmp]))
        filenames = [fn for _, fn in results]
        self.assertIn("visible.jpg", filenames)
        self.assertNotIn("hidden.jpg", filenames)

    def test_missing_root_does_not_raise(self):
        missing = self.tmp / "does_not_exist"
        results = list(walk_media([missing]))
        self.assertEqual(results, [])

    def test_multiple_roots(self):
        root_a = self.tmp / "a"
        root_b = self.tmp / "b"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "one.jpg").touch()
        (root_b / "two.jpg").touch()
        results = list(walk_media([root_a, root_b]))
        filenames = [fn for _, fn in results]
        self.assertIn("one.jpg", filenames)
        self.assertIn("two.jpg", filenames)

    def test_returns_path_objects(self):
        self._make("sub", "file.jpg")
        for dirpath, filename in walk_media([self.tmp]):
            self.assertIsInstance(dirpath, Path)
            self.assertIsInstance(filename, str)

    def test_empty_directory(self):
        results = list(walk_media([self.tmp]))
        self.assertEqual(results, [])

    def test_custom_exclude_dirs(self):
        self._make("MyArchive", "hidden.jpg")
        self._make("real", "visible.jpg")
        results = list(walk_media([self.tmp], exclude_dirs=frozenset(["MyArchive"])))
        filenames = [fn for _, fn in results]
        self.assertIn("visible.jpg", filenames)
        self.assertNotIn("hidden.jpg", filenames)

    def test_custom_exclude_re(self):
        self._make("keep.jpg")
        self._make("skip.bak")
        results = list(walk_media([self.tmp], exclude_files_re=r"\.bak$"))
        filenames = [fn for _, fn in results]
        self.assertIn("keep.jpg", filenames)
        self.assertNotIn("skip.bak", filenames)

    def test_default_exclusions_still_apply_without_args(self):
        self._make("@eaDir", "syno_meta.jpg")
        self._make("real.jpg")
        results = list(walk_media([self.tmp]))
        filenames = [fn for _, fn in results]
        self.assertIn("real.jpg", filenames)
        self.assertNotIn("syno_meta.jpg", filenames)


class TestDirCacheMkdirFailure(unittest.TestCase):
    """Regression tests for bug: DirCache.ensure() adds to _made before
    mkdir completes, poisoning the cache on failure."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = DirCache()

    def test_failed_mkdir_does_not_poison_cache(self):
        """A failed mkdir must NOT mark the path as done in _made.

        Bug: _made.add(target) is called before mkdir(), so an OSError leaves
        the path cached as 'created' even though the directory was never made.
        Subsequent calls silently return without retrying.
        """
        target = self.tmp / "will_not_be_created"
        with patch.object(Path, 'mkdir', side_effect=OSError("simulated disk full")):
            with self.assertRaises(OSError):
                self.cache.ensure(target)

        # Directory was never created — must not be in _made.
        # Before fix: target IS in _made → this assertion FAILS.
        self.assertNotIn(target, self.cache._made)

    def test_retry_after_transient_failure_creates_directory(self):
        """A second call to ensure() after a transient failure must retry mkdir.

        Bug: if the path is already in _made (due to the poisoning above),
        ensure() returns immediately on the second call, leaving the directory
        uncreated and causing the subsequent file-move to fail with a confusing
        error instead of the true cause.
        """
        target = self.tmp / "retry_target"
        original_mkdir = Path.mkdir
        call_counts = [0]

        def flaky_mkdir(path_self, **kwargs):
            call_counts[0] += 1
            if call_counts[0] == 1:
                raise OSError("transient error")
            return original_mkdir(path_self, **kwargs)

        with patch.object(Path, 'mkdir', flaky_mkdir):
            with self.assertRaises(OSError):
                self.cache.ensure(target)
            # Second call — must retry mkdir, not silently return because
            # the (failed) first attempt left target in _made.
            self.cache.ensure(target)

        # Before fix: mkdir is never called a second time; target doesn't exist.
        self.assertTrue(target.is_dir())


if __name__ == "__main__":
    unittest.main()
