"""Tests for deduper.config — Config construction and validation."""

import contextlib
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.config import Config


class TestConfigDefaults(unittest.TestCase):

    def test_all_defaults(self):
        cfg = Config()
        self.assertIsNone(cfg.photos_path)
        self.assertIsNone(cfg.videos_path)
        self.assertIsNone(cfg.misdated_path)
        self.assertIsNone(cfg.import_path)
        self.assertFalse(cfg.do_load)
        self.assertFalse(cfg.do_dupes)
        self.assertFalse(cfg.do_validate)
        self.assertFalse(cfg.do_force)
        self.assertFalse(cfg.verbose)
        self.assertTrue(cfg.dryrun)
        self.assertFalse(cfg.parallel)
        self.assertEqual(cfg.pool_size, 4)
        self.assertEqual(cfg.pool_chunksize, 100)
        self.assertEqual(cfg.default_sep, "/")
        self.assertIsNone(cfg.report_to)
        self.assertEqual(cfg.diagnostic_paths, [])


class TestConfigExcludeDirsDefaults(unittest.TestCase):

    def test_picasaoriginals_excluded_by_default(self):
        cfg = Config()
        self.assertIn(".picasaoriginals", cfg.exclude_dirs)

    def test_synoeadir_excluded_by_default(self):
        cfg = Config()
        self.assertIn("@eaDir", cfg.exclude_dirs)


class TestConfigFromDefaults(unittest.TestCase):

    def test_override_single_field(self):
        cfg = Config.from_defaults(verbose=True, dryrun=True)
        self.assertTrue(cfg.verbose)
        self.assertTrue(cfg.dryrun)

    def test_override_paths(self):
        cfg = Config.from_defaults(photos_path=Path("/photos"), videos_path=Path("/videos"))
        self.assertEqual(cfg.photos_path, Path("/photos"))
        self.assertEqual(cfg.videos_path, Path("/videos"))


class TestConfigFromArgs(unittest.TestCase):

    def _make_args(self, **kwargs):
        # Action flags default to None (BooleanOptionalAction: not specified on CLI)
        defaults = dict(
            photos=None, videos=None, misdated=None, loadpath=None,
            load=None, dupes=None, validate=None, prune=None, fix=False, force=False,
            parallel=False, verbose=False,
            compareExif=False, exif=False, getDate=False, paths=[],
            config=None, report=None,
            exclude_dir=None, exclude_re=None, exiftool=None,
        )
        defaults.update(kwargs)
        return MagicMock(**defaults)

    def test_load_flag(self):
        args = self._make_args(load=True)
        cfg = Config.from_args(args)
        self.assertTrue(cfg.do_load)

    def test_loadpath_implies_load(self):
        args = self._make_args(loadpath="/import")
        cfg = Config.from_args(args)
        self.assertTrue(cfg.do_load)
        self.assertEqual(cfg.import_path, Path("/import"))

    def test_verbose_independent_of_dryrun(self):
        args = self._make_args(verbose=True)
        cfg = Config.from_args(args)
        self.assertTrue(cfg.verbose)
        self.assertTrue(cfg.dryrun)  # dryrun default unchanged

    def test_verbose_with_fix_runs(self):
        args = self._make_args(verbose=True, fix=True)
        cfg = Config.from_args(args)
        self.assertTrue(cfg.verbose)
        self.assertFalse(cfg.dryrun)

    def test_location_paths_converted(self):
        args = self._make_args(photos="/p", videos="/v", misdated="/m")
        cfg = Config.from_args(args)
        self.assertEqual(cfg.photos_path, Path("/p"))
        self.assertEqual(cfg.videos_path, Path("/v"))
        self.assertEqual(cfg.misdated_path, Path("/m"))

    def test_diagnostic_paths_converted(self):
        args = self._make_args(paths=["/a/b.jpg", "/c/d.mp4"])
        cfg = Config.from_args(args)
        self.assertEqual(cfg.diagnostic_paths, [Path("/a/b.jpg"), Path("/c/d.mp4")])

    def test_paths_empty_gives_empty_list(self):
        args = self._make_args(paths=[])
        cfg = Config.from_args(args)
        self.assertEqual(cfg.diagnostic_paths, [])

    def test_exif_flag_propagated(self):
        args = self._make_args(exif=True, paths=["/a/b.jpg"])
        cfg = Config.from_args(args)
        self.assertTrue(cfg.do_exif)

    def test_getdate_flag_propagated(self):
        args = self._make_args(getDate=True, paths=["/a/b.jpg"])
        cfg = Config.from_args(args)
        self.assertTrue(cfg.do_getdate)

    def test_exclude_dir_cli_overrides(self):
        args = self._make_args(exclude_dir=["Archive", "Trash"])
        cfg = Config.from_args(args)
        self.assertEqual(cfg.exclude_dirs, frozenset(["Archive", "Trash"]))

    def test_exclude_dir_cli_drops_config_file_dirs(self):
        """--exclude-dir replaces the config-file value entirely.
        Dirs set in the config file are silently dropped when CLI overrides.
        This is documented behaviour: the CLI value takes complete precedence."""
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False) as f:
            f.write("[LOCATIONS]\nexclude_dirs = ConfigDir, AnotherDir\n")
            ini_path = f.name
        self.addCleanup(__import__("os").unlink, ini_path)
        args = self._make_args(config=ini_path, exclude_dir=["CLIOnly"])
        cfg = Config.from_args(args)
        # CLIOnly is present; config-file dirs are gone
        self.assertIn("CLIOnly", cfg.exclude_dirs)
        self.assertNotIn("ConfigDir", cfg.exclude_dirs)
        self.assertNotIn("AnotherDir", cfg.exclude_dirs)

    def test_exclude_re_cli_overrides(self):
        args = self._make_args(exclude_re=r"\.bak$")
        cfg = Config.from_args(args)
        self.assertEqual(cfg.exclude_files_re, r"\.bak$")

    def test_config_flag_loads_ini_file(self):
        """--config should cause the ini file to be applied before CLI args."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False) as f:
            f.write("[LOCATIONS]\nphotos = /from/ini\n")
            ini_path = f.name
        self.addCleanup(__import__("os").unlink, ini_path)
        args = self._make_args(config=ini_path, load=True)
        cfg = Config.from_args(args)
        self.assertEqual(cfg.photos_path, Path("/from/ini"))

    def test_exiftool_flag_sets_executable(self):
        """--exiftool should propagate to cfg.exiftool_executable."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            exe_path = f.name
        self.addCleanup(os.unlink, exe_path)
        args = self._make_args(exiftool=exe_path)
        cfg = Config.from_args(args)
        self.assertEqual(cfg.exiftool_executable, exe_path)

    def test_exiftool_flag_rejects_missing_path(self):
        """--exiftool with a non-existent path should raise ValueError."""
        args = self._make_args(exiftool="/no/such/exiftool")
        with self.assertRaises(ValueError, msg="--exiftool"):
            Config.from_args(args)

    def test_exclude_re_invalid_regex_raises(self):
        """--exclude-re with a bad regex should raise ValueError."""
        args = self._make_args(exclude_re="[invalid(")
        with self.assertRaises(ValueError, msg="--exclude-re"):
            Config.from_args(args)

    def test_prune_flag_sets_do_prune(self):
        args = self._make_args(prune=True)
        cfg = Config.from_args(args)
        self.assertTrue(cfg.do_prune)

    def test_prune_default_false(self):
        args = self._make_args()
        cfg = Config.from_args(args)
        self.assertFalse(cfg.do_prune)

    def test_no_validate_suppresses_config_file_setting(self):
        """--no-validate (False) must override a config-file validate=true."""
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False) as f:
            f.write("[ACTIONS]\nvalidate = true\n")
            ini_path = f.name
        self.addCleanup(__import__("os").unlink, ini_path)
        args = self._make_args(config=ini_path, validate=False)
        cfg = Config.from_args(args)
        self.assertFalse(cfg.do_validate)

    def test_no_dupes_suppresses_config_file_setting(self):
        """--no-dupes (False) must override a config-file dupes=true."""
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False) as f:
            f.write("[ACTIONS]\ndupes = true\n")
            ini_path = f.name
        self.addCleanup(__import__("os").unlink, ini_path)
        args = self._make_args(config=ini_path, dupes=False)
        cfg = Config.from_args(args)
        self.assertFalse(cfg.do_dupes)

    def test_no_load_suppresses_config_file_setting(self):
        """--no-load (False) must override a config-file import=true."""
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False) as f:
            f.write("[ACTIONS]\nimport = true\n")
            ini_path = f.name
        self.addCleanup(__import__("os").unlink, ini_path)
        args = self._make_args(config=ini_path, load=False)
        cfg = Config.from_args(args)
        self.assertFalse(cfg.do_load)

    def test_cli_validate_true_overrides_config_false(self):
        """--validate (True) must enable validate even if config-file says false."""
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False) as f:
            f.write("[ACTIONS]\nvalidate = false\n")
            ini_path = f.name
        self.addCleanup(__import__("os").unlink, ini_path)
        args = self._make_args(config=ini_path, validate=True)
        cfg = Config.from_args(args)
        self.assertTrue(cfg.do_validate)


class TestConfigFromFile(unittest.TestCase):

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def _write_ini(self, content: str) -> str:
        p = self.tmp / "test.ini"
        p.write_text(content)
        return str(p)

    def test_locations_loaded(self):
        ini = self._write_ini("""
[LOCATIONS]
photos = /mnt/photos
videos = /mnt/videos
misdated = /mnt/misc
import = /mnt/import
""")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertEqual(cfg.photos_path, Path("/mnt/photos"))
        self.assertEqual(cfg.videos_path, Path("/mnt/videos"))
        self.assertEqual(cfg.misdated_path, Path("/mnt/misc"))
        self.assertEqual(cfg.import_path, Path("/mnt/import"))

    def test_globals_loaded(self):
        ini = self._write_ini("""
[GLOBALS]
VERBOSE = true
DEFAULT_SEP = -
POOL_SIZE = 4
POOL_CHUNKSIZE = 20
""")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertTrue(cfg.verbose)
        self.assertEqual(cfg.default_sep, "-")
        self.assertEqual(cfg.pool_size, 4)
        self.assertEqual(cfg.pool_chunksize, 20)

    def test_watchdog_globals_loaded(self):
        ini = self._write_ini("""
[GLOBALS]
hash_timeout = 30.0
slow_extensions = tif, tiff, .cr2
slow_ext_timeout = 240.0
""")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertEqual(cfg.hash_timeout, 30.0)
        self.assertEqual(cfg.slow_extensions, frozenset({"tif", "tiff", "cr2"}))
        self.assertEqual(cfg.slow_ext_timeout, 240.0)

    def test_actions_loaded(self):
        ini = self._write_ini("""
[ACTIONS]
import = true
dupes = true
validate = false
prune = true
""")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertTrue(cfg.do_load)
        self.assertTrue(cfg.do_dupes)
        self.assertFalse(cfg.do_validate)
        self.assertTrue(cfg.do_prune)

    def test_missing_file_does_not_raise(self):
        cfg = Config()
        cfg._apply_config_file(str(self.tmp / "nonexistent.ini"))  # Should not raise

    def test_empty_ini_leaves_defaults(self):
        ini = self._write_ini("")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertIsNone(cfg.photos_path)

    def test_exclude_dirs_loaded(self):
        ini = self._write_ini("""
[LOCATIONS]
exclude_dirs = MyArchive, Backup, Temp
""")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertEqual(cfg.exclude_dirs, frozenset(["MyArchive", "Backup", "Temp"]))

    def test_exclude_re_loaded(self):
        ini = self._write_ini(r"""
[LOCATIONS]
exclude_re = \.bak$|\.tmp$
""")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertEqual(cfg.exclude_files_re, r"\.bak$|\.tmp$")

    def test_exiftool_path_loaded_from_globals(self):
        ini = self._write_ini("""
[GLOBALS]
exiftool_path = /opt/Image-ExifTool/exiftool
""")
        cfg = Config()
        cfg._apply_config_file(ini)
        self.assertEqual(cfg.exiftool_executable, "/opt/Image-ExifTool/exiftool")


class TestConfigDateStr(unittest.TestCase):

    def test_slash_sep(self):
        cfg = Config(default_sep="/")
        self.assertEqual(cfg.date_str(date(2023, 8, 14)), "2023/08/14")

    def test_hyphen_sep(self):
        cfg = Config(default_sep="-")
        self.assertEqual(cfg.date_str(date(2023, 8, 14)), "2023-08-14")

    def test_none_date(self):
        cfg = Config(default_sep="/")
        self.assertEqual(cfg.date_str(None), "0000/00/00")


class TestConfigValidateForAction(unittest.TestCase):

    def test_no_action_raises(self):
        cfg = Config()
        with self.assertRaises(ValueError):
            cfg.validate_for_action()

    def test_load_action_ok(self):
        cfg = Config(do_load=True)
        cfg.validate_for_action()  # Should not raise

    def test_dupes_action_ok(self):
        cfg = Config(do_dupes=True)
        cfg.validate_for_action()

    def test_validate_action_ok(self):
        cfg = Config(do_validate=True)
        cfg.validate_for_action()

    def test_diagnostic_paths_ok(self):
        cfg = Config(diagnostic_paths=[Path("/some/file.jpg")])
        cfg.validate_for_action()

    def test_import_inside_photos_raises(self):
        """validate_for_action must reject an import_path that lives inside photos_path.

        Bug: no path-overlap check exists, so --load with an inbox inside the
        library scans existing library files as imports and may relocate them.
        """
        cfg = Config(
            do_load=True,
            photos_path=Path("/photos"),
            import_path=Path("/photos/inbox"),
        )
        # Before fix: does NOT raise → bug is present.
        # After fix:  raises ValueError.
        with self.assertRaises(ValueError):
            cfg.validate_for_action()

    def test_import_inside_videos_raises(self):
        cfg = Config(
            do_load=True,
            videos_path=Path("/videos"),
            import_path=Path("/videos/inbox"),
        )
        with self.assertRaises(ValueError):
            cfg.validate_for_action()

    def test_library_inside_import_raises(self):
        """The overlap check must work in both directions."""
        cfg = Config(
            do_load=True,
            photos_path=Path("/inbox/photos"),
            import_path=Path("/inbox"),
        )
        with self.assertRaises(ValueError):
            cfg.validate_for_action()

    def test_disjoint_import_and_library_ok(self):
        """Separate inbox and library paths must not raise."""
        cfg = Config(
            do_load=True,
            photos_path=Path("/photos"),
            import_path=Path("/inbox"),
        )
        cfg.validate_for_action()  # Must not raise

    def test_prune_action_ok(self):
        cfg = Config(do_prune=True)
        cfg.validate_for_action()  # Must not raise

    def test_photos_path_dated_suffix_raises(self):
        """photos_path ending in YYYY/MM/DD must be rejected — it is a dated subdir."""
        cfg = Config(do_validate=True, photos_path=Path("/photos/2015/11/25"))
        with self.assertRaises(ValueError, msg="--photos"):
            cfg.validate_for_action()

    def test_videos_path_dated_suffix_raises(self):
        """videos_path ending in YYYY/MM/DD must be rejected."""
        cfg = Config(do_validate=True, videos_path=Path("/videos/0000/00/00"))
        with self.assertRaises(ValueError, msg="--videos"):
            cfg.validate_for_action()

    def test_photos_path_dated_hyphen_suffix_raises(self):
        """Hyphen-separated YYYY-MM-DD suffix must also be rejected."""
        cfg = Config(do_validate=True, photos_path=Path("/photos/2023-08-14"))
        with self.assertRaises(ValueError, msg="--photos"):
            cfg.validate_for_action()

    def test_library_root_without_date_suffix_ok(self):
        """A normal library root must pass validation."""
        cfg = Config(do_validate=True, photos_path=Path("/photos/by date"))
        cfg.validate_for_action()  # Must not raise


if __name__ == "__main__":
    unittest.main()
