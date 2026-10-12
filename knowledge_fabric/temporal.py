"""
Time in questions and documents.

    parse("Why did profit fall after 2017?", latest_year=2024)
      -> TimeScope(start=2018, end=2024, relation="after",
                   context_start=2015, context_end=2024, ...)

Understands BEFORE, AFTER, DURING, BETWEEN, SINCE, UNTIL, a YEAR, a QUARTER, a
MONTH, a FISCAL YEAR, decades, relative periods ("the last five years") and the
measures YOY, CAGR, TREND and CHANGE.

Two rules:
  * a relative period is resolved against the latest year the CORPUS covers
    (not today's date - "the last five years of records" means the records),
    and the resolution is written into `assumptions`;
  * a question about change or cause also gets a CONTEXT window - the years
    around the one asked about - so the analysis can see what came before.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

YEAR = r"(19[5-9]\d|20[0-4]\d)"
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
                 "twenty": 20, "thirty": 30, "a couple of": 2, "a few": 3, "several": 3}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                        "sep", "oct", "nov", "dec"], start=1)}
_ORDINAL_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3,
              "4th": 4, "last": 4}
_DECADE_WORDS = {"eighties": 1980, "nineties": 1990, "noughties": 2000, "twenties": 2020,
                 "twenty-tens": 2010, "twenty tens": 2010}


@dataclass
class TimeScope:
    start: Optional[int] = None
    end: Optional[int] = None
    points: List[int] = field(default_factory=list)       # explicit years being compared
    periods: List[List[int]] = field(default_factory=list)  # ranges being compared: [[2010, 2015], [2020, 2025]]
    quarter: Optional[int] = None
    month: Optional[int] = None
    fiscal: Optional[str] = None                          # the label as written, e.g. FY2019-20
    relation: str = "none"   # none|in|between|since|after|before|until|during|relative|decade
    context_start: Optional[int] = None
    context_end: Optional[int] = None
    measures: List[str] = field(default_factory=list)     # yoy|cagr|trend|change
    relative_years: Optional[int] = None
    assumptions: List[str] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.start is None and self.end is None and not self.points

    def years(self) -> List[int]:
        if self.points and self.start is None:
            return sorted(set(self.points))
        if self.start is None and self.end is None:
            return []
        lo = self.start if self.start is not None else self.end
        hi = self.end if self.end is not None else self.start
        return list(range(min(lo, hi), max(lo, hi) + 1))

    def window(self) -> Optional[tuple]:
        """The years the ANALYSIS should read: the scope widened by its context."""
        years = self.years()
        if not years:
            return None
        lo = min(years + ([self.context_start] if self.context_start else []))
        hi = max(years + ([self.context_end] if self.context_end else []))
        return lo, hi

    def contains(self, year: Optional[int]) -> bool:
        if year is None:
            return False
        if self.points and self.start is None:
            return year in self.points
        lo, hi = (self.start or -10**6), (self.end or 10**6)
        return lo <= year <= hi

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["years"] = self.years()
        data["window"] = self.window()
        return data


def _num(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def _full_year(token: str, reference: Optional[int] = None) -> Optional[int]:
    token = token.strip()
    if len(token) == 4 and token.isdigit():
        return int(token)
    if len(token) == 2 and token.isdigit():
        value = int(token)
        century = (reference // 100 * 100) if reference else 2000
        year = century + value
        if reference and year < reference - 50:
            year += 100
        return year if year < 2100 else year - 100
    return None


def _fiscal_range(first: str, second: Optional[str], convention: str) -> tuple:
    """FY2019-20 -> (2019, 2020). A bare FY2019 follows `convention`:
    'end' (the Indian and most corporate usage: FY2019 ends in 2019, so it
    spans 2018-2019) or 'start'."""
    a = _full_year(first)
    if second:
        b = _full_year(second, reference=a)
        return a, b
    if convention == "start":
        return a, a + 1
    return a - 1, a


def fiscal_convention() -> str:
    try:
        import configparser
        from core.paths import config_path
        parser = configparser.ConfigParser()
        parser.read(config_path(), encoding="utf-8")
        value = parser.get("knowledge", "fiscal_year_label", fallback="end").strip().lower()
        return value if value in ("start", "end") else "end"
    except Exception:
        return "end"


def parse(text: str, latest_year: Optional[int] = None, earliest_year: Optional[int] = None,
          today: Optional[_dt.date] = None) -> TimeScope:
    scope = TimeScope()
    if not text:
        return scope
    low = " " + text.lower() + " "
    today = today or _dt.date.today()
    latest = latest_year or today.year
    earliest = earliest_year

    # ---- measures (do not affect the range) ----------------------------------
    for pattern, measure in ((r"\byo[y]\b|year[- ]on[- ]year|year[- ]over[- ]year", "yoy"),
                             (r"\bcagr\b|compound(ed)? annual", "cagr"),
                             (r"\btrend|over time|trajector|over the (years|period)", "trend"),
                             (r"\bchang(e|ed|es)\b|\bdifference\b|\bdelta\b|\bshift", "change"),
                             (r"\b(grow|grew|growth|increas|declin|decreas|fell|fall|drop|rose|rise)", "trend")):
        if re.search(pattern, low) and measure not in scope.measures:
            scope.measures.append(measure)

    # ---- fiscal years ----------------------------------------------------------
    fiscal = re.search(r"\bfy\s*'?(\d{2,4})(?:\s*[-/]\s*'?(\d{2,4}))?\b", low)
    if fiscal:
        a, b = _fiscal_range(fiscal.group(1), fiscal.group(2), fiscal_convention())
        if a and b:
            scope.start, scope.end, scope.relation = a, b, "in"
            scope.fiscal = fiscal.group(0).strip().upper().replace(" ", "")
            if not fiscal.group(2):
                scope.assumptions.append(
                    f"{scope.fiscal} read as the fiscal year ending in {b if fiscal_convention() == 'end' else a + 1} "
                    f"(config.ini [knowledge] fiscal_year_label)")
            scope.mentions.append(fiscal.group(0).strip())

    # ---- quarters --------------------------------------------------------------
    quarter = (re.search(rf"\bq([1-4])\s*(?:of\s*)?(?:fy\s*)?'?(\d{{4}}|\d{{2}})\b", low)
               or re.search(rf"\b(\d{{4}})\s*q([1-4])\b", low)
               or re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th|last)\s+quarter\s+(?:of\s+)?(?:fy\s*)?(\d{4})", low))
    if quarter:
        g1, g2 = quarter.group(1), quarter.group(2)
        if g1.isdigit() and len(g1) == 4:
            year, q = int(g1), int(g2)
        elif g1.isdigit():
            year, q = _full_year(g2), int(g1)
        else:
            year, q = int(g2), _ORDINAL_Q[g1]
        if year:
            scope.start = scope.end = year
            scope.quarter, scope.relation = q, "in"
            scope.mentions.append(quarter.group(0).strip())

    # ---- months ----------------------------------------------------------------
    month = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?[\s\-,/]+'?(\d{4})\b", low)
    if month and not quarter:
        scope.start = scope.end = int(month.group(2))
        scope.month = _MONTHS[month.group(1)[:3]]
        scope.relation = "in"
        scope.mentions.append(month.group(0).strip())

    # ---- explicit ranges ---------------------------------------------------------
    range_match = (re.search(rf"\bbetween\s+{YEAR}\s+and\s+{YEAR}\b", low)
                   or re.search(rf"\bfrom\s+{YEAR}\s+(?:to|till|until|through|thru|-|–)\s+{YEAR}\b", low)
                   or re.search(rf"\b{YEAR}\s*(?:-|–|—|to|till|until|through)\s*{YEAR}\b", low))
    spans = list(dict.fromkeys(
        (min(int(m.group(1)), int(m.group(2))), max(int(m.group(1)), int(m.group(2))))
        for m in re.finditer(rf"\b{YEAR}\s*(?:-|–|—|to|till|until|through)\s*{YEAR}\b", low)))
    if len(spans) >= 2:
        # "compare 2010-2015 with 2020-2025": two periods, each read whole - the scope
        # spans both so every year of each is retrieved.
        scope.periods = [list(s) for s in spans[:4]]
        scope.start, scope.end = min(s[0] for s in spans), max(s[1] for s in spans)
        scope.relation = "between"
        scope.mentions += [f"{a}-{b}" for a, b in spans[:4]]
    elif range_match:
        a, b = int(range_match.group(1)), int(range_match.group(2))
        scope.start, scope.end = min(a, b), max(a, b)
        scope.relation = "between"
        scope.mentions.append(range_match.group(0).strip())
    else:
        short = re.search(r"\b(19[5-9]\d|20[0-4]\d)\s*[-–/]\s*(\d{2})\b", low)
        if short and not fiscal:
            a = int(short.group(1))
            b = _full_year(short.group(2), reference=a)
            if b and b > a:
                scope.start, scope.end, scope.relation = a, b, "between"
                scope.mentions.append(short.group(0).strip())

    # ---- relations with one year ---------------------------------------------------
    # "revenue in 2016 and 2023" names two points, not the year 2016.
    listed = re.search(rf"{YEAR}\s*(?:,|and|&|or|vs\.?|versus)\s*(?:in\s+|with\s+)?{YEAR}", low)
    if scope.relation in ("none",) and not listed:
        for words, relation in ((r"since|from", "since"), (r"after|post|following|subsequent to", "after"),
                                (r"before|prior to|pre|earlier than|up to", "before"),
                                (r"until|till|through|to|upto", "until"),
                                (r"during|throughout", "during"), (r"in|for|of", "in")):
            match = re.search(rf"\b(?:{words})[\s-]+(?:the\s+)?(?:year\s+)?{YEAR}\b", low)
            if match:
                year = int(match.group(1))
                if relation == "since":
                    scope.start, scope.end = year, latest
                elif relation == "after":
                    scope.start, scope.end = year + 1, latest
                elif relation == "before":
                    scope.start, scope.end = earliest, year - 1
                elif relation == "until":
                    scope.start, scope.end = earliest, year
                else:
                    scope.start = scope.end = year
                scope.relation = relation
                scope.mentions.append(match.group(0).strip())
                break

    # ---- decades ---------------------------------------------------------------------
    decade = re.search(r"\b(?:the\s+)?(19[5-9]0|20[0-4]0)'?s\b", low)
    if decade and scope.relation == "none":
        base = int(decade.group(1))
        scope.start, scope.end, scope.relation = base, base + 9, "decade"
        scope.mentions.append(decade.group(0).strip())
    else:
        for word, base in _DECADE_WORDS.items():
            if re.search(rf"\b{word}\b", low) and scope.relation == "none":
                scope.start, scope.end, scope.relation = base, base + 9, "decade"
                scope.mentions.append(word)

    # ---- relative periods --------------------------------------------------------------
    rel = (re.search(r"\b(?:last|past|previous|preceding|recent|over the last|over the past)\s+(\d+|[a-z]+(?: of)?)\s+(?:fiscal\s+)?years?\b", low)
           or re.search(r"\b(\d+|[a-z]+)\s+(?:consecutive\s+|straight\s+)?years?\s+(?:of|in a row|running)\b", low))
    if rel and scope.relation == "none":
        n = _num(rel.group(1).replace(" of", ""))
        if n:
            scope.relative_years = n
            scope.start, scope.end, scope.relation = latest - n + 1, latest, "relative"
            scope.assumptions.append(
                f"'{rel.group(0).strip()}' resolved against the latest year in the records "
                f"({latest}): {latest - n + 1}-{latest}")
            scope.mentions.append(rel.group(0).strip())
    elif re.search(r"\blast (fiscal )?year\b|\bprevious year\b", low) and scope.relation == "none":
        scope.start = scope.end = latest - 1
        scope.relation = "relative"
        scope.assumptions.append(f"'last year' resolved against the latest year in the records ({latest}): {latest - 1}")
    elif re.search(r"\b(this|current) year\b", low) and scope.relation == "none":
        scope.start = scope.end = latest
        scope.relation = "relative"

    # ---- every other explicit year (comparisons: "2010 vs 2020") ------------------------
    years = [int(y) for y in re.findall(YEAR, low)]
    if years:
        if scope.relation == "none":
            if len(set(years)) == 1:
                scope.start = scope.end = years[0]
                scope.relation = "in"
            else:
                scope.points = sorted(set(years))
                if re.search(r"\bcompar|\bvs\.?\b|\bversus\b|\bagainst\b", low) or listed:
                    scope.relation = "points"
                else:
                    scope.start, scope.end = min(years), max(years)
                    scope.relation = "between"
        else:
            extra = sorted(set(years) - set(scope.years()))
            if extra and re.search(r"\bcompar|\bvs\.?\b|\bversus\b", low):
                scope.points = sorted(set(years))

    # ---- open-ended bounds need the corpus -----------------------------------------------
    if scope.start is None and scope.end is not None and scope.relation in ("before", "until"):
        scope.assumptions.append("no lower bound: from the earliest record available")
    if scope.end is not None and scope.relation in ("since", "after") and latest_year is None:
        scope.assumptions.append(f"open-ended period closed at {latest} (no corpus year known)")

    # ---- context window for change / cause questions -------------------------------------
    asks_cause = bool(re.search(r"\bwhy\b|\bcause|\breason|\bdriver|\bexplain|what happened|root cause", low))
    if (asks_cause or "change" in scope.measures or "trend" in scope.measures) and scope.years():
        years_in = scope.years()
        if scope.relation == "after":
            scope.context_start = years_in[0] - 3
        elif scope.relation in ("in", "during"):
            scope.context_start, scope.context_end = years_in[0] - 2, years_in[-1] + 1
        elif scope.relation in ("before", "until"):
            scope.context_end = years_in[-1] + 2
        elif scope.relation in ("between", "relative", "since", "decade"):
            scope.context_start = years_in[0] - 1
        if scope.context_end is not None and latest_year is not None:
            scope.context_end = min(scope.context_end, latest)
    return scope


def years_in_text(text: str) -> List[int]:
    """Every plausible year mentioned in a passage, in order of appearance."""
    return [int(y) for y in re.findall(rf"\b{YEAR}\b", text or "")]


def dominant_year(text: str) -> Optional[int]:
    """The year a passage is mainly about: the most frequent, earliest on ties."""
    years = years_in_text(text)
    if not years:
        return None
    counts: Dict[int, int] = {}
    for y in years:
        counts[y] = counts.get(y, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], years.index(kv[0])))[0][0]


def period_of(label: str) -> Dict[str, Optional[int]]:
    """A table header or cell as a period: '2018', 'FY2019-20', 'Q3 2018',
    'Mar-2019', '2018-19'. Returns year / quarter / month / label (None if not a period)."""
    text = (label or "").strip()
    low = text.lower()
    if not text or len(text) > 24:
        return {"year": None, "quarter": None, "month": None, "label": None}
    m = re.fullmatch(r"(?:fy\s*)?'?(\d{2,4})\s*[-/]\s*'?(\d{2,4})", low)
    if m and (low.startswith("fy") or len(m.group(1)) == 4):
        a = _full_year(m.group(1))
        b = _full_year(m.group(2), reference=a)
        if a and b and b == a + 1:
            return {"year": b if fiscal_convention() == "end" else a, "quarter": None, "month": None,
                    "label": text}
    m = re.fullmatch(r"fy\s*'?(\d{2,4})", low)
    if m:
        y = _full_year(m.group(1))
        return {"year": y, "quarter": None, "month": None, "label": text}
    m = re.fullmatch(r"q([1-4])\s*[-/ ]?\s*(?:fy\s*)?'?(\d{2,4})", low) or re.fullmatch(r"(\d{4})\s*[-/ ]?\s*q([1-4])", low)
    if m:
        a, b = m.group(1), m.group(2)
        if len(a) == 4:
            return {"year": int(a), "quarter": int(b), "month": None, "label": text}
        return {"year": _full_year(b), "quarter": int(a), "month": None, "label": text}
    m = re.fullmatch(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[\s\-/]*'?(\d{2,4})", low)
    if m:
        return {"year": _full_year(m.group(2)), "quarter": None, "month": _MONTHS[m.group(1)[:3]], "label": text}
    m = re.fullmatch(r"(19[5-9]\d|20[0-4]\d)(?:\.0)?", low)
    if m:
        return {"year": int(m.group(1)), "quarter": None, "month": None, "label": text}
    return {"year": None, "quarter": None, "month": None, "label": None}
