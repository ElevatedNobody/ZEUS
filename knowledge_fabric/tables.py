"""
Tables stay tables.

A table that reaches the knowledge fabric is stored three ways:
  * the table itself (title, headers, units, orientation, page, document),
  * every cell (row, column, header, row label, text, number, unit),
  * NUMERIC FACTS - (metric, entity, period, value, unit) - each pointing back to
    the exact cell it came from.

The facts are what make "What was production in Q3 2018?" or "Which unit had the
highest maintenance cost?" answerable by a query instead of by a model reading
flattened text. Nothing is inferred silently: a fact whose period comes from the
document date rather than the table is stored with lower confidence and says so.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from knowledge_fabric.temporal import period_of

_NUMBER = re.compile(r"^[\(\-−+]?\s*(?:₹|rs\.?|inr|usd|\$|eur|€|£)?\s*"
                     r"(\d{1,3}(?:,\d{2,3})+|\d+)(\.\d+)?\s*\)?\s*(%|cr|crore|crores|lakh|lakhs|mn|m|bn|b|k)?$",
                     re.IGNORECASE)
_UNIT_IN_HEADER = re.compile(r"[\(\[]\s*([^\)\]]{1,24})\s*[\)\]]\s*$")
_EMPTY = {"", "-", "–", "—", "n/a", "na", "nil", "none", "null", "..", "…", "nan"}

ENTITY_HEADERS = ("unit", "plant", "equipment", "asset", "department", "dept", "project",
                  "site", "location", "refinery", "area", "product", "vendor", "supplier",
                  "customer", "business unit", "division", "tag", "facility", "segment")
PERIOD_HEADERS = ("year", "fy", "fiscal year", "financial year", "period", "quarter",
                  "month", "date", "qtr")

# Metric names that mean the same thing - only where the equivalence is standard.
METRIC_SYNONYMS = {
    "pat": "net profit", "profit after tax": "net profit", "net profit after tax": "net profit",
    "net income": "net profit", "profit / (loss)": "net profit", "profit/(loss)": "net profit",
    "net profit / (loss)": "net profit", "net profit/(loss)": "net profit",
    "turnover": "revenue", "sales": "revenue", "net sales": "revenue", "total revenue": "revenue",
    "revenue from operations": "revenue", "income from operations": "revenue",
    "pbt": "profit before tax", "ebitda margin": "ebitda margin",
    "crude throughput": "crude throughput", "crude processed": "crude throughput",
    "throughput": "crude throughput",
    "maintenance expense": "maintenance cost", "maintenance expenditure": "maintenance cost",
    "maintenance spend": "maintenance cost", "repair and maintenance": "maintenance cost",
    "unplanned downtime": "unplanned downtime", "downtime": "downtime",
    "gross refining margin": "grm", "grm": "grm",
}


def parse_number(text: Any) -> Optional[float]:
    """'1,23,456.7' -> 123456.7, '(1,200)' -> -1200, '12.5%' -> 12.5, '-' -> None.
    Scale words (cr, lakh, mn) are NOT applied - the unit is kept separately so a
    crore never silently becomes a number of rupees."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    raw = str(text).strip()
    if raw.lower() in _EMPTY:
        return None
    cleaned = raw.replace("−", "-").replace(" ", "")
    m = _NUMBER.match(cleaned) or _NUMBER.match(raw)
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    value = float(whole + (m.group(2) or ""))
    negative = cleaned.startswith("(") and cleaned.endswith(")") or cleaned.startswith("-")
    return -value if negative else value


_UNIT_SUFFIX = re.compile(r"^(.*?[A-Za-z0-9])[\s_]+(cr|crore|crores|inr|rs|pct|percent|lakh|lakhs|mn|bn|usd|"
                          r"units|days|hrs|hours|mt|kl|kt)$", re.IGNORECASE)
_SUFFIX_UNITS = {"cr": "INR Cr", "crore": "INR Cr", "crores": "INR Cr", "inr": "INR", "rs": "INR", "pct": "%",
                 "percent": "%", "lakh": "INR lakh", "lakhs": "INR lakh", "mn": "mn", "bn": "bn", "usd": "USD",
                 "units": "units", "days": "days", "hrs": "hours", "hours": "hours", "mt": "MT", "kl": "KL", "kt": "kt"}
# A header that is only a unit ('INR Cr', '%', 'Value') - the column of a metric/value table.
_UNIT_ONLY = re.compile(r"^(?:(?:inr|rs\.?|₹|usd|\$)\s*)?(?:cr|crore|crores|lakh|lakhs|mn|bn|k)$|^%$|^(?:value|amount|figure|total)$",
                        re.IGNORECASE)
METRIC_LABEL_HEADERS = ("metric", "item", "particulars", "parameter", "kpi", "measure", "indicator", "line item",
                        "description", "head", "account")
# Money in different scales is one quantity: compare in INR crore.
MONEY_SCALE = {"inr": 1e-7, "rs": 1e-7, "₹": 1e-7, "inr lakh": 1e-2, "lakh": 1e-2, "inr cr": 1.0, "cr": 1.0,
               "₹ cr": 1.0, "rs cr": 1.0, "crore": 1.0, "inr crore": 1.0}


def header_unit(header: str) -> Tuple[str, Optional[str]]:
    """'Net profit (₹ Cr)' -> ('Net profit', '₹ Cr'); 'Revenue_Cr' -> ('Revenue', 'INR Cr');
    'Gross_Margin_Pct' -> ('Gross Margin', '%')."""
    header = (header or "").strip()
    m = _UNIT_IN_HEADER.search(header)
    if m:
        return header[:m.start()].strip(" :-"), m.group(1).strip()
    m = _UNIT_SUFFIX.match(header)
    if m:
        return m.group(1).replace("_", " ").strip(), _SUFFIX_UNITS[m.group(2).lower()]
    if "_" in header:
        return header.replace("_", " ").strip(), None
    return header, None


def to_crore(value: float, unit: Optional[str]) -> Optional[float]:
    """A money value in INR crore, or None when the unit is not a money unit."""
    scale = MONEY_SCALE.get((unit or "").strip().lower())
    return value * scale if scale is not None else None


_KNOWN_METRICS = ("maintenance cost", "net profit", "revenue", "downtime", "unplanned downtime", "production",
                  "crude throughput", "operating cost", "energy consumption", "availability", "grm", "ebitda",
                  "capital expenditure", "interest cost", "capacity utilisation", "fuel and loss", "yield")


def metric_from_title(title: str) -> Optional[str]:
    """'Maintenance cost by unit' -> 'maintenance cost' (only standard metric names)."""
    low = (title or "").lower()
    for name in sorted(set(_KNOWN_METRICS) | set(METRIC_SYNONYMS), key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            return METRIC_SYNONYMS.get(name, name)
    return None


def normalize_metric(name: str) -> str:
    base, _unit = header_unit(name)
    key = re.sub(r"\s+", " ", base.lower()).strip(" :.-")
    return METRIC_SYNONYMS.get(key, key)


def grid_from_text(text: str) -> List[List[str]]:
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(" | ")] if " | " in line else [c.strip() for c in line.split("\t")]
        rows.append(cells)
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


@dataclass
class Fact:
    metric: str
    metric_norm: str
    value: float
    unit: Optional[str]
    year: Optional[int]
    quarter: Optional[int] = None
    month: Optional[int] = None
    period: Optional[str] = None
    entity_label: Optional[str] = None
    cell_ref: str = ""
    source_text: str = ""
    confidence: float = 1.0


@dataclass
class ParsedTable:
    table_id: str
    headers: List[str]
    units: List[Optional[str]]
    rows: List[List[str]]
    orientation: str                 # wide_time | long | generic
    facts: List[Fact] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _is_label(cell: str) -> bool:
    return bool(cell) and parse_number(cell) is None and period_of(cell)["year"] is None


def _header_row_index(rows: List[List[str]]) -> int:
    """The first row that looks like headers: mostly non-numeric, or years."""
    for index, row in enumerate(rows[:4]):
        filled = [c for c in row if c]
        if len(filled) < 2:
            continue
        labels = sum(1 for c in filled if parse_number(c) is None or period_of(c)["year"] is not None)
        if labels >= max(2, int(len(filled) * 0.6)):
            return index
    return 0


def _column(rows: List[List[str]], index: int) -> List[str]:
    return [r[index] if index < len(r) else "" for r in rows]


def parse_table(rows: List[List[str]], document_id: str, ordinal: int,
                doc_year: Optional[int] = None, title: str = "",
                table_unit: Optional[str] = None) -> ParsedTable:
    """Rows -> headers, units, orientation and numeric facts."""
    table_id = hashlib.sha1(f"{document_id}:table:{ordinal}".encode()).hexdigest()[:16]
    if not rows:
        return ParsedTable(table_id, [], [], [], "generic")
    h = _header_row_index(rows)
    raw_headers = rows[h]
    body = [r for r in rows[h + 1:] if any(c.strip() for c in r)]
    headers, units, names = [], [], []
    for cell in raw_headers:
        name, unit = header_unit(cell)
        # A header with its unit in brackets is shown without it (as before); a
        # 'Revenue_Cr' header is kept as written - registers show their own columns.
        headers.append((name or cell) if _UNIT_IN_HEADER.search((cell or "").strip()) else cell)
        names.append(name or cell)
        units.append(unit or table_unit)
    table = ParsedTable(table_id, headers, units, body, "generic")
    if not body:
        return table
    daily = _daily_table(table, names, doc_year)
    if daily is not None:
        return daily

    periods = [period_of(c) for c in headers]
    period_cols = [i for i, p in enumerate(periods) if p["year"] is not None]

    # ---- wide: periods run across the columns --------------------------------
    if len(period_cols) >= 2 and 0 not in period_cols:
        table.orientation = "wide_time"
        label_col = 0
        # "Unit | 2018 | 2019" under a title like "Maintenance cost by unit": each row
        # is an ENTITY and the metric comes from the title - not "Unit 5" as a metric.
        label_header = (headers[0] if headers else "").lower().strip()
        title_metric = metric_from_title(title)
        rows_are_entities = bool(title_metric) and any(
            label_header == word or label_header.startswith(word + " ") for word in ENTITY_HEADERS)
        if rows_are_entities:
            table.notes.append(f"rows are {label_header}s; metric '{title_metric}' taken from the table title")
        for r_index, row in enumerate(body):
            label = row[label_col].strip() if label_col < len(row) else ""
            if not label:
                continue
            metric, unit_in_label = header_unit(label)
            entity_label = None
            if rows_are_entities:
                metric, entity_label = title_metric, label
            for c in period_cols:
                value = parse_number(row[c] if c < len(row) else None)
                if value is None:
                    continue
                p = periods[c]
                table.facts.append(Fact(
                    metric=metric, metric_norm=normalize_metric(metric), value=value,
                    unit=unit_in_label or units[c] or table_unit, year=p["year"],
                    quarter=p["quarter"], month=p["month"], period=p["label"], entity_label=entity_label,
                    cell_ref=f"R{r_index + h + 2}C{c + 1}",
                    source_text=f"{label} | {headers[c]} = {row[c]}"))
        return table

    # ---- long: one row per period (and maybe per entity) ------------------------
    period_col = None
    for i, name in enumerate(headers):
        low = _norm_header(name)
        if any(low == p or low.startswith(p + " ") or low.endswith(" " + p) for p in PERIOD_HEADERS):
            if low in _MONTH_HEADERS:
                continue                       # a month column is read together with the year
            period_col = i
            break
    if period_col is None:
        for i in range(len(headers)):
            column = _column(body, i)
            hits = sum(1 for c in column if period_of(c)["year"] is not None)
            if column and hits >= max(2, int(len(column) * 0.7)):
                period_col = i
                break
    month_col = _month_column(headers, body) if period_col is not None else None
    entity_col = None
    for i, name in enumerate(headers):
        if i in (period_col, month_col):
            continue
        low = _norm_header(name)
        if any(word == low or low.startswith(word) for word in ENTITY_HEADERS):
            column = _column(body, i)
            # 'Sites_Active: 1, 3, 4' names a count, not the entity a row is about
            if column and sum(1 for c in column if _is_label(c)) >= max(1, int(len(column) * 0.6)):
                entity_col = i
                break
    if entity_col is None and period_col is None:
        first = _column(body, 0)
        if first and sum(1 for c in first if _is_label(c)) >= max(1, int(len(first) * 0.7)):
            entity_col = 0

    # A metric / value table ('Metric | INR Cr' - 'Revenue | 252.54'): the rows are the metrics.
    if period_col is None and body and headers and (
            _norm_header(headers[0]) in METRIC_LABEL_HEADERS
            or (len(headers) >= 2 and all(_UNIT_ONLY.match((h or "").strip()) for h in headers[1:] if (h or "").strip()))):
        kv = _metric_rows(table, names, doc_year, table_unit)
        if kv is not None:
            return kv
    numeric_cols = []
    for i in range(len(headers)):
        if i in (period_col, entity_col, month_col) or _norm_header(headers[i]) in _CALENDAR_HEADERS:
            continue
        column = _column(body, i)
        numbers = sum(1 for c in column if parse_number(c) is not None)
        if column and numbers >= max(1, int(len(column) * 0.6)):
            numeric_cols.append(i)
    if not numeric_cols:
        return table

    table.orientation = "long" if period_col is not None else "generic"
    if period_col is None:
        if doc_year:
            table.notes.append(f"no period column - values dated by the document ({doc_year}), confidence 0.6")
        else:
            table.notes.append("no period column and no document date - values are undated")
    for r_index, row in enumerate(body):
        period = period_of(row[period_col]) if period_col is not None and period_col < len(row) else None
        if period_col is not None and (not period or period["year"] is None):
            continue
        if month_col is not None and period:
            month = _month_value(row[month_col] if month_col < len(row) else "")
            if month is None:
                continue
            period = {**period, "month": month, "label": f"{period['year']}-{month:02d}"}
        entity = row[entity_col].strip() if entity_col is not None and entity_col < len(row) else None
        for c in numeric_cols:
            value = parse_number(row[c] if c < len(row) else None)
            if value is None:
                continue
            year = period["year"] if period else doc_year
            table.facts.append(Fact(
                metric=names[c], metric_norm=normalize_metric(names[c]), value=value,
                unit=units[c] or table_unit, year=year,
                quarter=period["quarter"] if period else None,
                month=period["month"] if period else None,
                period=period["label"] if period else (str(doc_year) if doc_year else None),
                entity_label=entity or None, cell_ref=f"R{r_index + h + 2}C{c + 1}",
                source_text=" | ".join(x for x in row if x)[:300],
                confidence=1.0 if period else 0.6))
    return table


_MONTH_HEADERS = ("month", "mon", "month no", "month number")
_CALENDAR_HEADERS = ("year", "fy", "fy year", "month", "mon", "day", "weekday", "week", "quarter", "qtr",
                     "date", "month no", "month number", "day of week")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")
_NOT_ADDITIVE = re.compile(r"pct|percent|margin|ratio|rate|flag|index|price|per[\s_]|average|avg|%", re.IGNORECASE)
_MONTH_NAMES = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                             "nov", "dec"], start=1)}


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").replace("_", " ")).strip().lower()


def _month_value(cell: Any) -> Optional[int]:
    text = str(cell or "").strip().lower()
    number = parse_number(text)
    if number is not None and number == int(number) and 1 <= int(number) <= 12:
        return int(number)
    return _MONTH_NAMES.get(text[:3]) if text[:3] in _MONTH_NAMES else None


def _month_column(headers: List[str], body: List[List[str]]) -> Optional[int]:
    for i, name in enumerate(headers):
        if _norm_header(name) in _MONTH_HEADERS:
            column = [c for c in _column(body, i) if str(c).strip()]
            if column and sum(1 for c in column if _month_value(c) is not None) >= 0.8 * len(column):
                return i
    return None


def _daily_table(table: "ParsedTable", names: List[str], doc_year: Optional[int]) -> Optional["ParsedTable"]:
    """A day-by-day table (a daily P&L): one fact per DAY would put 30 values on
    each month and read as conflicts. Additive columns are summed per month - each
    fact says how many daily rows it adds up; ratios and flags are not summed."""
    headers, body = table.headers, table.rows
    date_col = None
    for i in range(len(headers)):
        column = [str(c).strip() for c in _column(body, i) if str(c).strip()]
        if len(column) >= 60 and sum(1 for c in column if _ISO_DATE.match(c)) >= 0.8 * len(column) \
                and len(set(column)) >= 60:
            date_col = i
            break
    if date_col is None:
        return None
    measures = []
    for c in range(len(headers)):
        low = _norm_header(headers[c])
        if c == date_col or low in _CALENDAR_HEADERS or low.endswith((" id", " code")) or \
                table.units[c] == "%" or _NOT_ADDITIVE.search(headers[c]):
            continue
        column = _column(body, c)
        if column and sum(1 for v in column if parse_number(v) is not None) >= 0.8 * len(column):
            measures.append(c)
    table.orientation = "daily"
    groups: Dict[Tuple[int, int], List[int]] = {}
    for r_index, row in enumerate(body):
        m = _ISO_DATE.match(str(row[date_col] if date_col < len(row) else "").strip())
        if m:
            groups.setdefault((int(m.group(1)), int(m.group(2))), []).append(r_index)
    for (year, month), indexes in sorted(groups.items()):
        first, last = body[indexes[0]][date_col], body[indexes[-1]][date_col]
        for c in measures:
            values = [parse_number(body[i][c] if c < len(body[i]) else None) for i in indexes]
            values = [v for v in values if v is not None]
            if not values:
                continue
            table.facts.append(Fact(
                metric=names[c], metric_norm=normalize_metric(names[c]), value=round(sum(values), 2),
                unit=table.units[c], year=year, month=month, period=f"{year}-{month:02d}",
                cell_ref=f"R{indexes[0] + 2}-R{indexes[-1] + 2}C{c + 1} (sum)",
                source_text=f"sum of {len(values)} daily rows {first} to {last}", confidence=0.9))
    monthly = len(table.facts)
    # A site / plant column: the same rows summed per YEAR and site as well, so "which
    # site had the highest revenue" is answered from the ledger (money in INR Cr).
    site_col = _site_column(headers, body, date_col)
    if site_col is not None:
        by_site: Dict[Tuple[int, str], List[int]] = {}
        for r_index, row in enumerate(body):
            m = _ISO_DATE.match(str(row[date_col] if date_col < len(row) else "").strip())
            site = str(row[site_col] if site_col < len(row) else "").strip()
            if m and site:
                by_site.setdefault((int(m.group(1)), site), []).append(r_index)
        for (year, site), indexes in sorted(by_site.items()):
            for c in measures:
                values = [parse_number(body[i][c] if c < len(body[i]) else None) for i in indexes]
                values = [v for v in values if v is not None]
                if not values:
                    continue
                total, unit, how = sum(values), table.units[c], ""
                crore = to_crore(total, unit)
                if crore is not None and (unit or "").strip().lower() not in ("inr cr", "cr", "crore", "inr crore", "₹ cr", "rs cr"):
                    total, unit, how = crore, "INR Cr", f", converted from {table.units[c]} (/ 10^7)"
                table.facts.append(Fact(
                    metric=names[c], metric_norm=normalize_metric(names[c]), value=round(total, 4), unit=unit,
                    year=year, period=str(year), entity_label=site,
                    cell_ref=f"R{indexes[0] + 2}-R{indexes[-1] + 2}C{c + 1} (sum)",
                    source_text=f"sum of {len(values)} daily rows for {headers[site_col]} {site} in {year}{how}",
                    confidence=0.9))
    skipped = [headers[c] for c in range(len(headers)) if c not in measures and c != date_col
               and c != site_col and _norm_header(headers[c]) not in _CALENDAR_HEADERS]
    table.notes.append(f"daily table: {len(body)} rows summed per month into {monthly} facts"
                       + (f", and per year and {headers[site_col]} into {len(table.facts) - monthly} facts"
                          if site_col is not None else "")
                       + (f"; not summed (ratios, flags, labels): {', '.join(skipped[:8])}" if skipped else ""))
    return table


_SITE_HEADERS = ("site", "site code", "site id", "plant", "plant code", "unit", "unit code", "location",
                 "location code", "branch", "branch code", "region", "facility", "facility code", "city")


def _site_column(headers: List[str], body: List[List[Any]], date_col: int) -> Optional[int]:
    """The column naming the site of each daily row: a site-like header with a few
    distinct text labels (not numbers, not one value per row)."""
    for c, header in enumerate(headers):
        if c == date_col or _norm_header(header) not in _SITE_HEADERS:
            continue
        column = [str(v).strip() for v in _column(body, c) if str(v).strip()]
        labels = {v for v in column if parse_number(v) is None}
        if column and len(labels) >= 1 and len(labels) <= 60 and len([v for v in column if v in labels]) >= 0.9 * len(column):
            return c
    return None


def _metric_rows(table: "ParsedTable", names: List[str], doc_year: Optional[int],
                 table_unit: Optional[str]) -> Optional["ParsedTable"]:
    """'Metric | INR Cr' tables: the metric is the row label, the unit the column's."""
    value_cols = [c for c in range(1, len(table.headers))
                  if sum(1 for v in _column(table.rows, c) if parse_number(v) is not None) >= 0.6 * len(table.rows)]
    if not value_cols:
        return None
    table.orientation = "metric_rows"
    if doc_year:
        table.notes.append(f"metric / value table - values dated by the document ({doc_year}), confidence 0.6")
    for r_index, row in enumerate(table.rows):
        label = str(row[0]).strip() if row else ""
        if not label or parse_number(label) is not None:
            continue
        metric, unit_in_label = header_unit(label)
        for c in value_cols:
            value = parse_number(row[c] if c < len(row) else None)
            if value is None:
                continue
            header = (table.headers[c] or "").strip()
            unit = unit_in_label or table.units[c] or (header if _UNIT_ONLY.match(header) and
                                                        header.lower() not in ("value", "amount", "figure", "total") else None) or table_unit
            if unit and unit.lower() in ("cr", "crore", "crores"):
                unit = "INR Cr"
            table.facts.append(Fact(
                metric=metric, metric_norm=normalize_metric(metric), value=value, unit=unit, year=doc_year,
                period=str(doc_year) if doc_year else None, cell_ref=f"R{r_index + 2}C{c + 1}",
                source_text=f"{label} | {header} = {row[c]}", confidence=0.6))
    return table


def table_markdown(headers: List[str], rows: List[List[str]], limit: int = 40) -> str:
    """Readable text for FTS and for the evidence viewer - the grid kept intact."""
    head = " | ".join(headers)
    lines = [head, " | ".join("---" for _ in headers)]
    lines += [" | ".join(r) for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows")
    return "\n".join(lines)
