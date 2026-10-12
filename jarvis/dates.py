"""
Day-level dates for site records.

The knowledge fabric reasons in years and quarters; a schedule reasons in days.
This module reads the dates that appear in site diaries, DPRs, letters and
questions: ISO dates, dd/mm/yyyy (the Indian order - never mm/dd), "5 Sept",
"Sept 5th", "05-09-26", Hindi and transliterated month names ("5 सितंबर",
"sitambar"), and the relative words a foreman writes ("today", "yesterday",
"aaj", "kal se"). A date without a year takes the reference date's year.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import List, Optional, Tuple

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
    # Hindi (Devanagari) and common transliterations
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6, "जुलाई": 7,
    "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10, "नवंबर": 11, "नवम्बर": 11,
    "दिसंबर": 12, "दिसम्बर": 12,
    "janvari": 1, "farvari": 2, "aprail": 4, "julai": 7, "agast": 8, "sitambar": 9, "sitmbar": 9,
    "aktubar": 10, "navambar": 11, "disambar": 12,
}
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_DMY = re.compile(r"(?<![\d/.-])(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})(?![\d/.-])")
_D_MON = re.compile(rf"(?<![\w])(\d{{1,2}})(?:st|nd|rd|th)?[\s\-.]*(?:of\s+)?({_MONTH_ALT})(?![\w])\.?"
                    rf"(?:[\s,\-]+(\d{{4}})(?!\d)|\s*'(\d{{2}})(?!\d))?", re.IGNORECASE)
_MON_D = re.compile(rf"(?<![\w])({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?![\d:])(?:,?\s+(\d{{4}})(?!\d))?",
                    re.IGNORECASE)
_RELATIVE = [
    (re.compile(r"\b(today|aaj)\b|आज", re.IGNORECASE), 0),
    (re.compile(r"\b(yesterday)\b", re.IGNORECASE), -1),
    (re.compile(r"\b(tomorrow)\b|\bkal se\b|कल से", re.IGNORECASE), 1),
    (re.compile(r"\b(day after tomorrow|parso|परसों)\b", re.IGNORECASE), 2),
]


def today() -> _dt.date:
    return _dt.date.today()


def _year(token: Optional[str], reference: _dt.date) -> int:
    if not token:
        return reference.year
    token = token.strip("'")
    value = int(token)
    value = value + 2000 if value < 100 else value
    return value if 1990 <= value <= 2100 else reference.year


def _make(year: int, month: int, day: int) -> Optional[_dt.date]:
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str, reference: Optional[_dt.date] = None,
               relative: bool = True) -> List[Tuple[_dt.date, Tuple[int, int], str]]:
    """Every date in `text` as (date, span, surface), in order of appearance."""
    reference = reference or today()
    text = text or ""
    found: List[Tuple[_dt.date, Tuple[int, int], str]] = []
    taken: List[Tuple[int, int]] = []

    def free(span):
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    def add(value, match):
        if value is not None and free(match.span()):
            found.append((value, match.span(), match.group(0).strip()))
            taken.append(match.span())

    for m in _ISO.finditer(text):
        add(_make(int(m.group(1)), int(m.group(2)), int(m.group(3))), m)
    for m in _DMY.finditer(text):
        day, month = int(m.group(1)), int(m.group(2))
        add(_make(_year(m.group(3), reference), month, day), m)
    for m in _D_MON.finditer(text):
        month = MONTHS.get(m.group(2).lower().rstrip("."))
        if month:
            add(_make(_year(m.group(3) or m.group(4), reference), month, int(m.group(1))), m)
    for m in _MON_D.finditer(text):
        month = MONTHS.get(m.group(1).lower().rstrip("."))
        if month:
            add(_make(_year(m.group(3), reference), month, int(m.group(2))), m)
    if relative:
        for pattern, offset in _RELATIVE:
            for m in pattern.finditer(text):
                add(reference + _dt.timedelta(days=offset), m)
    found.sort(key=lambda item: item[1][0])
    return found


def parse_date(text: str, reference: Optional[_dt.date] = None) -> Optional[_dt.date]:
    """The first date in `text`, or None."""
    if isinstance(text, _dt.datetime):
        return text.date()
    if isinstance(text, _dt.date):
        return text
    hits = find_dates(str(text or ""), reference)
    return hits[0][0] if hits else None


def parse_range(text: str, reference: Optional[_dt.date] = None) -> Tuple[Optional[_dt.date], Optional[_dt.date]]:
    """'between 10 September and 17 September' / 'from 5 Sep to 9 Sep' / 'on 12 Sept'."""
    hits = find_dates(text, reference, relative=False)
    if not hits:
        return None, None
    if len(hits) == 1:
        return hits[0][0], hits[0][0]
    first, second = hits[0][0], hits[1][0]
    return (first, second) if first <= second else (second, first)


def from_iso(value) -> Optional[_dt.date]:
    """'2026-09-05' / '2026-09-05T10:31:00' / a date -> date (None if unreadable)."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    m = _ISO.search(str(value))
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return parse_date(str(value))


def parse_datetime(value) -> Optional[_dt.datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    text = str(value).strip()
    m = _ISO.search(text)
    if m:
        try:
            return _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
        except ValueError:
            return None
    day = parse_date(text)
    return _dt.datetime(day.year, day.month, day.day) if day else None


def iso(value) -> Optional[str]:
    day = from_iso(value)
    return day.isoformat() if day else None


def iso_dt(value) -> Optional[str]:
    moment = parse_datetime(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") if moment else None


def end_of_day(value) -> Optional[str]:
    """A knowledge cut-off: everything captured up to the end of that day."""
    day = from_iso(value)
    return f"{day.isoformat()}T23:59:59" if day else None


def fmt(value) -> str:
    day = from_iso(value)
    return day.strftime("%d %b %Y") if day else "—"


def days_between(a, b) -> Optional[int]:
    first, second = from_iso(a), from_iso(b)
    if first is None or second is None:
        return None
    return (second - first).days
