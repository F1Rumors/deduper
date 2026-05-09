"""Tests for deduper.cli — command-line interface."""

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.cli import build_parser, main


class TestBuildParser(unittest.TestCase):

    def _parse(self, *args):
        return build_parser().parse_args(args)

    def test_defaults(self):
        args = self._parse("--load")
        self.assertTrue(args.load)
        self.assertFalse(args.dupes)
        self.assertFalse(args.validate)
        self.assertFalse(args.fix)
        self.assertFalse(args.debug)
        self.assertFalse(args.dryrun)
        self.assertFalse(args.parallel)
        self.assertIsNone(args.photos)
        self.assertIsNone(args.report)

    def test_all_flags(self):
        args = self._parse(
            "--load", "--dupes", "--validate", "--fix", "--force",
            "--parallel", "--debug", "--dryrun", "--compareExif",
            "--photos", "/p", "--videos", "/v", "--misdated", "/m",
            "--loadpath", "/i", "--report", "a@b.com",
        )
        self.assertTrue(args.load)
        self.assertTrue(args.dupes)
        self.assertTrue(args.validate)
        self.assertTrue(args.fix)
        self.assertTrue(args.force)
        self.assertTrue(args.parallel)
        self.assertTrue(args.debug)
        self.assertTrue(args.dryrun)
        self.assertTrue(args.compareExif)
        self.assertEqual(args.photos, "/p")
        self.assertEqual(args.videos, "/v")
        self.assertEqual(args.misdated, "/m")
        self.assertEqual(args.loadpath, "/i")
        self.assertEqual(args.report, "a@b.com")

    def test_exif_flag(self):
        args = self._parse("--exif", "/a.jpg", "/b.jpg")
        self.assertTrue(args.exif)
        self.assertEqual(args.paths, ["/a.jpg", "/b.jpg"])

    def test_getdate_flag(self):
        args = self._parse("--getDate", "/a.jpg")
        self.assertTrue(args.getDate)
        self.assertEqual(args.paths, ["/a.jpg"])

    def test_config_arg(self):
        args = self._parse("--load", "--config", "myconfig.ini")
        self.assertEqual(args.config, "myconfig.ini")

    def test_stdout_flag(self):
        args = self._parse("--load", "--stdout")
        self.assertTrue(args.stdout)

    def test_stdout_false_by_default(self):
        args = self._parse("--load")
        self.assertFalse(args.stdout)


class TestTee(unittest.TestCase):
    """_Tee writes to both the original stdout and a file."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_tee_writes_to_file(self):
        from deduper.cli import _Tee
        log = self.tmp / "out.log"
        tee = _Tee(log)
        try:
            print("hello tee")
        finally:
            tee.close()
        self.assertIn("hello tee", log.read_text())

    def test_tee_restores_stdout(self):
        from deduper.cli import _Tee
        original = sys.stdout
        tee = _Tee(self.tmp / "out.log")
        tee.close()
        self.assertIs(sys.stdout, original)

    def test_stdout_flag_suppresses_log_file(self):
        """--stdout must not create a log file."""
        inbox = self.tmp / "inbox"
        inbox.mkdir()
        photos = self.tmp / "photos"
        photos.mkdir()
        (inbox / "IMG_2023-08-14.jpg").write_bytes(b"data")
        import os
        orig_dir = os.getcwd()
        os.chdir(self.tmp)
        try:
            main([
                "--loadpath", str(inbox),
                "--photos", str(photos),
                "--dryrun", "--stdout",
            ])
        finally:
            os.chdir(orig_dir)
        log_files = list(self.tmp.glob("dedupe.*.log"))
        self.assertEqual(log_files, [], "No log file should be created with --stdout")

    def test_no_stdout_flag_creates_log_file(self):
        """Without --stdout, a timestamped log file is created in cwd."""
        inbox = self.tmp / "inbox"
        inbox.mkdir()
        photos = self.tmp / "photos"
        photos.mkdir()
        (inbox / "IMG_2023-08-14.jpg").write_bytes(b"data")
        import os
        orig_dir = os.getcwd()
        os.chdir(self.tmp)
        try:
            main([
                "--loadpath", str(inbox),
                "--photos", str(photos),
                "--dryrun",
            ])
        finally:
            os.chdir(orig_dir)
        log_files = list(self.tmp.glob("dedupe.*.log"))
        self.assertEqual(len(log_files), 1)


class TestMainNoAction(unittest.TestCase):
    """main() exits with an error when no action is specified."""

    def test_no_action_exits(self):
        with self.assertRaises(SystemExit) as cm:
            main(["--photos", "/p"])
        self.assertNotEqual(cm.exception.code, 0)


class TestMainExifPath(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_exif_mode_exits_zero(self):
        f = self.tmp / "test.jpg"
        f.write_bytes(b"not a real image")
        with patch("deduper.cli.cmd_exif") as mock_exif:
            result = main(["--exif", str(f)])
        self.assertEqual(result, 0)
        mock_exif.assert_called_once()
        (paths, _cfg), _ = mock_exif.call_args
        self.assertEqual(paths, [f])


class TestMainLoad(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.inbox = self.tmp / "inbox"
        self.photos = self.tmp / "photos"
        self.inbox.mkdir()
        self.photos.mkdir()

    def test_load_dryrun_returns_zero(self):
        (self.inbox / "IMG_2023-08-14.jpg").write_bytes(b"data")
        result = main([
            "--loadpath", str(self.inbox),
            "--photos", str(self.photos),
            "--dryrun",
        ])
        self.assertEqual(result, 0)

    def test_load_imports_file(self):
        (self.inbox / "IMG_2023-08-14.jpg").write_bytes(b"data")
        result = main([
            "--loadpath", str(self.inbox),
            "--photos", str(self.photos),
            "--fix",
        ])
        self.assertEqual(result, 0)
        expected = self.photos / "2023" / "08" / "14" / "IMG_2023-08-14.jpg"
        self.assertTrue(expected.exists())


class TestMainDupes(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"
        d = self.photos / "2023" / "08" / "14"
        d.mkdir(parents=True)
        # Two identical files
        (d / "a.jpg").write_bytes(b"same" * 500)
        (d / "b.jpg").write_bytes(b"same" * 500)

    def test_dupes_reported_without_fix(self):
        result = main([
            "--dupes",
            "--photos", str(self.photos),
        ])
        self.assertEqual(result, 0)

    def test_dupes_removed_with_fix(self):
        result = main([
            "--dupes", "--fix",
            "--photos", str(self.photos),
        ])
        self.assertEqual(result, 0)
        d = self.photos / "2023" / "08" / "14"
        existing = list(d.glob("*.jpg"))
        self.assertEqual(len(existing), 1)


class TestMainValidate(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.photos = self.tmp / "photos"

    def test_validate_clean_tree(self):
        d = self.photos / "2023" / "08" / "14"
        d.mkdir(parents=True)
        # File that matches its path date (via filename)
        (d / "IMG_2023-08-14.jpg").write_bytes(b"data")
        result = main([
            "--validate",
            "--photos", str(self.photos),
        ])
        self.assertEqual(result, 0)


# ── Diagnostic commands — missing file handling ───────────────────────────

class TestCmdDiagnosticMissingFile(unittest.TestCase):
    """Each diagnostic command must print a clear 'file not found' message
    and continue (not raise) when a supplied path does not exist."""

    def setUp(self):
        from deduper.config import Config
        self.cfg = Config.from_defaults()
        # A path that is guaranteed not to exist
        self.missing = Path("/no/such/file_deduper_test_xyz.jpg")

    def _captured_print(self, fn, *args, **kwargs):
        """Call fn(*args, **kwargs) and return all text sent to print()."""
        lines = []
        with patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(str(x) for x in a))):
            fn(*args, **kwargs)
        return "\n".join(lines)

    def test_cmd_exif_missing_file(self):
        from deduper.cli import cmd_exif
        from deduper.exif import ExifToolReader
        with patch.object(ExifToolReader, "_ensure_started"):
            output = self._captured_print(cmd_exif, [self.missing], self.cfg)
        self.assertIn("file not found", output)

    def test_cmd_exif_continues_after_missing_file(self):
        """A missing file must not prevent subsequent valid files being processed."""
        import tempfile
        from deduper.cli import cmd_exif
        from deduper.exif import ExifToolReader
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"data")
            existing = Path(f.name)
        self.addCleanup(existing.unlink, missing_ok=True)
        processed = []
        def fake_get_raw(self_inner, path):
            processed.append(path)
            return {}
        with patch.object(ExifToolReader, "_ensure_started"), \
             patch.object(ExifToolReader, "get_raw", fake_get_raw), \
             patch("builtins.print"):
            cmd_exif([self.missing, existing], self.cfg)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0], existing)

    def test_cmd_get_date_missing_file(self):
        from deduper.cli import cmd_get_date
        output = self._captured_print(cmd_get_date, [self.missing], self.cfg)
        self.assertIn("file not found", output)

    def test_cmd_get_date_continues_after_missing_file(self):
        """Processing must continue to subsequent paths after a missing file."""
        import tempfile
        from deduper.cli import cmd_get_date
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"data")
            existing = Path(f.name)
        self.addCleanup(existing.unlink, missing_ok=True)
        lines = []
        with patch("builtins.print", side_effect=lambda *a, **k: lines.append(str(a[0]) if a else "")):
            cmd_get_date([self.missing, existing], self.cfg)
        full = "\n".join(lines)
        # The existing file's path should appear in output
        self.assertIn(existing.stem, full)

    def test_cmd_compare_exif_missing_file(self):
        from deduper.cli import cmd_compare_exif
        from deduper.exif import ExifToolReader
        with patch.object(ExifToolReader, "_ensure_started"):
            output = self._captured_print(cmd_compare_exif, [self.missing], self.cfg)
        self.assertIn("file not found", output)


# ── cmd_compare_exif — existing file paths ────────────────────────────────

class TestCmdCompareExifHappyPath(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        from deduper.config import Config
        self.cfg = Config.from_defaults()

    def test_no_output_when_dates_match(self):
        """No date-mismatch line when Pillow and ExifTool agree."""
        from datetime import date as date_type
        from deduper.cli import cmd_compare_exif
        from deduper.exif import ExifToolReader, ImageExifReader
        f = self.tmp / "photo.jpg"
        f.write_bytes(b"data")
        d = date_type(2023, 8, 14)
        printed = []
        with patch.object(ExifToolReader, "_ensure_started"), \
             patch.object(ExifToolReader, "get_date", return_value=d), \
             patch.object(ImageExifReader, "get_date", return_value=d), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(a)):
            cmd_compare_exif([f], self.cfg)
        self.assertEqual(printed, [])

    def test_prints_diff_when_dates_differ(self):
        """Prints mismatch line when Pillow and ExifTool disagree."""
        from datetime import date as date_type
        from deduper.cli import cmd_compare_exif
        from deduper.exif import ExifToolReader, ImageExifReader
        f = self.tmp / "photo.jpg"
        f.write_bytes(b"data")
        with patch.object(ExifToolReader, "_ensure_started"), \
             patch.object(ExifToolReader, "get_date", return_value=date_type(2023, 8, 14)), \
             patch.object(ImageExifReader, "get_date", return_value=date_type(2020, 1, 1)), \
             patch("builtins.print") as mock_print:
            cmd_compare_exif([f], self.cfg)
        mock_print.assert_called_once()
        output = " ".join(str(a) for a in mock_print.call_args[0])
        self.assertIn("Pillow=", output)
        self.assertIn("ExifTool=", output)


# ── cmd_get_date — unrecognized and video file paths ──────────────────────

class TestCmdGetDateFileTypes(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        from deduper.config import Config
        self.cfg = Config.from_defaults()

    def test_unrecognized_file_skipped(self):
        """A .txt file (not media) should print 'not a recognised media file'."""
        from deduper.cli import cmd_get_date
        f = self.tmp / "notes.txt"
        f.write_bytes(b"hello")
        printed = []
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            cmd_get_date([f], self.cfg)
        self.assertTrue(any("not a recognised media file" in line for line in printed))

    def test_video_file_uses_exiftool(self):
        """cmd_get_date must use ExifToolReader (not ImageExifReader) for video."""
        from datetime import date as date_type
        from deduper.cli import cmd_get_date
        from deduper.exif import ExifToolReader
        f = self.tmp / "clip.mp4"
        f.write_bytes(b"data")
        with patch.object(ExifToolReader, "_ensure_started"), \
             patch.object(ExifToolReader, "get_date", return_value=date_type(2023, 8, 14)) as mock_get, \
             patch("builtins.print"):
            cmd_get_date([f], self.cfg)
        self.assertTrue(mock_get.called)
        # ExifToolReader.get_date should have been invoked with the resolved path
        self.assertIn(f.resolve(), [call.args[0] for call in mock_get.call_args_list])


# ── main() — diagnostic multi-flag and error paths ────────────────────────

class TestMainDiagnosticMultipleFlags(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.f = self.tmp / "photo.jpg"
        self.f.write_bytes(b"data")

    def test_exif_and_compare_exif_prints_separator(self):
        """--exif then --compareExif should print a blank separator line."""
        with patch("deduper.cli.cmd_exif"), \
             patch("deduper.cli.cmd_compare_exif"), \
             patch("builtins.print") as mock_print:
            result = main(["--exif", "--compareExif", str(self.f)])
        self.assertEqual(result, 0)
        # A blank print() separates the two command outputs
        self.assertIn((), [call.args for call in mock_print.call_args_list])

    def test_exif_and_getdate_prints_separator(self):
        """--exif then --getDate should print a blank separator line."""
        with patch("deduper.cli.cmd_exif"), \
             patch("deduper.cli.cmd_get_date"), \
             patch("builtins.print") as mock_print:
            result = main(["--exif", "--getDate", str(self.f)])
        self.assertEqual(result, 0)
        self.assertIn((), [call.args for call in mock_print.call_args_list])

    def test_default_diagnostic_invokes_cmd_exif(self):
        """Paths with no diagnostic flag should default to cmd_exif."""
        with patch("deduper.cli.cmd_exif") as mock_exif:
            result = main([str(self.f)])
        self.assertEqual(result, 0)
        mock_exif.assert_called_once()
        (paths, _cfg), _ = mock_exif.call_args
        self.assertEqual(paths, [self.f])

    def test_keyboard_interrupt_returns_1(self):
        """KeyboardInterrupt during a scan run returns exit code 1."""
        inbox = self.tmp / "inbox"
        inbox.mkdir()
        photos = self.tmp / "photos"
        photos.mkdir()
        with patch("deduper.scanner.MediaScanner.run_load", side_effect=KeyboardInterrupt), \
             patch("builtins.print"):
            result = main([
                "--load",
                "--loadpath", str(inbox),
                "--photos", str(photos),
            ])
        self.assertEqual(result, 1)

    def test_unexpected_exception_returns_2(self):
        """An unhandled exception during a scan run returns exit code 2."""
        inbox = self.tmp / "inbox"
        inbox.mkdir()
        photos = self.tmp / "photos"
        photos.mkdir()
        with patch("deduper.scanner.MediaScanner.run_load", side_effect=RuntimeError("boom")), \
             patch("builtins.print"):
            result = main([
                "--load",
                "--loadpath", str(inbox),
                "--photos", str(photos),
            ])
        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
