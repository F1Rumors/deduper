"""Tests for deduper.media — domain model for media files.

All tests use real temporary files so that stat/hash operations work
correctly, but EXIF readers are stubbed out so no Pillow or exiftool
interaction is needed.
"""

import contextlib
import errno
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.config import Config
from deduper.filesystem import DirCache
from deduper.media import (
    ImageFile,
    VideoFile,
    MediaFile,
    MediaRegistry,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    IGNORED_EXTENSIONS,
    _UNSET,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def make_cfg(**kwargs) -> Config:
    return Config.from_defaults(**kwargs)


def make_image(tmp: Path, filename: str, content: bytes = b"x" * 100, **cfg_kw) -> ImageFile:
    cfg = make_cfg(**cfg_kw)
    path = tmp / filename
    path.write_bytes(content)
    reader = MagicMock()
    reader.get_date.return_value = None
    return ImageFile(tmp, filename, cfg, exif_reader=reader)


def make_image_with_date(tmp: Path, filename: str, d: date, **cfg_kw) -> ImageFile:
    cfg = make_cfg(**cfg_kw)
    (tmp / filename).write_bytes(b"data")
    reader = MagicMock()
    reader.get_date.return_value = d
    return ImageFile(tmp, filename, cfg, exif_reader=reader)


# ── ImageFile — basic properties ───────────────────────────────────────────

class TestMediaFileProperties(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_directory_property(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertEqual(mf.directory, self.tmp.resolve())

    def test_filename_property(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertEqual(mf.filename, "photo.jpg")

    def test_path_property(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertEqual(mf.path, (self.tmp / "photo.jpg").resolve())

    def test_size_property(self):
        mf = make_image(self.tmp, "photo.jpg", content=b"A" * 512)
        self.assertEqual(mf.size, 512)

    def test_size_cached(self):
        mf = make_image(self.tmp, "photo.jpg")
        _ = mf.size
        _ = mf.size  # Second access — should not stat again

    def test_size_error_returns_minus_one(self):
        mf = make_image(self.tmp, "photo.jpg")
        mf._size = None
        # Simulate missing file
        (self.tmp / "photo.jpg").unlink()
        self.assertEqual(mf.size, -1)
        self.assertTrue(mf.error)

    def test_hash_property(self):
        mf = make_image(self.tmp, "photo.jpg", content=b"B" * 200)
        h = mf.hash
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 32)

    def test_deleted_starts_false(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertFalse(mf.deleted)

    def test_original_false_by_default(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertFalse(mf.original)

    def test_original_true_when_path_contains_originals(self):
        originals_dir = self.tmp / "originals"
        originals_dir.mkdir()
        (originals_dir / "photo.jpg").write_bytes(b"data")
        cfg = make_cfg()
        reader = MagicMock()
        reader.get_date.return_value = None
        mf = ImageFile(originals_dir, "photo.jpg", cfg, exif_reader=reader)
        self.assertTrue(mf.original)

    def test_sequence_zero_when_not_present(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertEqual(mf.sequence, 0)

    def test_sequence_extracted_from_filename(self):
        mf = make_image(self.tmp, "photo_042.jpg")
        self.assertEqual(mf.sequence, 42)

    def test_sequence_three_digit(self):
        mf = make_image(self.tmp, "img-123.png")
        self.assertEqual(mf.sequence, 123)

    def test_is_copy_false_for_plain_file(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertFalse(mf._is_copy)

    def test_is_copy_false_for_sequenced_file(self):
        mf = make_image(self.tmp, "photo_001.jpg")
        self.assertFalse(mf._is_copy)

    def test_is_copy_true_for_windows_copy(self):
        mf = make_image(self.tmp, "photo - Copy.jpg")
        self.assertTrue(mf._is_copy)

    def test_is_copy_true_for_numbered_copy(self):
        mf = make_image(self.tmp, "photo - Copy (2).jpg")
        self.assertTrue(mf._is_copy)

    def test_is_copy_true_for_sequenced_copy(self):
        mf = make_image(self.tmp, "photo__01 - Copy.jpg")
        self.assertTrue(mf._is_copy)

    def test_copy_sorts_after_original(self):
        """A '- Copy' file must sort after its original so the original is kept."""
        a = make_image(self.tmp, "photo.jpg")
        b = make_image(self.tmp, "photo - Copy.jpg", content=b"x" * 100)
        self.assertLess(a, b)

    def test_sequenced_copy_sorts_after_original(self):
        """'__01 - Copy.jpg' (seq=0 but is_copy=True) must sort after '__01.jpg'."""
        a = make_image(self.tmp, "IMG_20190814_084318__01.jpg")
        b = make_image(self.tmp, "IMG_20190814_084318__01 - Copy.jpg", content=b"x" * 100)
        self.assertLess(a, b)


# ── Dated property ─────────────────────────────────────────────────────────

class TestMediaFileDated(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_exif_date_returned_first(self):
        mf = make_image_with_date(self.tmp, "photo.jpg", date(2023, 8, 14))
        self.assertEqual(mf.dated, date(2023, 8, 14))

    def test_filename_date_fallback(self):
        cfg = make_cfg()
        (self.tmp / "IMG_2021-06-15.jpg").write_bytes(b"data")
        reader = MagicMock()
        reader.get_date.return_value = None
        mf = ImageFile(self.tmp, "IMG_2021-06-15.jpg", cfg, exif_reader=reader)
        self.assertEqual(mf.dated, date(2021, 6, 15))

    def test_no_date_returns_none(self):
        mf = make_image(self.tmp, "undated.jpg")
        self.assertIsNone(mf.dated)

    def test_dated_is_cached(self):
        reader = MagicMock()
        reader.get_date.return_value = date(2020, 1, 1)
        cfg = make_cfg()
        (self.tmp / "photo.jpg").write_bytes(b"x")
        mf = ImageFile(self.tmp, "photo.jpg", cfg, exif_reader=reader)
        _ = mf.dated
        _ = mf.dated
        # get_date should only have been called once
        self.assertEqual(reader.get_date.call_count, 1)

    def test_exif_date_takes_priority_over_filename_date(self):
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 1, 1)
        cfg = make_cfg()
        (self.tmp / "IMG_2020-06-15.jpg").write_bytes(b"x")
        mf = ImageFile(self.tmp, "IMG_2020-06-15.jpg", cfg, exif_reader=reader)
        self.assertEqual(mf.dated, date(2023, 1, 1))


# ── Misdated property ──────────────────────────────────────────────────────

class TestMediaFileMisdated(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_not_misdated_when_path_date_matches(self):
        d = date(2023, 8, 14)
        dated_dir = self.tmp / "2023" / "08" / "14"
        dated_dir.mkdir(parents=True)
        (dated_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg()
        mf = ImageFile(dated_dir, "photo.jpg", cfg, exif_reader=reader)
        self.assertFalse(mf.misdated)

    def test_misdated_when_path_date_differs(self):
        d = date(2023, 8, 14)
        wrong_dir = self.tmp / "2020" / "01" / "01"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg()
        mf = ImageFile(wrong_dir, "photo.jpg", cfg, exif_reader=reader)
        self.assertTrue(mf.misdated)

    def test_not_misdated_when_no_path_date(self):
        mf = make_image_with_date(self.tmp, "photo.jpg", date(2023, 1, 1))
        # self.tmp has no date component
        self.assertFalse(mf.misdated)

    def test_not_misdated_when_file_has_no_date(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertFalse(mf.misdated)


# ── _valid_directories ─────────────────────────────────────────────────────

class TestValidDirectories(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def _make_in(self, rel_dir: str, filename: str, d: date) -> ImageFile:
        dirpath = self.photos / rel_dir
        dirpath.mkdir(parents=True, exist_ok=True)
        (dirpath / filename).write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        return ImageFile(dirpath, filename, make_cfg(photos_path=self.photos), exif_reader=reader)

    def test_returns_both_separator_forms(self):
        mf = self._make_in("2023/08/14", "photo.jpg", date(2023, 8, 14))
        valid = mf._valid_directories(self.photos)
        self.assertIn((self.photos / "2023" / "08" / "14").resolve(), valid)
        self.assertIn((self.photos / "2023-08-14").resolve(), valid)

    def test_exactly_two_entries(self):
        mf = self._make_in("2023/08/14", "photo.jpg", date(2023, 8, 14))
        self.assertEqual(len(mf._valid_directories(self.photos)), 2)


# ── Validate ───────────────────────────────────────────────────────────────

class TestMediaFileValidate(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_returns_none_when_correctly_placed(self):
        photos = self.tmp / "photos"
        d = date(2023, 8, 14)
        correct_dir = photos / "2023" / "08" / "14"
        correct_dir.mkdir(parents=True)
        (correct_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=photos)
        mf = ImageFile(correct_dir, "photo.jpg", cfg, exif_reader=reader)
        self.assertIsNone(mf.validate())

    def test_returns_none_for_slash_dir_when_sep_is_dash(self):
        """File in yyyy/mm/dd must not be flagged when default_sep is '-'."""
        photos = self.tmp / "photos"
        d = date(2023, 8, 14)
        slash_dir = photos / "2023" / "08" / "14"
        slash_dir.mkdir(parents=True)
        (slash_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=photos, default_sep="-")
        mf = ImageFile(slash_dir, "photo.jpg", cfg, exif_reader=reader)
        self.assertIsNone(mf.validate())

    def test_returns_none_for_dash_dir_when_sep_is_slash(self):
        """File in yyyy-mm-dd must not be flagged when default_sep is '/'."""
        photos = self.tmp / "photos"
        d = date(2023, 8, 14)
        dash_dir = photos / "2023-08-14"
        dash_dir.mkdir(parents=True)
        (dash_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=photos, default_sep="/")
        mf = ImageFile(dash_dir, "photo.jpg", cfg, exif_reader=reader)
        self.assertIsNone(mf.validate())

    def test_returns_none_for_deleted_file(self):
        """Deleted files should never be reported as misplaced."""
        photos = self.tmp / "photos"
        wrong_dir = self.tmp / "somewhere_else"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=photos)
        mf = ImageFile(wrong_dir, "photo.jpg", cfg, exif_reader=reader)
        mf._deleted = True
        self.assertIsNone(mf.validate())

    def test_returns_message_when_misplaced(self):
        photos = self.tmp / "photos"
        d = date(2023, 8, 14)
        wrong_dir = self.tmp / "somewhere_else"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=photos)
        mf = ImageFile(wrong_dir, "photo.jpg", cfg, exif_reader=reader)
        result = mf.validate()
        self.assertIsNotNone(result)
        self.assertIsInstance(result, str)

    def test_wrong_date_flagged_regardless_of_sep(self):
        """A completely wrong directory date is flagged no matter which
        separator format it uses."""
        photos = self.tmp / "photos"
        wrong_dir = photos / "2020" / "01" / "01"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=photos, default_sep="-")
        mf = ImageFile(wrong_dir, "photo.jpg", cfg, exif_reader=reader)
        self.assertIsNotNone(mf.validate())

    def test_returns_none_for_named_subdirectory(self):
        """File in yyyy/mm/dd/Originals/ must not be flagged as misplaced."""
        photos = self.tmp / "photos"
        d = date(2008, 3, 24)
        subdir = photos / "2008" / "03" / "24" / "Originals"
        subdir.mkdir(parents=True)
        (subdir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=photos)
        mf = ImageFile(subdir, "photo.jpg", cfg, exif_reader=reader)
        self.assertIsNone(mf.validate())

    def test_returns_none_for_deep_subdirectory(self):
        """Multi-level subdirectory under the correct dated dir is also valid."""
        photos = self.tmp / "photos"
        d = date(2008, 3, 24)
        subdir = photos / "2008" / "03" / "24" / "Originals" / "Raw"
        subdir.mkdir(parents=True)
        (subdir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=photos)
        mf = ImageFile(subdir, "photo.jpg", cfg, exif_reader=reader)
        self.assertIsNone(mf.validate())

    def test_returns_message_for_subdir_of_wrong_date(self):
        """Named subdir under a wrong dated directory is still flagged."""
        photos = self.tmp / "photos"
        wrong_subdir = photos / "2020" / "01" / "01" / "Originals"
        wrong_subdir.mkdir(parents=True)
        (wrong_subdir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2008, 3, 24)
        cfg = make_cfg(photos_path=photos)
        mf = ImageFile(wrong_subdir, "photo.jpg", cfg, exif_reader=reader)
        self.assertIsNotNone(mf.validate())

    def test_returns_message_when_no_date(self):
        mf = make_image(self.tmp, "undated.jpg")
        result = mf.validate()
        self.assertIsNotNone(result)

    def test_returns_message_when_no_root(self):
        mf = make_image_with_date(self.tmp, "photo.jpg", date(2023, 1, 1))
        # No photos_path in config
        result = mf.validate()
        self.assertIsNotNone(result)


# ── Delete ─────────────────────────────────────────────────────────────────

class TestMediaFileDelete(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_delete_removes_file(self):
        mf = make_image(self.tmp, "photo.jpg", dryrun=False)
        registry = MediaRegistry(make_cfg(dryrun=False))
        mf.delete(registry)
        self.assertFalse((self.tmp / "photo.jpg").exists())
        self.assertTrue(mf.deleted)

    def test_delete_dryrun_does_not_remove(self):
        mf = make_image(self.tmp, "photo.jpg", dryrun=True)
        registry = MediaRegistry(make_cfg(dryrun=True))
        mf.delete(registry)
        self.assertTrue((self.tmp / "photo.jpg").exists())
        self.assertTrue(mf.deleted)

    def test_delete_idempotent(self):
        mf = make_image(self.tmp, "photo.jpg")
        registry = MediaRegistry(make_cfg())
        mf.delete(registry)
        mf.delete(registry)  # Should not raise
        self.assertTrue(mf.deleted)


# ── fix_date ───────────────────────────────────────────────────────────────

class TestMediaFileFixDate(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def test_moves_to_correct_directory(self):
        src = self.tmp / "inbox"
        src.mkdir()
        (src / "photo.jpg").write_bytes(b"data")
        d = date(2023, 8, 14)
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=self.photos, dryrun=False)
        mf = ImageFile(src, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)
        dir_cache = DirCache()

        msg = mf.fix_date(registry, dir_cache)
        expected = self.photos / "2023" / "08" / "14" / "photo.jpg"
        self.assertIsNone(msg, msg)
        self.assertTrue(expected.exists())
        self.assertFalse((src / "photo.jpg").exists())

    def test_returns_message_for_undated_file(self):
        mf = make_image(self.tmp, "undated.jpg")
        registry = MediaRegistry(make_cfg())
        dir_cache = DirCache()
        msg = mf.fix_date(registry, dir_cache)
        self.assertIsNotNone(msg)

    def test_dryrun_does_not_move(self):
        src = self.tmp / "inbox"
        src.mkdir()
        (src / "photo.jpg").write_bytes(b"data")
        d = date(2023, 8, 14)
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=self.photos, dryrun=True)
        mf = ImageFile(src, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)
        dir_cache = DirCache()

        msg = mf.fix_date(registry, dir_cache)
        self.assertIsNotNone(msg)
        self.assertIn("dryrun", msg.lower())
        self.assertTrue((src / "photo.jpg").exists())

    def test_duplicate_target_deletes_self(self):
        src = self.tmp / "inbox"
        dst = self.photos / "2023" / "08" / "14"
        src.mkdir()
        dst.mkdir(parents=True)
        data = b"identical content"
        (src / "photo.jpg").write_bytes(data)
        (dst / "photo.jpg").write_bytes(data)

        d = date(2023, 8, 14)
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=self.photos, dryrun=False)
        mf = ImageFile(src, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)
        dir_cache = DirCache()

        msg = mf.fix_date(registry, dir_cache)
        self.assertFalse((src / "photo.jpg").exists())
        self.assertTrue(mf.deleted)

    def test_dryrun_duplicate_target_reports_message(self):
        """In dryrun mode, when the target already holds an identical file,
        the message should say 'duplicate' and the source must not be deleted."""
        src = self.tmp / "inbox"
        dst = self.photos / "2023" / "08" / "14"
        src.mkdir()
        dst.mkdir(parents=True)
        data = b"identical content" * 10
        (src / "photo.jpg").write_bytes(data)
        (dst / "photo.jpg").write_bytes(data)

        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos, dryrun=True)
        mf = ImageFile(src, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)
        dir_cache = DirCache()

        msg = mf.fix_date(registry, dir_cache)
        self.assertIsNotNone(msg)
        self.assertIn("duplicate", msg.lower())
        # Source must survive in dryrun
        self.assertTrue((src / "photo.jpg").exists())

    def test_already_in_place_returns_none(self):
        correct_dir = self.photos / "2023" / "08" / "14"
        correct_dir.mkdir(parents=True)
        (correct_dir / "photo.jpg").write_bytes(b"data")
        d = date(2023, 8, 14)
        reader = MagicMock()
        reader.get_date.return_value = d
        cfg = make_cfg(photos_path=self.photos)
        mf = ImageFile(correct_dir, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)
        dir_cache = DirCache()
        msg = mf.fix_date(registry, dir_cache)
        self.assertIsNone(msg)

    def test_subdir_preserved_on_relocation(self):
        """File in wrong_date/Originals/ must land in correct_date/Originals/."""
        src_subdir = self.photos / "2023" / "01" / "01" / "Originals"
        src_subdir.mkdir(parents=True)
        (src_subdir / "photo.jpg").write_bytes(b"data")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos, dryrun=False)
        mf = ImageFile(src_subdir, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)
        msg = mf.fix_date(registry, DirCache())
        expected = self.photos / "2023" / "08" / "14" / "Originals" / "photo.jpg"
        self.assertIsNone(msg, msg)
        self.assertTrue(expected.exists())
        self.assertFalse((src_subdir / "photo.jpg").exists())

    def test_subdir_preserved_dryrun_message(self):
        """Dryrun message must include the subdir in the target path."""
        src_subdir = self.photos / "2023" / "01" / "01" / "Originals"
        src_subdir.mkdir(parents=True)
        (src_subdir / "photo.jpg").write_bytes(b"data")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos, dryrun=True)
        mf = ImageFile(src_subdir, "photo.jpg", cfg, exif_reader=reader)
        msg = mf.fix_date(MediaRegistry(cfg), DirCache())
        self.assertIsNotNone(msg)
        self.assertIn("Originals", msg)

    def test_already_in_place_with_subdir_returns_none(self):
        """File already in correct_date/Originals/ must not be moved."""
        subdir = self.photos / "2023" / "08" / "14" / "Originals"
        subdir.mkdir(parents=True)
        (subdir / "photo.jpg").write_bytes(b"data")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos)
        mf = ImageFile(subdir, "photo.jpg", cfg, exif_reader=reader)
        msg = mf.fix_date(MediaRegistry(cfg), DirCache())
        self.assertIsNone(msg)


# ── Sorting / comparison ───────────────────────────────────────────────────

class TestMediaFileSorting(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def _make_pair(self, name_a, name_b, date_a=None, date_b=None):
        for n in (name_a, name_b):
            (self.tmp / n).write_bytes(b"x")
        reader_a = MagicMock()
        reader_a.get_date.return_value = date_a
        reader_b = MagicMock()
        reader_b.get_date.return_value = date_b
        cfg = make_cfg()
        a = ImageFile(self.tmp, name_a, cfg, exif_reader=reader_a)
        b = ImageFile(self.tmp, name_b, cfg, exif_reader=reader_b)
        return a, b

    def test_alphabetical_filename_ordering(self):
        a, b = self._make_pair("aaa.jpg", "zzz.jpg")
        self.assertLess(a, b)

    def test_sequenced_files_sort_after_non_sequenced(self):
        a, b = self._make_pair("photo.jpg", "photo_001.jpg")
        # sequence 0 < sequence 1, so photo.jpg < photo_001.jpg
        self.assertLess(a, b)

    def test_equality_reflexive(self):
        (self.tmp / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = None
        cfg = make_cfg()
        mf = ImageFile(self.tmp, "photo.jpg", cfg, exif_reader=reader)
        self.assertEqual(mf, mf)

    def test_sorted_list(self):
        files = []
        for name in ("c.jpg", "a.jpg", "b.jpg"):
            (self.tmp / name).write_bytes(b"x")
            reader = MagicMock()
            reader.get_date.return_value = None
            mf = ImageFile(self.tmp, name, make_cfg(), exif_reader=reader)
            files.append(mf)
        names = [mf.filename for mf in sorted(files)]
        self.assertEqual(names, ["a.jpg", "b.jpg", "c.jpg"])


# ── MediaRegistry ──────────────────────────────────────────────────────────

class TestMediaRegistry(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def _make_file(self, name: str, content: bytes = b"x") -> Path:
        p = self.tmp / name
        p.write_bytes(content)
        return p

    def test_creates_image_for_jpg(self):
        self._make_file("photo.jpg")
        registry = MediaRegistry(make_cfg())
        mf = registry.get_or_create(self.tmp, "photo.jpg")
        self.assertIsInstance(mf, ImageFile)

    def test_creates_video_for_mp4(self):
        self._make_file("clip.mp4")
        registry = MediaRegistry(make_cfg())
        mf = registry.get_or_create(self.tmp, "clip.mp4")
        self.assertIsInstance(mf, VideoFile)

    def test_returns_none_for_ignored_extension(self):
        self._make_file("audio.mp3")
        registry = MediaRegistry(make_cfg())
        mf = registry.get_or_create(self.tmp, "audio.mp3")
        self.assertIsNone(mf)

    def test_unknown_extension_recorded_with_count(self):
        """Unrecognised (non-ignored) extensions must be tracked in a count dict."""
        self._make_file("report.docx")
        self._make_file("notes.docx")
        registry = MediaRegistry(make_cfg())
        registry.get_or_create(self.tmp, "report.docx")
        registry.get_or_create(self.tmp, "notes.docx")
        self.assertEqual(registry._unknown_extensions.get("docx"), 2)

    def test_ignored_extensions_not_in_unknown(self):
        """Known-ignored extensions (audio, .thm, etc.) must NOT enter _unknown_extensions."""
        self._make_file("audio.mp3")
        registry = MediaRegistry(make_cfg())
        registry.get_or_create(self.tmp, "audio.mp3")
        self.assertNotIn("mp3", registry._unknown_extensions)

    def test_returns_none_for_no_extension(self):
        self._make_file("README")
        registry = MediaRegistry(make_cfg())
        mf = registry.get_or_create(self.tmp, "README")
        self.assertIsNone(mf)

    def test_cache_hit_returns_same_object(self):
        self._make_file("photo.jpg")
        registry = MediaRegistry(make_cfg())
        mf1 = registry.get_or_create(self.tmp, "photo.jpg")
        mf2 = registry.get_or_create(self.tmp, "photo.jpg")
        self.assertIs(mf1, mf2)

    def test_evict_removes_from_cache(self):
        self._make_file("photo.jpg")
        registry = MediaRegistry(make_cfg())
        mf = registry.get_or_create(self.tmp, "photo.jpg")
        registry.evict(mf.path)
        # After eviction, a new call should build a new object
        mf2 = registry.get_or_create(self.tmp, "photo.jpg")
        self.assertIsNot(mf, mf2)

    def test_all_image_extensions_recognised(self):
        registry = MediaRegistry(make_cfg())
        for ext in IMAGE_EXTENSIONS:
            name = f"file.{ext}"
            self._make_file(name)
            mf = registry.get_or_create(self.tmp, name)
            self.assertIsInstance(mf, ImageFile, f".{ext} should be ImageFile")

    def test_all_video_extensions_recognised(self):
        registry = MediaRegistry(make_cfg())
        for ext in VIDEO_EXTENSIONS:
            name = f"clip.{ext}"
            self._make_file(name)
            mf = registry.get_or_create(self.tmp, name)
            self.assertIsInstance(mf, VideoFile, f".{ext} should be VideoFile")

    def test_move_updates_path(self):
        self._make_file("photo.jpg")
        dest = self.tmp / "dest"
        dest.mkdir()
        registry = MediaRegistry(make_cfg())
        mf = registry.get_or_create(self.tmp, "photo.jpg")
        registry.move(mf, dest, "photo.jpg")
        self.assertEqual(mf.directory, dest.resolve())
        self.assertTrue((dest / "photo.jpg").exists())


# ── _normalised_directory ─────────────────────────────────────────────────

class TestNormalisedDirectory(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_returns_none_when_no_date(self):
        mf = make_image(self.tmp, "undated.jpg")
        self.assertIsNone(mf._normalised_directory)

    def test_returns_none_when_no_root(self):
        # photos_path=None → default_root is None
        (self.tmp / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        mf = ImageFile(self.tmp, "photo.jpg", make_cfg(), exif_reader=reader)
        self.assertIsNone(mf._normalised_directory)


# ── __eq__, __lt__, __hash__, __repr__ ────────────────────────────────────

class TestMediaFileSpecialMethods(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_eq_returns_not_implemented_for_non_mediafile(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertEqual(mf.__eq__("not a media file"), NotImplemented)

    def test_lt_returns_not_implemented_for_non_mediafile(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertEqual(mf.__lt__(42), NotImplemented)

    def test_hash_allows_use_in_set(self):
        mf = make_image(self.tmp, "photo.jpg")
        s = {mf}
        self.assertIn(mf, s)

    def test_repr_normal(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertIn("photo.jpg", repr(mf))

    def test_repr_deleted(self):
        mf = make_image(self.tmp, "photo.jpg")
        mf._deleted = True
        self.assertIn("DELETED", repr(mf))

    def test_repr_debug_misdated(self):
        wrong_dir = self.tmp / "2020" / "01" / "01"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(debug=True)
        mf = ImageFile(wrong_dir, "photo.jpg", cfg, exif_reader=reader)
        # Force dated to be computed so _dated is not _UNSET
        _ = mf.dated
        r = repr(mf)
        self.assertIn("photo.jpg", r)


# ── fix_date edge cases ───────────────────────────────────────────────────

class TestFixDateEdgeCases(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def test_fix_date_on_deleted_file_returns_message(self):
        mf = make_image(self.tmp, "photo.jpg")
        mf._deleted = True
        registry = MediaRegistry(make_cfg())
        dir_cache = DirCache()
        msg = mf.fix_date(registry, dir_cache)
        self.assertIsNotNone(msg)
        self.assertIn("deleted", msg.lower())

    def test_fix_date_with_dated_but_no_root_returns_message(self):
        (self.tmp / "IMG_2023-08-14.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        # No photos_path → default_root is None → no target
        mf = ImageFile(self.tmp, "IMG_2023-08-14.jpg", make_cfg(), exif_reader=reader)
        registry = MediaRegistry(make_cfg())
        dir_cache = DirCache()
        msg = mf.fix_date(registry, dir_cache)
        self.assertIsNotNone(msg)
        self.assertIn("no target root", msg.lower())

    def test_relocate_source_missing_returns_message(self):
        src = self.tmp / "inbox"
        src.mkdir()
        (src / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos)
        mf = ImageFile(src, "photo.jpg", cfg, exif_reader=reader)
        # Delete the file so _relocate sees a missing source
        (src / "photo.jpg").unlink()
        registry = MediaRegistry(cfg)
        dir_cache = DirCache()
        msg = mf.fix_date(registry, dir_cache)
        self.assertIsNotNone(msg)
        self.assertIn("missing", msg.lower())


# ── _relocate collision paths ─────────────────────────────────────────────

class TestRelocateCollision(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def _make_collision(self, src_data=b"AAA" * 50, dst_data=b"BBB" * 50):
        """Place file in inbox and put a different file at the target."""
        src = self.tmp / "inbox"
        src.mkdir(exist_ok=True)
        dst = self.photos / "2023" / "08" / "14"
        dst.mkdir(parents=True, exist_ok=True)
        (src / "photo.jpg").write_bytes(src_data)
        (dst / "photo.jpg").write_bytes(dst_data)
        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos)
        mf = ImageFile(src, "photo.jpg", cfg, exif_reader=reader)
        return mf, cfg, src

    def test_collision_returns_error_without_force(self):
        mf, cfg, _ = self._make_collision()
        registry = MediaRegistry(cfg)
        msg = mf.fix_date(registry, DirCache())
        self.assertIsNotNone(msg)
        self.assertIn("Collision", msg)

    def test_collision_renames_with_force(self):
        mf, _, src = self._make_collision()
        cfg = make_cfg(photos_path=self.photos, do_force=True, dryrun=False)
        mf._config = cfg
        registry = MediaRegistry(cfg)
        msg = mf.fix_date(registry, DirCache())
        # With force, a renamed copy should land in the target dir
        target = self.photos / "2023" / "08" / "14"
        moved = list(target.glob("photo_*.jpg"))
        self.assertTrue(len(moved) >= 1, "Expected a renamed copy in target")

    def test_collision_uses_overflow_dir(self):
        overflow = self.tmp / "overflow"
        overflow.mkdir()
        mf, _, src = self._make_collision()
        cfg = make_cfg(photos_path=self.photos, misdated_path=overflow, dryrun=False)
        mf._config = cfg
        registry = MediaRegistry(cfg)
        msg = mf.fix_date(registry, DirCache(), overflow_path=overflow)
        # File should end up in overflow (no collision there)
        self.assertTrue(list(overflow.rglob("photo.jpg")))


# ── VideoFile ─────────────────────────────────────────────────────────────

class TestVideoFile(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_default_root_returns_videos_path(self):
        videos = self.tmp / "videos"
        videos.mkdir()
        (self.tmp / "clip.mp4").write_bytes(b"x")
        cfg = make_cfg(videos_path=videos)
        exif_reader = MagicMock()
        exif_reader.get_date.return_value = None
        mf = VideoFile(self.tmp, "clip.mp4", cfg, exif_reader=exif_reader)
        self.assertEqual(mf.default_root, videos)

    def test_exif_date_uses_mediainfo_fallback(self):
        (self.tmp / "clip.mp4").write_bytes(b"x")
        exif_reader = MagicMock()
        exif_reader.get_date.return_value = None
        mediainfo_reader = MagicMock()
        mediainfo_reader.get_date.return_value = date(2021, 3, 15)
        cfg = make_cfg()
        mf = VideoFile(self.tmp, "clip.mp4", cfg,
                       exif_reader=exif_reader,
                       mediainfo_reader=mediainfo_reader)
        self.assertEqual(mf.dated, date(2021, 3, 15))
        mediainfo_reader.get_date.assert_called_once_with(mf.path)


# ── MediaRegistry edge cases ──────────────────────────────────────────────

class TestMediaRegistryEdgeCases(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_move_raises_when_target_in_cache(self):
        """move() must raise RuntimeError if the target path is already cached."""
        (self.tmp / "photo.jpg").write_bytes(b"x")
        dest = self.tmp / "dest"
        dest.mkdir()
        (dest / "photo.jpg").write_bytes(b"y")

        cfg = make_cfg()
        registry = MediaRegistry(cfg)
        mf_src = registry.get_or_create(self.tmp, "photo.jpg")
        # Pre-populate the target in the cache
        registry.get_or_create(dest, "photo.jpg")

        with self.assertRaises(RuntimeError):
            registry.move(mf_src, dest, "photo.jpg")

    def test_mediainfo_debug_log_when_available(self):
        """MediaRegistry logs a debug message when MediaInfo is found."""
        with patch("deduper.media.MediaInfoReader.available", return_value=True), \
             patch("deduper.media.logger") as mock_log:
            MediaRegistry(make_cfg())
        mock_log.debug.assert_called()
        debug_msgs = [str(c[0][0]) for c in mock_log.debug.call_args_list]
        self.assertTrue(any("MediaInfo" in m for m in debug_msgs))


class TestRelocateSelfDeletion(unittest.TestCase):
    """Regression tests for bug: _relocate can delete the source file when the
    overflow path is the same directory as the source file.

    Scenario: primary target has a different file (collision); overflow is
    misconfigured to equal the source directory.  The recursive _relocate call
    finds 'photo.jpg' already at overflow/photo.jpg — which is the source file
    itself — compares hashes (identical, it IS the same file), and deletes it.
    """

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def test_no_self_deletion_when_overflow_is_source_directory(self):
        """Source file must survive when overflow == source directory."""
        src_dir = self.tmp / "inbox"
        src_dir.mkdir()
        dst_dir = self.photos / "2023" / "08" / "14"
        dst_dir.mkdir(parents=True)

        (src_dir / "photo.jpg").write_bytes(b"source content" * 10)
        (dst_dir / "photo.jpg").write_bytes(b"different content" * 10)

        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos)
        mf = ImageFile(src_dir, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)

        # overflow == src_dir: the file's own directory
        msg = mf.fix_date(registry, DirCache(), overflow_path=src_dir)

        # Before fix: source file is deleted (self-deletion bug).
        # After fix:  source file survives and an explanatory message is returned.
        self.assertTrue(
            (src_dir / "photo.jpg").exists(),
            "Source file was deleted — self-deletion bug triggered",
        )
        self.assertFalse(mf.deleted)

    def test_overflow_to_different_directory_still_works(self):
        """Normal overflow (to a genuinely different directory) must not be broken."""
        src_dir = self.tmp / "inbox"
        overflow_dir = self.tmp / "overflow"
        dst_dir = self.photos / "2023" / "08" / "14"
        src_dir.mkdir()
        overflow_dir.mkdir()
        dst_dir.mkdir(parents=True)

        (src_dir / "photo.jpg").write_bytes(b"source content" * 10)
        (dst_dir / "photo.jpg").write_bytes(b"collision content" * 10)

        reader = MagicMock()
        reader.get_date.return_value = date(2023, 8, 14)
        cfg = make_cfg(photos_path=self.photos, dryrun=False)
        mf = ImageFile(src_dir, "photo.jpg", cfg, exif_reader=reader)
        registry = MediaRegistry(cfg)

        mf.fix_date(registry, DirCache(), overflow_path=overflow_dir)

        self.assertTrue((overflow_dir / "photo.jpg").exists())
        self.assertFalse((src_dir / "photo.jpg").exists())


class TestRegistryMoveXdev(unittest.TestCase):
    """Regression tests for bug: MediaRegistry.move() uses Path.rename(),
    which raises OSError(EXDEV) on cross-device moves and silently overwrites
    on POSIX when a race puts a file at the destination.  Fix: shutil.move()."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def _xdev_error(self):
        return OSError(errno.EXDEV, "Invalid cross-device link")

    def test_move_does_not_raise_on_cross_device_error(self):
        """registry.move() must complete when Path.rename raises EXDEV.

        Bug: media.path.rename(new_path) propagates the OSError uncaught,
        aborting the entire scan run mid-batch with no partial-completion
        report.  Any files not yet processed are left in place silently.
        After fix (shutil.move): the copy+delete fallback is used instead.
        """
        src_dir = self.tmp / "inbox"
        dst_dir = self.tmp / "photos" / "2023" / "08" / "14"
        src_dir.mkdir()
        dst_dir.mkdir(parents=True)
        (src_dir / "photo.jpg").write_bytes(b"photo data")

        cfg = make_cfg()
        registry = MediaRegistry(cfg)
        mf = registry.get_or_create(src_dir, "photo.jpg")

        with patch.object(Path, 'rename', side_effect=self._xdev_error()):
            # Before fix: raises OSError → test fails with unexpected exception.
            # After fix:  shutil.move uses os.rename (not patched) → completes.
            registry.move(mf, dst_dir, "photo.jpg")

        self.assertTrue((dst_dir / "photo.jpg").exists())
        self.assertFalse((src_dir / "photo.jpg").exists())

    def test_registry_reflects_new_location_after_cross_device_move(self):
        """After a cross-device move the registry must track the new path."""
        src_dir = self.tmp / "src"
        dst_dir = self.tmp / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()
        (src_dir / "photo.jpg").write_bytes(b"data")

        cfg = make_cfg()
        registry = MediaRegistry(cfg)
        mf = registry.get_or_create(src_dir, "photo.jpg")

        with patch.object(Path, 'rename', side_effect=self._xdev_error()):
            registry.move(mf, dst_dir, "photo.jpg")

        # MediaFile object must reflect new location.
        self.assertEqual(mf.directory, dst_dir.resolve())
        self.assertEqual(mf.filename, "photo.jpg")
        # New path cached; old path evicted.
        self.assertIn((dst_dir / "photo.jpg").resolve(), registry._cache)
        self.assertNotIn((src_dir / "photo.jpg").resolve(), registry._cache)


# ── _subdir_below_date ────────────────────────────────────────────────────

class TestSubdirBelowDate(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def _make_in(self, rel_dir: str, filename: str, exif_date=None) -> ImageFile:
        dirpath = self.photos / Path(rel_dir)
        dirpath.mkdir(parents=True, exist_ok=True)
        (dirpath / filename).write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = exif_date
        return ImageFile(dirpath, filename, make_cfg(photos_path=self.photos), exif_reader=reader)

    def test_none_when_at_date_level(self):
        mf = self._make_in("2008/03/24", "photo.jpg", date(2008, 3, 24))
        self.assertIsNone(mf._subdir_below_date())

    def test_returns_subdir_for_one_level_below(self):
        mf = self._make_in("2008/11/23/Originals", "photo.jpg", date(2008, 3, 24))
        self.assertEqual(mf._subdir_below_date(), Path("Originals"))

    def test_returns_subdir_for_deep_nesting(self):
        mf = self._make_in("2008/11/23/Originals/Raw", "photo.jpg", date(2008, 3, 24))
        self.assertEqual(mf._subdir_below_date(), Path("Originals/Raw"))

    def test_uses_path_date_not_exif_date(self):
        """For a misplaced file, path_date (2008-11-23) differs from self.dated
        (2008-03-24).  _subdir_below_date must use the path date to locate
        the date-level parent, not the EXIF date."""
        mf = self._make_in("2008/11/23/Originals", "photo.jpg", date(2008, 3, 24))
        # If it incorrectly used self.dated (2008-03-24), it would try to
        # relativize against photos/2008/03/24 — which fails → returns None.
        self.assertEqual(mf._subdir_below_date(), Path("Originals"))

    def test_none_when_no_date_in_path(self):
        (self.tmp / "inbox").mkdir(exist_ok=True)
        (self.tmp / "inbox" / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2008, 3, 24)
        mf = ImageFile(self.tmp / "inbox", "photo.jpg",
                       make_cfg(photos_path=self.photos), exif_reader=reader)
        self.assertIsNone(mf._subdir_below_date())

    def test_none_when_no_root(self):
        (self.tmp / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2008, 3, 24)
        mf = ImageFile(self.tmp, "photo.jpg", make_cfg(), exif_reader=reader)
        self.assertIsNone(mf._subdir_below_date())

    def test_none_for_load_path_outside_library(self):
        """Files from an external import path should not get a subdir extracted
        even if the import path contains a date component."""
        inbox = self.tmp / "inbox" / "2008" / "11" / "23" / "Originals"
        inbox.mkdir(parents=True)
        (inbox / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2008, 3, 24)
        # photos_path is self.photos; file is under inbox — relative_to fails
        mf = ImageFile(inbox, "photo.jpg",
                       make_cfg(photos_path=self.photos), exif_reader=reader)
        self.assertIsNone(mf._subdir_below_date())


# ── _in_correct_subtree ───────────────────────────────────────────────────

class TestInCorrectSubtree(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def _make_in(self, rel_dir: str, filename: str, d: date) -> ImageFile:
        dirpath = self.photos / Path(rel_dir)
        dirpath.mkdir(parents=True, exist_ok=True)
        (dirpath / filename).write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        return ImageFile(dirpath, filename, make_cfg(photos_path=self.photos), exif_reader=reader)

    def test_true_for_exact_slash_dir(self):
        mf = self._make_in("2008/03/24", "photo.jpg", date(2008, 3, 24))
        self.assertTrue(mf._in_correct_subtree(self.photos))

    def test_true_for_exact_dash_dir(self):
        mf = self._make_in("2008-03-24", "photo.jpg", date(2008, 3, 24))
        self.assertTrue(mf._in_correct_subtree(self.photos))

    def test_true_for_named_subdirectory(self):
        """A file in yyyy/mm/dd/Originals/ is within the correct subtree."""
        mf = self._make_in("2008/03/24/Originals", "photo.jpg", date(2008, 3, 24))
        self.assertTrue(mf._in_correct_subtree(self.photos))

    def test_true_for_deep_named_subdirectory(self):
        """Multi-level subdirectories are accepted."""
        mf = self._make_in("2008/03/24/Originals/Raw", "photo.jpg", date(2008, 3, 24))
        self.assertTrue(mf._in_correct_subtree(self.photos))

    def test_true_for_subdir_of_dash_dir(self):
        mf = self._make_in("2008-03-24/Originals", "photo.jpg", date(2008, 3, 24))
        self.assertTrue(mf._in_correct_subtree(self.photos))

    def test_false_for_wrong_dated_dir(self):
        """Wrong date directory is not a valid subtree even with a matching subdir name."""
        mf = self._make_in("2020/01/01/Originals", "photo.jpg", date(2008, 3, 24))
        self.assertFalse(mf._in_correct_subtree(self.photos))

    def test_false_for_parent_of_valid_dir(self):
        """A parent of the valid dir (e.g. yyyy/mm/) is not a valid subtree."""
        mf = self._make_in("2008/03", "photo.jpg", date(2008, 3, 24))
        self.assertFalse(mf._in_correct_subtree(self.photos))

    def test_false_when_no_date(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertFalse(mf._in_correct_subtree(self.photos))


# ── _in_correct_location ──────────────────────────────────────────────────

class TestInCorrectLocation(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def _make_in(self, rel_dir: str, filename: str, d: date) -> ImageFile:
        dirpath = self.photos / Path(rel_dir)
        dirpath.mkdir(parents=True, exist_ok=True)
        (dirpath / filename).write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = d
        return ImageFile(dirpath, filename, make_cfg(photos_path=self.photos), exif_reader=reader)

    def test_true_when_in_correct_slash_dir(self):
        mf = self._make_in("2021/09/29", "photo.jpg", date(2021, 9, 29))
        self.assertTrue(mf._in_correct_location)

    def test_true_when_in_correct_dash_dir(self):
        mf = self._make_in("2021-09-29", "photo.jpg", date(2021, 9, 29))
        self.assertTrue(mf._in_correct_location)

    def test_false_when_in_nested_subdir(self):
        """File in 2021/09/29/2021-09-29/ is NOT in a valid home for its date."""
        mf = self._make_in("2021/09/29/2021-09-29", "photo.jpg", date(2021, 9, 29))
        self.assertFalse(mf._in_correct_location)

    def test_false_when_wrong_date_dir(self):
        mf = self._make_in("2020/01/01", "photo.jpg", date(2021, 9, 29))
        self.assertFalse(mf._in_correct_location)

    def test_false_when_no_date(self):
        mf = make_image(self.tmp, "photo.jpg")
        self.assertFalse(mf._in_correct_location)

    def test_false_when_no_root(self):
        (self.tmp / "photo.jpg").write_bytes(b"x")
        reader = MagicMock()
        reader.get_date.return_value = date(2021, 9, 29)
        mf = ImageFile(self.tmp, "photo.jpg", make_cfg(), exif_reader=reader)
        self.assertFalse(mf._in_correct_location)

    def test_correctly_placed_sorts_before_nested(self):
        """File in 2021/09/29/ must sort before file in 2021/09/29/2021-09-29/."""
        d = date(2021, 9, 29)
        correct = self._make_in("2021/09/29", "VID_20210929.mp4", d)
        nested  = self._make_in("2021/09/29/2021-09-29", "VID_20210929.mp4", d)
        self.assertLess(correct, nested)


if __name__ == "__main__":
    unittest.main()
