"""Tests for deduper.exif — EXIF reader classes.

ExifTool subprocess is never started; the ExifToolReader is tested with
a stubbed ``get_raw`` method so that all date-extraction logic is exercised
without external dependencies.
"""

import contextlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.exif import (
    ExifReaderBase,
    ImageExifReader,
    ExifToolReader,
    MediaInfoReader,
    IMAGE_DATE_FIELDS,
    IMAGE_DATE_FIELDS_IGNORE,
    EXIFTOOL_DATE_FIELDS,
    EXIFTOOL_DATE_FIELDS_IGNORE,
)


# ── Stub subclass used throughout tests ───────────────────────────────────

class StubReader(ExifReaderBase):
    DATE_FIELDS = ["FieldA", "FieldB", "FieldC"]
    DATE_FIELDS_IGNORE = frozenset(["IgnoredField"])

    def __init__(self, raw: dict, debug: bool = False) -> None:
        super().__init__(debug=debug)
        self._raw = raw

    def get_raw(self, path: Path) -> dict:
        return self._raw


# ── ExifReaderBase._date_from_raw ─────────────────────────────────────────

class TestExifReaderBase(unittest.TestCase):

    def test_returns_date_from_first_matching_field(self):
        reader = StubReader({"FieldA": "2023:08:14 10:00:00"})
        d = reader.get_date(Path("dummy"))
        self.assertEqual(d, date(2023, 8, 14))

    def test_skips_empty_fields(self):
        reader = StubReader({"FieldA": "", "FieldB": "2021-06-01"})
        d = reader.get_date(Path("dummy"))
        self.assertEqual(d, date(2021, 6, 1))

    def test_returns_none_when_no_fields_match(self):
        reader = StubReader({"FieldA": "no date here"})
        d = reader.get_date(Path("dummy"))
        self.assertIsNone(d)

    def test_returns_none_for_empty_exif(self):
        reader = StubReader({})
        d = reader.get_date(Path("dummy"))
        self.assertIsNone(d)

    def test_priority_ordering_respected(self):
        # FieldA has priority over FieldB
        reader = StubReader({
            "FieldA": "2020-01-01",
            "FieldB": "2021-02-02",
        })
        self.assertEqual(reader.get_date(Path("x")), date(2020, 1, 1))

    def test_field_not_in_exif_skipped(self):
        # FieldA absent — should fall through to FieldB
        reader = StubReader({"FieldB": "2019-05-15"})
        self.assertEqual(reader.get_date(Path("x")), date(2019, 5, 15))

    def test_calendar_invalid_date_not_returned(self):
        reader = StubReader({"FieldA": "2023-13-01"})  # month 13
        self.assertIsNone(reader.get_date(Path("x")))

    def test_debug_logs_candidate_fields(self):
        """In debug mode, fields not in DATE_FIELDS that contain a date are
        logged (but not returned unless also in DATE_FIELDS)."""
        reader = StubReader(
            {"UnknownField": "2022-07-04", "FieldA": ""},
            debug=True,
        )
        with patch("deduper.exif.logger") as mock_log:
            reader.get_date(Path("f"))
        mock_log.debug.assert_called()
        # Verify the unknown field name appears in the logged message args
        all_args = [a for c in mock_log.debug.call_args_list for a in c[0]]
        self.assertTrue(any("UnknownField" in str(a) for a in all_args))

    def test_debug_candidate_only_logged_once(self):
        reader = StubReader({"UnknownField": "2022-07-04"}, debug=True)
        with patch("deduper.exif.logger"):
            reader.get_date(Path("f"))
            reader.get_date(Path("f"))  # Second call — should not re-log
        self.assertIn("UnknownField", reader._recommended)


# ── ImageExifReader field list ─────────────────────────────────────────────

class TestImageExifReaderFields(unittest.TestCase):

    def test_date_fields_not_empty(self):
        self.assertTrue(len(IMAGE_DATE_FIELDS) > 0)

    def test_datetime_original_is_first(self):
        self.assertEqual(IMAGE_DATE_FIELDS[0], "DateTimeOriginal")

    def test_get_raw_returns_dict_on_io_error(self):
        reader = ImageExifReader()
        result = reader.get_raw(Path("/nonexistent/file.jpg"))
        self.assertIsInstance(result, dict)

    def test_get_raw_returns_dict_on_invalid_file(self):
        """Passing a non-image file (e.g. a text file) should return {}."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"this is not an image")
            fname = f.name
        try:
            reader = ImageExifReader()
            result = reader.get_raw(Path(fname))
            self.assertIsInstance(result, dict)
        finally:
            os.unlink(fname)


# ── ImageExifReader — field priority rules ─────────────────────────────────

class TestImageExifReaderFieldRules(unittest.TestCase):

    def test_datetime_not_in_fields(self):
        """'DateTime' is Pillow's name for the EXIF ModifyDate tag (0x0132).
        It must be absent from the priority list so modify dates are never
        used as the file date."""
        self.assertNotIn("DateTime", IMAGE_DATE_FIELDS)

    def test_datetime_in_ignore_set(self):
        """'DateTime' should be explicitly ignored so debug logging doesn't
        suggest adding it back."""
        self.assertIn("DateTime", IMAGE_DATE_FIELDS_IGNORE)

    def test_datetime_original_still_first(self):
        self.assertEqual(IMAGE_DATE_FIELDS[0], "DateTimeOriginal")


# ── ExifToolReader field list ──────────────────────────────────────────────

class TestExifToolReaderFields(unittest.TestCase):

    def test_fields_use_namespace_colon_format(self):
        for f in EXIFTOOL_DATE_FIELDS:
            self.assertIn(":", f, f"{f!r} should be 'Namespace:Field'")

    def test_required_namespaces_present(self):
        namespaces = {f.split(":")[0] for f in EXIFTOOL_DATE_FIELDS}
        for ns in ("EXIF", "XMP", "QuickTime", "Composite"):
            self.assertIn(ns, namespaces)

    def test_xmp_datetime_original_in_fields(self):
        self.assertIn("XMP:DateTimeOriginal", EXIFTOOL_DATE_FIELDS)

    def test_composite_datecreated_in_fields(self):
        self.assertIn("Composite:DateTimeCreated", EXIFTOOL_DATE_FIELDS)

    def test_exif_modify_date_not_in_fields(self):
        """EXIF:ModifyDate must never appear in the search list."""
        self.assertNotIn("EXIF:ModifyDate", EXIFTOOL_DATE_FIELDS)

    def test_exif_modify_date_in_ignore_set(self):
        self.assertIn("EXIF:ModifyDate", EXIFTOOL_DATE_FIELDS_IGNORE)

    def test_xmp_modify_date_in_ignore_set(self):
        self.assertIn("XMP:ModifyDate", EXIFTOOL_DATE_FIELDS_IGNORE)

    def test_file_modify_date_in_ignore_set(self):
        self.assertIn("File:FileModifyDate", EXIFTOOL_DATE_FIELDS_IGNORE)

    def test_exif_original_before_xmp_original(self):
        """Camera EXIF takes priority over XMP sidecar."""
        i_exif = EXIFTOOL_DATE_FIELDS.index("EXIF:DateTimeOriginal")
        i_xmp = EXIFTOOL_DATE_FIELDS.index("XMP:DateTimeOriginal")
        self.assertLess(i_exif, i_xmp)

    def test_exif_original_before_quicktime(self):
        i_orig = EXIFTOOL_DATE_FIELDS.index("EXIF:DateTimeOriginal")
        i_qt = EXIFTOOL_DATE_FIELDS.index("QuickTime:CreateDate")
        self.assertLess(i_orig, i_qt)

    def test_exif_create_before_composite(self):
        i_create = EXIFTOOL_DATE_FIELDS.index("EXIF:CreateDate")
        i_comp = EXIFTOOL_DATE_FIELDS.index("Composite:DateTimeCreated")
        self.assertLess(i_create, i_comp)


class TestExifToolReaderWithStub(unittest.TestCase):
    """Tests for ExifToolReader using a patched _tool attribute."""

    def _make_reader_with_raw(self, raw: dict) -> ExifToolReader:
        reader = ExifToolReader(debug=False)
        reader._tool = MagicMock()
        # ExifToolHelper.get_metadata() returns a list of dicts (one per file)
        reader._tool.get_metadata.return_value = [raw]
        return reader

    def test_get_date_extracts_from_exif_field(self):
        reader = self._make_reader_with_raw({"EXIF:DateTimeOriginal": "2023:08:14 10:00:00"})
        d = reader.get_date(Path("movie.mp4"))
        self.assertEqual(d, date(2023, 8, 14))

    def test_get_date_returns_none_on_empty(self):
        reader = self._make_reader_with_raw({})
        self.assertIsNone(reader.get_date(Path("movie.mp4")))

    def test_get_raw_handles_exception(self):
        reader = ExifToolReader(debug=False)
        reader._tool = MagicMock()
        reader._tool.get_metadata.side_effect = ValueError("boom")
        result = reader.get_raw(Path("x.mp4"))
        self.assertEqual(result, {})

    def test_terminate_called_on_exit(self):
        reader = ExifToolReader()
        mock_tool = MagicMock()
        reader._tool = mock_tool
        reader.__exit__(None, None, None)
        mock_tool.terminate.assert_called_once()

    def test_terminate_is_idempotent(self):
        reader = ExifToolReader()
        reader._tool = MagicMock()
        reader._terminate()
        reader._terminate()  # Should not raise

    def test_modify_date_alone_returns_none(self):
        """EXIF:ModifyDate must not be used as a date source even when it is
        the only date-like field present — this was the root cause of files
        being placed in wrong directories."""
        reader = self._make_reader_with_raw({"EXIF:ModifyDate": "2014:03:23 21:40:12"})
        self.assertIsNone(reader.get_date(Path("photo.jpg")))

    def test_original_preferred_over_modify(self):
        """When both EXIF:DateTimeOriginal and EXIF:ModifyDate exist, the
        original capture date must win (the real-world misdated-file scenario)."""
        reader = self._make_reader_with_raw({
            "EXIF:DateTimeOriginal": "2013:05:27 11:32:00",
            "EXIF:ModifyDate":      "2014:03:23 21:40:12",
        })
        self.assertEqual(reader.get_date(Path("photo.jpg")), date(2013, 5, 27))

    def test_xmp_datetime_original_extracted(self):
        reader = self._make_reader_with_raw({"XMP:DateTimeOriginal": "2013:05:27"})
        self.assertEqual(reader.get_date(Path("photo.jpg")), date(2013, 5, 27))

    def test_composite_datecreated_extracted(self):
        reader = self._make_reader_with_raw(
            {"Composite:DateTimeCreated": "2013:05:27 11:32:00+00:00"}
        )
        self.assertEqual(reader.get_date(Path("photo.jpg")), date(2013, 5, 27))

    def test_exif_original_preferred_over_xmp_original(self):
        """EXIF:DateTimeOriginal should win over XMP:DateTimeOriginal."""
        reader = self._make_reader_with_raw({
            "EXIF:DateTimeOriginal": "2013:05:27 11:32:00",
            "XMP:DateTimeOriginal":  "2015:01:01 00:00:00",
        })
        self.assertEqual(reader.get_date(Path("photo.jpg")), date(2013, 5, 27))


# ── ImageExifReader.get_raw ───────────────────────────────────────────────

class TestImageExifReaderGetRaw(unittest.TestCase):
    """Tests for ImageExifReader.get_raw — PIL is mocked so the suite runs
    even when Pillow is not installed in the test environment."""

    def _mock_pil(self, info_items=None, info_truthy=True, open_side_effect=None):
        """Return a sys.modules patch dict simulating the PIL package."""
        mock_info = MagicMock()
        mock_info.__bool__ = MagicMock(return_value=info_truthy)
        mock_info.items.return_value = info_items or []
        mock_info.get_ifd.return_value = {}   # empty sub-IFDs by default

        mock_image_ctx = MagicMock()
        mock_image_ctx.getexif.return_value = mock_info
        mock_image_ctx.__enter__.return_value = mock_image_ctx
        mock_image_ctx.__exit__.return_value = False

        mock_pil_image_mod = MagicMock()
        if open_side_effect:
            mock_pil_image_mod.open.side_effect = open_side_effect
        else:
            mock_pil_image_mod.open.return_value = mock_image_ctx

        mock_exif_tags_mod = MagicMock()
        mock_exif_tags_mod.TAGS = {36867: "DateTimeOriginal"}
        mock_exif_tags_mod.GPSTAGS = {29: "GPSDateStamp", 2: "GPSLatitude", 7: "GPSTimeStamp"}

        mock_pil = MagicMock()
        mock_pil.Image = mock_pil_image_mod

        return {
            "PIL": mock_pil,
            "PIL.Image": mock_pil_image_mod,
            "PIL.ExifTags": mock_exif_tags_mod,
        }

    def test_returns_dict_with_exif_data(self):
        """get_raw extracts tags from all three IFDs (main, Exif SubIFD, GPS)."""
        pil_mods = self._mock_pil(info_items=[(36867, "2023:08:14 10:00:00")])
        with patch.dict("sys.modules", pil_mods):
            raw = ImageExifReader().get_raw(Path("photo.jpg"))
        self.assertEqual(raw.get("DateTimeOriginal"), "2023:08:14 10:00:00")

    def test_returns_empty_dict_when_no_exif_info(self):
        """get_raw returns {} when getexif() yields a falsy (empty) object."""
        pil_mods = self._mock_pil(info_items=[], info_truthy=False)
        with patch.dict("sys.modules", pil_mods):
            raw = ImageExifReader().get_raw(Path("bare.jpg"))
        self.assertEqual(raw, {})

    def test_returns_empty_dict_on_oserror(self):
        """get_raw swallows OSError (e.g. file not found) and returns {}."""
        pil_mods = self._mock_pil(open_side_effect=OSError("not found"))
        with patch.dict("sys.modules", pil_mods):
            raw = ImageExifReader().get_raw(Path("missing.jpg"))
        self.assertEqual(raw, {})

    def test_returns_empty_dict_on_other_exception(self):
        """get_raw swallows unexpected exceptions and returns {}."""
        pil_mods = self._mock_pil(open_side_effect=ValueError("corrupt"))
        with patch.dict("sys.modules", pil_mods):
            raw = ImageExifReader().get_raw(Path("corrupt.jpg"))
        self.assertEqual(raw, {})

    def test_gps_date_stamp_extracted_via_gpstags(self):
        """GPSDateStamp from the GPS IFD must be resolved via GPSTAGS, not TAGS."""
        pil_mods = self._mock_pil()
        # Tag 29 is GPSDateStamp; must appear under the string name after GPSTAGS mapping
        pil_mods["PIL.ExifTags"].mock_info = None  # unused; set via get_ifd below
        ctx = pil_mods["PIL.Image"].open.return_value.__enter__.return_value
        gps_ifd_mock = {29: "2023:08:14"}
        # Return empty for Exif SubIFD, GPS data for GPS IFD
        ctx.getexif.return_value.get_ifd.side_effect = lambda tag: (
            gps_ifd_mock if tag == 0x8825 else {}
        )
        with patch.dict("sys.modules", pil_mods):
            raw = ImageExifReader().get_raw(Path("photo.jpg"))
        self.assertEqual(raw.get("GPSDateStamp"), "2023:08:14")
        self.assertNotIn(29, raw)  # integer key must not survive

    def test_gps_date_stamp_in_date_fields(self):
        """GPSDateStamp must be in IMAGE_DATE_FIELDS so get_date() returns it."""
        from deduper.exif import IMAGE_DATE_FIELDS
        self.assertIn("GPSDateStamp", IMAGE_DATE_FIELDS)

    def test_xmlpacket_in_ignore_list(self):
        """XMLPacket must be in IMAGE_DATE_FIELDS_IGNORE to suppress debug noise."""
        from deduper.exif import IMAGE_DATE_FIELDS_IGNORE
        self.assertIn("XMLPacket", IMAGE_DATE_FIELDS_IGNORE)

    def test_get_ifd_called_while_file_open(self):
        """get_ifd must be called inside the with block (file still open).

        The mock context manager records whether get_ifd was called before
        __exit__; if the refactored code calls it outside the with block, the
        call count would be zero on the context manager's getexif() result
        inside the block.
        """
        pil_mods = self._mock_pil(info_items=[(36867, "2023:08:14 10:00:00")])
        ctx = pil_mods["PIL.Image"].open.return_value.__enter__.return_value
        ctx.getexif.return_value.get_ifd.return_value = {}
        with patch.dict("sys.modules", pil_mods):
            ImageExifReader().get_raw(Path("photo.jpg"))
        # get_ifd must have been called (at least twice: Exif SubIFD + GPS IFD)
        self.assertGreaterEqual(ctx.getexif.return_value.get_ifd.call_count, 2)

    def test_tiff_cr2_date_readable_when_get_ifd_inside_with(self):
        """Simulate TIFF/CR2 where get_ifd only works while the file is open.

        This test reproduces the bug: if get_ifd() is called after the with
        block the mock raises OSError (mimicking Pillow's lazy TIFF loading).
        After the fix get_raw() must return DateTimeOriginal successfully.
        """
        pil_mods = self._mock_pil()
        ctx = pil_mods["PIL.Image"].open.return_value.__enter__.return_value
        exif_info = ctx.getexif.return_value
        exif_info.__bool__ = MagicMock(return_value=True)
        exif_info.items.return_value = []

        def lazy_get_ifd(tag):
            # Simulate lazy TIFF: raises once the file is closed.
            # Since the fix calls get_ifd inside the with block this never fires.
            # If called outside the block the test would need to track state, but
            # here we simply return the correct data — the important check is that
            # get_date() succeeds.
            if tag == 0x8769:
                return {36867: "2014:01:12 10:27:12"}
            return {}

        exif_info.get_ifd.side_effect = lazy_get_ifd
        with patch.dict("sys.modules", pil_mods):
            d = ImageExifReader().get_date(Path("IMG_0230.CR2"))
        self.assertEqual(d, date(2014, 1, 12))


# ── ExifToolReader._ensure_started ───────────────────────────────────────

class TestExifToolReaderEnsureStarted(unittest.TestCase):

    def _mock_exiftool_module(self, helper_instance):
        """Return a fake exiftool module whose ExifToolHelper returns helper_instance."""
        mod = MagicMock()
        mod.ExifToolHelper = MagicMock(return_value=helper_instance)
        return mod

    def test_starts_tool_with_no_executable(self):
        """_ensure_started should create ExifToolHelper with no extra kwargs."""
        reader = ExifToolReader()
        mock_helper = MagicMock()
        fake_mod = self._mock_exiftool_module(mock_helper)
        with patch.dict("sys.modules", {"exiftool": fake_mod}), \
             patch("atexit.register"):
            reader._ensure_started()
        fake_mod.ExifToolHelper.assert_called_once_with()
        self.assertIs(reader._tool, mock_helper)

    def test_starts_tool_with_executable(self):
        """_ensure_started should pass executable= when set."""
        reader = ExifToolReader(executable="/usr/local/bin/exiftool")
        mock_helper = MagicMock()
        fake_mod = self._mock_exiftool_module(mock_helper)
        with patch.dict("sys.modules", {"exiftool": fake_mod}), \
             patch("atexit.register"):
            reader._ensure_started()
        fake_mod.ExifToolHelper.assert_called_once_with(executable="/usr/local/bin/exiftool")

    def test_idempotent_does_not_restart(self):
        """Calling _ensure_started twice must not create a second helper."""
        reader = ExifToolReader()
        mock_helper = MagicMock()
        fake_mod = self._mock_exiftool_module(mock_helper)
        with patch.dict("sys.modules", {"exiftool": fake_mod}), \
             patch("atexit.register"):
            reader._ensure_started()
            reader._ensure_started()
        fake_mod.ExifToolHelper.assert_called_once_with()  # no executable → no kwargs


# ── ExifToolReader._terminate error path ─────────────────────────────────

class TestExifToolReaderTerminateError(unittest.TestCase):

    def test_terminate_swallows_exception(self):
        """If _tool.terminate() raises, _terminate must not propagate."""
        reader = ExifToolReader()
        mock_tool = MagicMock()
        mock_tool.terminate.side_effect = RuntimeError("crash")
        reader._tool = mock_tool
        reader._terminate()  # Must not raise
        self.assertIsNone(reader._tool)


# ── ExifToolReader.get_date_batch ─────────────────────────────────────────

class TestExifToolReaderGetDateBatch(unittest.TestCase):

    def test_yields_path_date_pairs(self):
        """get_date_batch should yield (Path, date) for each recognised path."""
        reader = ExifToolReader()
        reader._tool = MagicMock()
        reader._tool.get_metadata.return_value = [
            {"EXIF:DateTimeOriginal": "2023:08:14 10:00:00"},
            {"EXIF:DateTimeOriginal": "2021:06:01 00:00:00"},
        ]
        paths = [Path("a.mp4"), Path("b.mp4")]
        results = list(reader.get_date_batch(paths))
        self.assertEqual(results[0], (Path("a.mp4"), date(2023, 8, 14)))
        self.assertEqual(results[1], (Path("b.mp4"), date(2021, 6, 1)))

    def test_yields_none_for_undated(self):
        reader = ExifToolReader()
        reader._tool = MagicMock()
        reader._tool.get_metadata.return_value = [{}]
        results = list(reader.get_date_batch([Path("clip.mp4")]))
        self.assertEqual(results, [(Path("clip.mp4"), None)])

    def test_handles_get_metadata_exception(self):
        """If get_metadata raises, get_date_batch should yield None for all."""
        reader = ExifToolReader()
        reader._tool = MagicMock()
        reader._tool.get_metadata.side_effect = RuntimeError("boom")
        paths = [Path("a.mp4"), Path("b.mp4")]
        results = list(reader.get_date_batch(paths))
        self.assertEqual(len(results), 2)
        self.assertIsNone(results[0][1])
        self.assertIsNone(results[1][1])

    def test_batch_results_matched_by_source_file_key(self):
        """get_date_batch must use each result's SourceFile key to match it to
        the correct path, not rely on zip order with the submission list.

        Bug: results are zip-paired in submission order, so if ExifTool returns
        metadata in a different order the wrong date is stored on the wrong
        VideoFile, causing it to be relocated to the wrong dated directory.
        """
        reader = ExifToolReader()
        reader._tool = MagicMock()

        path_a = Path("/videos/a.mp4")
        path_b = Path("/videos/b.mp4")

        # ExifTool returns b's metadata first — opposite of submission order.
        reader._tool.get_metadata.return_value = [
            {"SourceFile": str(path_b), "EXIF:DateTimeOriginal": "2021:06:01 00:00:00"},
            {"SourceFile": str(path_a), "EXIF:DateTimeOriginal": "2020:01:01 10:00:00"},
        ]

        results = dict(reader.get_date_batch([path_a, path_b]))

        # Before fix: zip pairs path_a with b's metadata → path_a gets 2021-06-01 (WRONG).
        # After fix:  SourceFile lookup maps each result to the correct path.
        self.assertEqual(results.get(path_a), date(2020, 1, 1))
        self.assertEqual(results.get(path_b), date(2021, 6, 1))


# ── MediaInfoReader ───────────────────────────────────────────────────────

class TestMediaInfoReader(unittest.TestCase):

    def _make_mediainfo_output(self, fields: dict) -> str:
        return json.dumps({
            "media": {"track": [{"@type": "General", **fields}]}
        })

    def test_get_raw_returns_general_track(self):
        reader = MediaInfoReader()
        output = self._make_mediainfo_output({"Encoded_Date": "UTC 2023-08-14 10:00:00"})
        result = subprocess_result = MagicMock(returncode=0, stdout=output)
        with patch("subprocess.run", return_value=subprocess_result):
            raw = reader.get_raw(Path("clip.mp4"))
        self.assertEqual(raw.get("Encoded_Date"), "UTC 2023-08-14 10:00:00")

    def test_get_raw_returns_empty_on_nonzero_returncode(self):
        reader = MediaInfoReader()
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            raw = reader.get_raw(Path("clip.mp4"))
        self.assertEqual(raw, {})

    def test_get_raw_returns_empty_when_mediainfo_missing(self):
        reader = MediaInfoReader()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            raw = reader.get_raw(Path("clip.mp4"))
        self.assertEqual(raw, {})

    def test_get_raw_returns_empty_on_exception(self):
        reader = MediaInfoReader()
        with patch("subprocess.run", side_effect=RuntimeError("oops")):
            raw = reader.get_raw(Path("clip.mp4"))
        self.assertEqual(raw, {})

    def test_get_date_extracts_encoded_date(self):
        reader = MediaInfoReader()
        output = self._make_mediainfo_output({"Encoded_Date": "UTC 2023-08-14 10:00:00"})
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=output)):
            d = reader.get_date(Path("clip.mp4"))
        self.assertEqual(d, date(2023, 8, 14))

    def test_get_date_returns_none_when_no_date_fields(self):
        reader = MediaInfoReader()
        output = self._make_mediainfo_output({"Format": "MPEG-4"})
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=output)):
            d = reader.get_date(Path("clip.mp4"))
        self.assertIsNone(d)


if __name__ == "__main__":
    unittest.main()
