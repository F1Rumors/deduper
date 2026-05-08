"""Tests for deduper.dates — date parsing and formatting utilities."""

import unittest
from datetime import date

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from deduper.dates import parse_date, split_path_on_date, date_to_str


class TestParseDate(unittest.TestCase):

    # ── Happy paths ────────────────────────────────────────────────────────

    def test_slash_separated_path(self):
        self.assertEqual(parse_date("/photos/2023/08/14/IMG.jpg"), date(2023, 8, 14))

    def test_hyphen_separated_filename(self):
        self.assertEqual(parse_date("IMG_2021-12-25.jpg"), date(2021, 12, 25))

    def test_dot_separated(self):
        self.assertEqual(parse_date("2019.03.07-holiday"), date(2019, 3, 7))

    def test_no_separator(self):
        self.assertEqual(parse_date("20200101_party.jpg"), date(2020, 1, 1))

    def test_date_embedded_in_longer_string(self):
        self.assertEqual(parse_date("backup_2022_06_15_final.zip"), date(2022, 6, 15))

    def test_date_at_start(self):
        self.assertEqual(parse_date("2000/01/31"), date(2000, 1, 31))

    def test_date_at_end(self):
        self.assertEqual(parse_date("archive-2015-11-30"), date(2015, 11, 30))

    # ── Invalid / edge cases ───────────────────────────────────────────────

    def test_no_date_returns_none(self):
        self.assertIsNone(parse_date("no date here"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_date(""))

    def test_non_string_returns_none(self):
        self.assertIsNone(parse_date(None))   # type: ignore
        self.assertIsNone(parse_date(12345))  # type: ignore

    def test_invalid_month_returns_none(self):
        self.assertIsNone(parse_date("2023-13-01"))

    def test_invalid_day_returns_none(self):
        self.assertIsNone(parse_date("2023-01-32"))

    def test_all_zeros_returns_none(self):
        self.assertIsNone(parse_date("0000-00-00"))

    def test_date_in_directory_component(self):
        self.assertEqual(parse_date("/home/user/2017/07/04/fireworks.jpg"), date(2017, 7, 4))

    def test_four_digit_year_required(self):
        # Three-digit year should NOT match
        self.assertIsNone(parse_date("123-01-01"))

    def test_returns_first_date_found(self):
        # Two candidate dates — should return the first one
        result = parse_date("2020-01-01 and 2021-02-02")
        self.assertEqual(result, date(2020, 1, 1))


class TestSplitPathOnDate(unittest.TestCase):

    def test_typical_path(self):
        prefix, dt, suffix = split_path_on_date("/photos/2023-08-14/IMG.jpg")
        self.assertEqual(dt, date(2023, 8, 14))
        self.assertIn("photos", prefix)
        self.assertIn("IMG.jpg", suffix)

    def test_no_date(self):
        s = "/no/date/here"
        prefix, dt, suffix = split_path_on_date(s)
        self.assertEqual(prefix, s)
        self.assertIsNone(dt)
        self.assertEqual(suffix, "")

    def test_invalid_calendar_date(self):
        s = "/2023-13-01/file"
        prefix, dt, suffix = split_path_on_date(s)
        self.assertIsNone(dt)


class TestDateToStr(unittest.TestCase):

    def test_slash_separator(self):
        self.assertEqual(date_to_str(date(2023, 8, 4), "/"), "2023/08/04")

    def test_hyphen_separator(self):
        self.assertEqual(date_to_str(date(2023, 8, 4), "-"), "2023-08-04")

    def test_no_separator(self):
        self.assertEqual(date_to_str(date(2023, 8, 4), ""), "20230804")

    def test_none_gives_zeros(self):
        self.assertEqual(date_to_str(None, "/"), "0000/00/00")
        self.assertEqual(date_to_str(None, "-"), "0000-00-00")

    def test_single_digit_month_and_day_padded(self):
        self.assertEqual(date_to_str(date(2000, 1, 1), "-"), "2000-01-01")

    def test_leap_day(self):
        self.assertEqual(date_to_str(date(2000, 2, 29), "/"), "2000/02/29")


if __name__ == "__main__":
    unittest.main()
