"""
dates.py — Date parsing and formatting utilities.

All functions are pure (no global state) and are independently testable.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

# Matches yyyy[sep]mm[sep]dd anywhere in a string.
# Uses negative lookbehind (no preceding digit) and negative lookahead (no
# following digit) rather than \b because \b does not trigger between a word
# character like '_' and a digit, causing '_2021-12-25' to miss.
# The outer groups capture prefix and suffix so callers can reconstruct paths.
_DATE_RE = re.compile(
    r"(.*?)(?<!\d)(?P<yyyy>\d{4})\D?(?P<mm>\d{2})\D?(?P<dd>\d{2})(?!\d)(.*)",
    re.DOTALL,
)


def parse_date(s: str) -> Optional[date]:
    """Return the first plausible ``date`` found in *s*, or ``None``.

    Accepts separators ``-``, ``.``, ``/``, or none between year/month/day.
    Rejects structurally matching but calendar-invalid strings (e.g. month 13).

    >>> parse_date("holiday/2023/08/14/IMG_1234.jpg")
    datetime.date(2023, 8, 14)
    >>> parse_date("no date here") is None
    True
    >>> parse_date("2023-13-01") is None   # month 13 is invalid
    True
    """
    if not isinstance(s, str):
        return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group("yyyy")), int(m.group("mm")), int(m.group("dd")))
    except ValueError:
        return None  # Calendar-invalid date (e.g. 0000-00-00, month 13)


def split_path_on_date(s: str):
    """Split *s* around its embedded date, returning ``(prefix, dt, suffix)``.

    Returns ``(s, None, '')`` when no date is found.

    >>> split_path_on_date("/photos/2023-08-14/IMG.jpg")
    ('/photos/', datetime.date(2023, 8, 14), '/IMG.jpg')
    >>> split_path_on_date("/no/date/here")
    ('/no/date/here', None, '')
    """
    m = _DATE_RE.search(s)
    if not m:
        return s, None, ""
    try:
        dt = date(int(m.group("yyyy")), int(m.group("mm")), int(m.group("dd")))
    except ValueError:
        return s, None, ""
    prefix = m.group(1)
    suffix = m.group(5)
    return prefix, dt, suffix


def date_to_str(dt: Optional[date], sep: str = "/") -> str:
    """Format *dt* as ``yyyy<sep>mm<sep>dd``.

    >>> date_to_str(date(2023, 8, 4), "/")
    '2023/08/04'
    >>> date_to_str(date(2023, 8, 4), "-")
    '2023-08-04'
    >>> date_to_str(None, "/")
    '0000/00/00'
    """
    if dt is None:
        return f"0000{sep}00{sep}00"
    return f"{dt.year:04d}{sep}{dt.month:02d}{sep}{dt.day:02d}"
