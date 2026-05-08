"""Tests for deduper.scanner — MediaScanner and Report."""

import contextlib
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
from deduper.media import ImageFile, MediaRegistry
from deduper.scanner import MediaScanner, Report, _image_exif_worker, _hash_worker


# ── Helpers ────────────────────────────────────────────────────────────────

def make_cfg(**kwargs) -> Config:
    return Config.from_defaults(**kwargs)


def make_image_file(tmp: Path, filename: str, exif_date=None, content=b"data") -> ImageFile:
    (tmp / filename).write_bytes(content)
    reader = MagicMock()
    reader.get_date.return_value = exif_date
    return ImageFile(tmp, filename, make_cfg(), exif_reader=reader)


def make_mock_pool(imap_results=None):
    """Return a (mock_pool_cls, mock_pool) pair for patching deduper.scanner.Pool.

    The pool is a properly-configured context manager so ``with Pool(n) as pool:``
    binds ``pool`` to ``mock_pool``.  Both ``pool.imap(...)`` and
    ``pool.imap_unordered(...)`` return a fresh iterator over *imap_results* on
    each call, so the same helper works regardless of which variant is used.

    Magic-method return values must be set via ``.return_value`` on the pre-existing
    MagicMock attribute, not by replacing the attribute with a new MagicMock instance —
    the latter bypasses Python's special-method lookup via the type.
    """
    results = list(imap_results) if imap_results is not None else []
    mock_pool = MagicMock()
    mock_pool.__enter__.return_value = mock_pool
    mock_pool.__exit__.return_value = False
    # Use side_effect so each call returns a fresh iterator (iterators are single-use).
    mock_pool.imap.side_effect = lambda fn, it, **kw: iter(results)
    mock_pool.imap_unordered.side_effect = lambda fn, it, **kw: iter(results)
    mock_pool_cls = MagicMock(return_value=mock_pool)
    return mock_pool_cls, mock_pool


# ── Report ─────────────────────────────────────────────────────────────────

class TestReport(unittest.TestCase):

    def test_empty_render(self):
        r = Report()
        self.assertEqual(r.render(), "")

    def test_single_line(self):
        r = Report()
        r("hello")
        self.assertIn("hello", r.render())

    def test_multiple_lines(self):
        r = Report()
        r("line1")
        r("line2")
        text = r.render()
        self.assertIn("line1", text)
        self.assertIn("line2", text)

    def test_multiple_args_in_one_call(self):
        r = Report()
        r("a", "b", "c")
        text = r.render()
        self.assertIn("a", text)
        self.assertIn("b", text)

    def test_send_returns_render(self):
        r = Report()
        r("done")
        self.assertEqual(r.send(None), r.render())

    def test_send_appends_elapsed_time(self):
        r = Report()
        r("some work done")
        text = r.send(None)
        self.assertIn("Completed in", text)
        # Format is Xm00s; at least minutes and seconds must be present
        import re
        self.assertRegex(text, r"Completed in \d+m\d{2}s")

    def test_send_to_address_logs_warning(self):
        r = Report()
        with patch("deduper.scanner.logger") as mock_log:
            r.send("user@example.com")
        mock_log.warning.assert_called_once()
        warning_args = mock_log.warning.call_args[0]
        self.assertTrue(any("user@example.com" in str(a) for a in warning_args))

    def test_non_string_args_converted(self):
        r = Report()
        r(42, None, 3.14)
        text = r.render()
        self.assertIn("42", text)


# ── MediaScanner._find_dupes ───────────────────────────────────────────────

class TestFindDupes(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def _scanner(self) -> MediaScanner:
        cfg = make_cfg()
        return MediaScanner(config=cfg)

    def test_no_dupes(self):
        mf1 = make_image_file(self.tmp, "a.jpg", content=b"AAA")
        mf2 = make_image_file(self.tmp, "b.jpg", content=b"BBB")
        result = self._scanner()._find_dupes([mf1, mf2])
        self.assertEqual(result, {})

    def test_identical_files_detected(self):
        data = b"identical" * 100
        mf1 = make_image_file(self.tmp, "a.jpg", content=data)
        mf2 = make_image_file(self.tmp, "b.jpg", content=data)
        result = self._scanner()._find_dupes([mf1, mf2])
        self.assertEqual(len(result), 1)
        group = list(result.values())[0]
        self.assertEqual(len(group), 2)

    def test_three_identical_files(self):
        data = b"same" * 200
        mf1 = make_image_file(self.tmp, "a.jpg", content=data)
        mf2 = make_image_file(self.tmp, "b.jpg", content=data)
        mf3 = make_image_file(self.tmp, "c.jpg", content=data)
        result = self._scanner()._find_dupes([mf1, mf2, mf3])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(list(result.values())[0]), 3)

    def test_groups_sorted(self):
        """Dupe group should be sorted by _key (filename alphabetically)."""
        data = b"same" * 200
        mf_z = make_image_file(self.tmp, "z.jpg", content=data)
        mf_a = make_image_file(self.tmp, "a.jpg", content=data)
        result = self._scanner()._find_dupes([mf_z, mf_a])
        group = list(result.values())[0]
        self.assertEqual(group[0].filename, "a.jpg")
        self.assertEqual(group[1].filename, "z.jpg")

    def test_single_file_not_a_dupe(self):
        mf = make_image_file(self.tmp, "a.jpg")
        result = self._scanner()._find_dupes([mf])
        self.assertEqual(result, {})

    def test_empty_list(self):
        result = self._scanner()._find_dupes([])
        self.assertEqual(result, {})


# ── MediaScanner._report_and_remove_dupes ─────────────────────────────────

class TestReportAndRemoveDupes(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_no_dupes_reports_message(self):
        report = Report()
        cfg = make_cfg()
        scanner = MediaScanner(config=cfg, report=report)
        scanner._report_and_remove_dupes({})
        self.assertIn("No exact duplicates", report.render())

    def test_dryrun_does_not_delete(self):
        data = b"x" * 200
        mf1 = make_image_file(self.tmp, "a.jpg", content=data)
        mf2 = make_image_file(self.tmp, "b.jpg", content=data)
        dupes = {(mf1.size, mf1.hash): [mf1, mf2]}

        report = Report()
        cfg = make_cfg(dryrun=True, do_fix=False)
        registry = MediaRegistry(cfg)
        scanner = MediaScanner(config=cfg, report=report, registry=registry)
        scanner._report_and_remove_dupes(dupes)

        self.assertTrue((self.tmp / "a.jpg").exists())
        self.assertTrue((self.tmp / "b.jpg").exists())

    def test_fix_deletes_redundant(self):
        data = b"y" * 200
        mf1 = make_image_file(self.tmp, "a.jpg", content=data)
        mf2 = make_image_file(self.tmp, "b.jpg", content=data)
        dupes = {(mf1.size, mf1.hash): [mf1, mf2]}

        report = Report()
        cfg = make_cfg(do_fix=True)
        registry = MediaRegistry(cfg)
        scanner = MediaScanner(config=cfg, report=report, registry=registry)
        scanner._report_and_remove_dupes(dupes)

        # Keeper (a.jpg, comes first alphabetically) should survive
        self.assertTrue((self.tmp / "a.jpg").exists())
        self.assertFalse((self.tmp / "b.jpg").exists())

    def test_report_only_without_fix(self):
        data = b"z" * 200
        mf1 = make_image_file(self.tmp, "a.jpg", content=data)
        mf2 = make_image_file(self.tmp, "b.jpg", content=data)
        dupes = {(mf1.size, mf1.hash): [mf1, mf2]}

        report = Report()
        cfg = make_cfg(do_fix=False, dryrun=False)
        registry = MediaRegistry(cfg)
        scanner = MediaScanner(config=cfg, report=report, registry=registry)
        scanner._report_and_remove_dupes(dupes)

        # Both files should still exist (report only)
        self.assertTrue((self.tmp / "a.jpg").exists())
        self.assertTrue((self.tmp / "b.jpg").exists())
        self.assertIn("Would remove", report.render())


# ── MediaScanner.run_load ──────────────────────────────────────────────────

class TestRunLoad(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.inbox = self.tmp / "inbox"
        self.photos = self.tmp / "photos"
        self.inbox.mkdir()
        self.photos.mkdir()

    def test_no_import_path_reports_skip(self):
        report = Report()
        cfg = make_cfg(do_load=True)  # No import_path
        scanner = MediaScanner(config=cfg, report=report)
        scanner.run_load()
        self.assertIn("skipped", report.render().lower())

    def test_imports_file_to_photos(self):
        (self.inbox / "IMG_2023-08-14.jpg").write_bytes(b"photo data")
        report = Report()
        cfg = make_cfg(
            import_path=self.inbox,
            photos_path=self.photos,
            do_load=True,
        )
        registry = MediaRegistry(cfg)
        dir_cache = DirCache()
        scanner = MediaScanner(config=cfg, report=report, registry=registry, dir_cache=dir_cache)
        scanner.run_load()
        expected = self.photos / "2023" / "08" / "14" / "IMG_2023-08-14.jpg"
        self.assertTrue(expected.exists(), f"Expected file at {expected}")

    def test_dryrun_does_not_import(self):
        (self.inbox / "IMG_2023-08-14.jpg").write_bytes(b"data")
        report = Report()
        cfg = make_cfg(
            import_path=self.inbox,
            photos_path=self.photos,
            do_load=True,
            dryrun=True,
        )
        scanner = MediaScanner(config=cfg, report=report)
        scanner.run_load()
        self.assertTrue((self.inbox / "IMG_2023-08-14.jpg").exists())


# ── MediaScanner.run_validate ──────────────────────────────────────────────

class TestRunValidate(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def _place_file(self, rel_dir: str, filename: str, exif_date=None, content=b"x"):
        d = self.photos / rel_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_bytes(content)

    def test_correctly_placed_not_reported(self):
        # File name contains date matching directory; no EXIF needed
        self._place_file("2023/08/14", "IMG_2023-08-14.jpg")
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True)
        scanner = MediaScanner(config=cfg, report=report)
        scanner.run_validate()
        text = report.render()
        self.assertNotIn("IMG_2023-08-14.jpg", text)

    def test_misplaced_file_reported(self):
        wrong_dir = self.photos / "2020" / "01" / "01"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "IMG_2023-08-14.jpg").write_bytes(b"data")
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True)
        scanner = MediaScanner(config=cfg, report=report)
        scanner.run_validate()
        text = report.render()
        # The summary groups by date — confirm a misplaced group was found
        self.assertIn("misplaced file", text.lower())
        self.assertNotIn("No misplaced", text)

    def test_no_misplaced_reports_clean(self):
        # Empty photos dir
        self.photos.mkdir()
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True)
        scanner = MediaScanner(config=cfg, report=report)
        scanner.run_validate()
        self.assertIn("No misplaced", report.render())


# ── run_validate — separator tolerance ────────────────────────────────────

class TestRunValidateSeparator(unittest.TestCase):
    """Validate that both yyyy/mm/dd and yyyy-mm-dd are accepted as valid
    homes for a file, regardless of the configured default_sep."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def test_slash_dir_not_flagged_when_sep_is_dash(self):
        """File in yyyy/mm/dd must not be reported as misplaced when
        default_sep='-' would normalise to yyyy-mm-dd."""
        d = self.photos / "2023" / "08" / "14"
        d.mkdir(parents=True)
        (d / "IMG_2023-08-14.jpg").write_bytes(b"data")
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True, default_sep="-")
        MediaScanner(config=cfg, report=report).run_validate()
        self.assertIn("No misplaced", report.render())

    def test_dash_dir_not_flagged_when_sep_is_slash(self):
        """File in yyyy-mm-dd must not be reported as misplaced when
        default_sep='/' would normalise to yyyy/mm/dd."""
        d = self.photos / "2023-08-14"
        d.mkdir(parents=True)
        (d / "IMG_2023-08-14.jpg").write_bytes(b"data")
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True, default_sep="/")
        MediaScanner(config=cfg, report=report).run_validate()
        self.assertIn("No misplaced", report.render())

    def test_wrong_date_dir_still_flagged_with_dash_sep(self):
        """A genuinely wrong directory (wrong date, not just wrong separator)
        must still be reported."""
        wrong = self.photos / "2020" / "01" / "01"
        wrong.mkdir(parents=True)
        (wrong / "IMG_2023-08-14.jpg").write_bytes(b"data")
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True, default_sep="-")
        MediaScanner(config=cfg, report=report).run_validate()
        self.assertNotIn("No misplaced", report.render())


# ── run_validate — duplicate detection ────────────────────────────────────

class TestRunValidateDuplicateDetection(unittest.TestCase):
    """run_validate should label misplaced files as duplicates when the
    target location already holds an identical copy."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def _setup_misplaced_with_duplicate(self, data=b"content" * 50):
        """Create a file in the wrong directory and an identical copy at the
        correct location.  Returns (misplaced_path, keeper_path)."""
        wrong   = self.photos / "2020" / "01" / "01"
        correct = self.photos / "2023" / "08" / "14"
        wrong.mkdir(parents=True)
        correct.mkdir(parents=True)
        mp = wrong   / "IMG_2023-08-14.jpg"
        kp = correct / "IMG_2023-08-14.jpg"
        mp.write_bytes(data)
        kp.write_bytes(data)
        return mp, kp

    def test_duplicate_label_appears_in_sample(self):
        self._setup_misplaced_with_duplicate()
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True)
        MediaScanner(config=cfg, report=report).run_validate()
        self.assertIn("DUPLICATE", report.render())

    def test_target_count_appears_in_summary(self):
        self._setup_misplaced_with_duplicate()
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True)
        MediaScanner(config=cfg, report=report).run_validate()
        self.assertIn("already have a file at the target", report.render())

    def test_fix_removes_misplaced_duplicate(self):
        """With --fix, the misplaced copy should be deleted; the correctly
        placed copy must survive."""
        mp, kp = self._setup_misplaced_with_duplicate()
        cfg = make_cfg(photos_path=self.photos, do_validate=True, do_fix=True)
        MediaScanner(config=cfg).run_validate()
        self.assertFalse(mp.exists(), "Duplicate at wrong location should be deleted")
        self.assertTrue(kp.exists(),  "Keeper at correct location must remain")

    def test_collision_label_when_content_differs(self):
        """When target holds a DIFFERENT file (same name, different bytes),
        label it COLLISION not DUPLICATE."""
        wrong   = self.photos / "2020" / "01" / "01"
        correct = self.photos / "2023" / "08" / "14"
        wrong.mkdir(parents=True)
        correct.mkdir(parents=True)
        (wrong   / "IMG_2023-08-14.jpg").write_bytes(b"version A" * 30)
        (correct / "IMG_2023-08-14.jpg").write_bytes(b"version B" * 30)
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True)
        MediaScanner(config=cfg, report=report).run_validate()
        self.assertIn("COLLISION", report.render())
        self.assertNotIn("DUPLICATE", report.render())


# ── _image_exif_worker ────────────────────────────────────────────────────

class TestImageExifWorker(unittest.TestCase):
    """Tests for the module-level pool worker function.

    The worker now returns a (path_str, date_or_None) tuple so that
    imap_unordered results can be matched back to MediaFile objects by path
    regardless of arrival order.
    """

    def test_returns_tuple_for_nonexistent_file(self):
        path = "/nonexistent/file.jpg"
        path_out, d = _image_exif_worker(path)
        self.assertEqual(path_out, path)
        self.assertIsNone(d)

    def test_returns_tuple_for_non_image(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"not an image")
            fname = f.name
        self.addCleanup(os.unlink, fname)
        path_out, d = _image_exif_worker(fname)
        self.assertEqual(path_out, fname)
        self.assertIsNone(d)

    def test_returns_date_for_real_exif(self):
        """Worker returns (path, date) when EXIF is present."""
        import tempfile
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        from datetime import date as date_type

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            fname = f.name
        self.addCleanup(os.unlink, fname)

        img = Image.new("RGB", (10, 10))
        exif = img.getexif()
        exif[36867] = "2023:08:14 12:00:00"  # DateTimeOriginal
        img.save(fname, exif=exif.tobytes())

        path_out, d = _image_exif_worker(fname)
        self.assertEqual(path_out, fname)
        self.assertEqual(d, date_type(2023, 8, 14))


# ── Parallel mode ─────────────────────────────────────────────────────────

class TestParallelMode(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def test_parallel_mode_invokes_pool(self):
        """With parallel=True, _prefetch_image_dates must use a Pool."""
        dated_dir = self.photos / "2023" / "08" / "14"
        dated_dir.mkdir(parents=True)
        (dated_dir / "photo.jpg").write_bytes(b"fake")

        cfg = make_cfg(photos_path=self.photos, do_validate=True, parallel=True)
        scanner = MediaScanner(config=cfg)

        # Worker now returns (path_str, date_or_None) tuples so imap_unordered
        # results can be matched back to MediaFile objects by path.
        mock_pool_cls, mock_pool = make_mock_pool(
            imap_results=[(str(dated_dir / "photo.jpg"), None)]
        )
        with patch("deduper.scanner.Pool", mock_pool_cls):
            scanner.run_validate()

        mock_pool_cls.assert_called_once_with(cfg.pool_size)
        imap_args, imap_kwargs = mock_pool.imap_unordered.call_args
        self.assertIs(imap_args[0], _image_exif_worker)
        self.assertEqual(imap_args[1], [str(dated_dir / "photo.jpg")])
        self.assertEqual(imap_kwargs["chunksize"], cfg.pool_chunksize)

    def test_serial_mode_does_not_invoke_pool(self):
        """With parallel=False (default), Pool must not be used."""
        self.photos.mkdir(exist_ok=True)
        cfg = make_cfg(photos_path=self.photos, do_validate=True, parallel=False)
        scanner = MediaScanner(config=cfg)

        with patch("deduper.scanner.Pool") as mock_pool_cls:
            scanner.run_validate()

        mock_pool_cls.assert_not_called()

    def test_parallel_and_serial_same_result(self):
        """Parallel and serial modes must classify the same files."""
        wrong_dir = self.photos / "2020" / "01" / "01"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "IMG_2023-08-14.jpg").write_bytes(b"data")

        def _run(parallel: bool) -> str:
            report = Report()
            cfg = make_cfg(
                photos_path=self.photos, do_validate=True, parallel=parallel,
                pool_size=2, pool_chunksize=1,
            )
            MediaScanner(config=cfg, report=report).run_validate()
            return report.render()

        serial_out = _run(parallel=False)
        parallel_out = _run(parallel=True)
        # Both should report the same number of misplaced files
        self.assertIn("misplaced file", serial_out.lower())
        self.assertIn("misplaced file", parallel_out.lower())


# ── _image_exif_worker exception path ────────────────────────────────────

class TestImageExifWorkerException(unittest.TestCase):

    def test_returns_none_when_reader_raises(self):
        """Worker must return (path, None) — not propagate — when ImageExifReader raises."""
        path = "/some/photo.jpg"
        with patch("deduper.exif.ImageExifReader.get_date", side_effect=RuntimeError("boom")):
            path_out, d = _image_exif_worker(path)
        self.assertEqual(path_out, path)
        self.assertIsNone(d)


# ── MediaScanner.report property ─────────────────────────────────────────

class TestMediaScannerReport(unittest.TestCase):

    def test_report_property_returns_report_object(self):
        cfg = make_cfg()
        report = Report()
        scanner = MediaScanner(config=cfg, report=report)
        self.assertIs(scanner.report, report)


# ── run_dupes with no size-match candidates ───────────────────────────────

class TestRunDupesNoCandidates(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def test_no_size_matches_reports_no_duplicates(self):
        """When every file has a unique size, run_dupes must report 'No duplicates'."""
        d = self.photos / "2023" / "08" / "14"
        d.mkdir(parents=True)
        (d / "a.jpg").write_bytes(b"A" * 100)
        (d / "b.jpg").write_bytes(b"B" * 200)  # different size
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_dupes=True)
        MediaScanner(config=cfg, report=report).run_dupes()
        self.assertIn("No duplicates", report.render())


# ── run_validate with undated files ──────────────────────────────────────

class TestRunValidateUndated(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def test_undated_files_appear_in_report(self):
        """Files with no discernible date must appear in the undated sample."""
        d = self.photos / "misc"
        d.mkdir(parents=True)
        # Filename with no date; no EXIF → truly undated
        (d / "random_file.jpg").write_bytes(b"x" * 50)
        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True)
        MediaScanner(config=cfg, report=report).run_validate()
        text = report.render()
        self.assertIn("Undatable", text)


# ── _prefetch_image_dates edge cases ─────────────────────────────────────

class TestPrefetchImageDates(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def test_no_image_files_returns_early(self):
        """_prefetch_image_dates must be a no-op when the list has no ImageFiles."""
        from deduper.media import VideoFile
        cfg = make_cfg(photos_path=self.photos)
        scanner = MediaScanner(config=cfg)
        # Pass only VideoFile objects — method should return without calling Pool
        with patch("deduper.scanner.Pool") as mock_pool:
            scanner._prefetch_image_dates([])
        mock_pool.assert_not_called()

    def test_worker_result_populates_dated_cache(self):
        """When a pool worker returns a date, _dated on the ImageFile should be set."""
        dated_dir = self.photos / "2023" / "08" / "14"
        dated_dir.mkdir(parents=True)
        (dated_dir / "photo.jpg").write_bytes(b"fake")

        cfg = make_cfg(photos_path=self.photos, do_validate=True, parallel=True)
        scanner = MediaScanner(config=cfg)

        target_date = date(2023, 8, 14)
        photo_path = str(dated_dir / "photo.jpg")
        mock_pool_cls, mock_pool = make_mock_pool(
            imap_results=[(photo_path, target_date)]
        )
        with patch("deduper.scanner.Pool", mock_pool_cls):
            scanner.run_validate()

        # The worker's returned (path, date) tuple must be stored on the MediaFile
        mf = scanner._registry.get_or_create(dated_dir, "photo.jpg")
        self.assertEqual(mf._dated, target_date)


# ── _prefetch_video_dates ─────────────────────────────────────────────────

class TestPrefetchVideoDates(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def test_no_video_files_returns_early(self):
        """_prefetch_video_dates must be a no-op when there are no VideoFiles."""
        cfg = make_cfg(photos_path=self.photos)
        scanner = MediaScanner(config=cfg)
        from deduper.exif import ExifToolReader
        with patch.object(ExifToolReader, "_ensure_started") as mock_start:
            scanner._prefetch_video_dates([])
        mock_start.assert_not_called()

    def test_video_dates_populated_via_batch(self):
        """With a video file in parallel mode, _prefetch_video_dates uses
        get_date_batch and stores the result in mf._dated."""
        from deduper.media import VideoFile, _UNSET
        from deduper.exif import ExifToolReader
        videos = self.tmp / "videos"
        videos.mkdir()
        vid_dir = videos / "2021" / "03" / "15"
        vid_dir.mkdir(parents=True)
        (vid_dir / "clip.mp4").write_bytes(b"fake")

        cfg = make_cfg(videos_path=videos, do_validate=True, parallel=True)
        scanner = MediaScanner(config=cfg)

        target_date = date(2021, 3, 15)
        mock_pool_cls, _ = make_mock_pool(imap_results=[])
        with patch("deduper.scanner.Pool", mock_pool_cls), \
             patch.object(ExifToolReader, "_ensure_started"), \
             patch.object(ExifToolReader, "get_date_batch",
                          return_value=iter([(vid_dir / "clip.mp4", target_date)])) as mock_batch, \
             patch.object(ExifToolReader, "_terminate"):
            scanner.run_validate()

        # Verify get_date_batch was called with the video path and returned date was stored
        mock_batch.assert_called_once()
        (batch_paths,), _ = mock_batch.call_args
        self.assertIn(vid_dir / "clip.mp4", batch_paths)
        mf = scanner._registry.get_or_create(vid_dir, "clip.mp4")
        self.assertEqual(mf._dated, target_date)


# ── _find_dupes OSError handling ──────────────────────────────────────────

class TestFindDupesOSError(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_oserror_during_hash_is_skipped(self):
        """Files that raise OSError during hashing must be skipped, not crash."""
        mf1 = make_image_file(self.tmp, "a.jpg", content=b"data" * 100)
        mf2 = make_image_file(self.tmp, "b.jpg", content=b"data" * 100)

        # Patch the class-level hash property; restore it immediately on cleanup
        # to avoid contaminating later tests that create ImageFile instances.
        original_hash = type(mf2).hash
        self.addCleanup(setattr, type(mf2), "hash", original_hash)
        type(mf2).hash = property(lambda self: (_ for _ in ()).throw(OSError("no read")))

        scanner = MediaScanner(config=make_cfg())
        result = scanner._find_dupes([mf1, mf2])
        # mf1 hashes fine, mf2 errors out → no dupe group
        self.assertEqual(result, {})


# ── _hash_worker ─────────────────────────────────────────────────────────────

class TestHashWorker(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_returns_path_and_hash_for_existing_file(self):
        p = self.tmp / "file.jpg"
        p.write_bytes(b"content" * 100)
        path_out, hex_hash = _hash_worker((str(p), p.stat().st_size))
        self.assertEqual(path_out, str(p))
        self.assertIsNotNone(hex_hash)
        self.assertEqual(len(hex_hash), 32)  # MD5 hex digest length

    def test_returns_none_hash_on_oserror(self):
        path_out, hex_hash = _hash_worker(("/nonexistent/file.jpg", 100))
        self.assertEqual(path_out, "/nonexistent/file.jpg")
        self.assertIsNone(hex_hash)

    def test_same_content_same_hash(self):
        data = b"identical" * 200
        p1 = self.tmp / "a.jpg"
        p2 = self.tmp / "b.jpg"
        p1.write_bytes(data)
        p2.write_bytes(data)
        size = len(data)
        _, h1 = _hash_worker((str(p1), size))
        _, h2 = _hash_worker((str(p2), size))
        self.assertEqual(h1, h2)

    def test_different_content_different_hash(self):
        p1 = self.tmp / "a.jpg"
        p2 = self.tmp / "b.jpg"
        p1.write_bytes(b"AAA" * 200)
        p2.write_bytes(b"BBB" * 200)
        size = len(b"AAA" * 200)
        _, h1 = _hash_worker((str(p1), size))
        _, h2 = _hash_worker((str(p2), size))
        self.assertNotEqual(h1, h2)


# ── _find_dupes parallel hashing ─────────────────────────────────────────────

class TestFindDupesParallel(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_parallel_mode_uses_pool_for_hashing(self):
        """With parallel=True, _find_dupes must hash via Pool.imap_unordered."""
        data = b"same" * 200
        mf1 = make_image_file(self.tmp, "a.jpg", content=data)
        mf2 = make_image_file(self.tmp, "b.jpg", content=data)
        expected_hash = mf1.hash  # compute serially for comparison

        cfg = make_cfg(parallel=True)
        mock_pool_cls, mock_pool = make_mock_pool(imap_results=[
            (str(mf1.path), expected_hash),
            (str(mf2.path), expected_hash),
        ])
        scanner = MediaScanner(config=cfg)
        with patch("deduper.scanner.Pool", mock_pool_cls):
            result = scanner._find_dupes([mf1, mf2])

        mock_pool_cls.assert_called_once_with(cfg.pool_size)
        imap_args, imap_kwargs = mock_pool.imap_unordered.call_args
        self.assertIs(imap_args[0], _hash_worker)
        # Args passed to pool must be (path_str, size) tuples
        self.assertEqual(
            imap_args[1],
            [(str(mf1.path), mf1.size), (str(mf2.path), mf2.size)],
        )
        self.assertEqual(imap_kwargs["chunksize"], cfg.pool_chunksize)
        self.assertEqual(len(result), 1)

    def test_parallel_dupes_match_serial_result(self):
        """Parallel and serial _find_dupes must identify the same dupe groups."""
        data = b"identical" * 200
        mf1 = make_image_file(self.tmp, "a.jpg", content=data)
        mf2 = make_image_file(self.tmp, "b.jpg", content=data)

        serial_result = MediaScanner(config=make_cfg())._find_dupes([mf1, mf2])

        # Parallel path: mock pool returns real hashes so the logic is exercised
        expected_hash = mf1.hash
        mock_pool_cls, _ = make_mock_pool(imap_results=[
            (str(mf1.path), expected_hash),
            (str(mf2.path), expected_hash),
        ])
        with patch("deduper.scanner.Pool", mock_pool_cls):
            parallel_result = MediaScanner(config=make_cfg(parallel=True))._find_dupes([mf1, mf2])

        self.assertEqual(set(serial_result.keys()), set(parallel_result.keys()))


class TestRunValidateFixLoopContinuesOnError(unittest.TestCase):
    """Regression test for bug: the fix loop in run_validate has no try/except,
    so an OSError from any fix_date call propagates and aborts the whole batch.
    Files not yet processed are silently left in place; the report is lost."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        self.photos.mkdir()

    def test_fix_loop_continues_after_fix_date_raises(self):
        """If fix_date raises on one file, the remaining files must still be
        processed and an error message must appear in the report."""
        from deduper.media import MediaFile

        wrong_dir = self.photos / "2000" / "01" / "01"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "IMG_2023-01-01.jpg").write_bytes(b"a" * 50)
        (wrong_dir / "IMG_2023-06-15.jpg").write_bytes(b"b" * 50)

        report = Report()
        cfg = make_cfg(photos_path=self.photos, do_validate=True, do_fix=True)
        scanner = MediaScanner(config=cfg, report=report)

        call_count = [0]
        original_fix_date = MediaFile.fix_date

        def raise_then_succeed(self_mf, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("simulated disk full")
            return original_fix_date(self_mf, *args, **kwargs)

        with patch.object(MediaFile, "fix_date", raise_then_succeed):
            # Before fix: OSError propagates out of run_validate → test fails
            # with an unexpected exception.
            # After fix: error is caught and reported; loop continues.
            scanner.run_validate()

        # Both misplaced files must have been attempted.
        # Before fix: call_count stays at 1 (loop aborted after first raise).
        self.assertEqual(call_count[0], 2)

        # The report must contain a message about the error.
        self.assertIn("disk full", report.render())


if __name__ == "__main__":
    unittest.main()
