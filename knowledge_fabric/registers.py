"""
Registers: tables whose rows are RECORDS - a lease register, an employee
master, a tax-filing index, a vendor contract list - not figures over time.

A register row is not "a value in some year": 'LS-2021-1001 | rent 85,000'
says nothing about 2022 just because the workbook mentions 2022 most often.
So a register:

  * keeps one evidence node per row (kind 'record'), citable on its own;
  * yields no dated facts unless it has a real period column (Period, Month,
    FY, Quarter) - an event date such as DOJ or End_Date is not a period;
  * is queried by code, not by the language model: rows are filtered by the
    conditions the question states (status, category, date window, amount)
    and counted / summed / ranked by the engine, with every input row listed.

Data quality is checked on every query against the system date: a record
marked Active whose end date has passed, a payment marked Paid with no date,
a filing dated in the future. Such rows are reported as conflicts - never
silently trusted or dropped.

Access: every query joins kf_documents and applies the caller's access
clause, exactly like passage retrieval. Columns that hold personal
identifiers (PAN, Aadhaar, UAN, bank, salary...) are never shown in an
answer unless the question asks for that column by name.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from knowledge_fabric import store, tables

MIN_ROWS, MIN_COLS = 3, 4
MAX_SHOWN = 25
CODE = re.compile(r"^[A-Z][A-Z0-9]{1,7}(?:[-/][A-Z0-9]+)+$")
CODE_IN_TEXT = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{1,7}(?:[-/][A-Z0-9]+)+)(?![\w-])")
KEY_HEADER = re.compile(r"(?:^|[\s_])(?:id|code|no|number|ref|key)$", re.I)
PERIOD_HEADER = re.compile(r"^(?:period|month|year|fy|fiscal[\s_]year|financial[\s_]year|quarter|qtr|ay|assessment[\s_]year)$", re.I)
SENSITIVE = re.compile(r"(?:^|[\s_])(?:pan|aadhaa?r|uan|ifsc|bank|account|dob|birth|phone|mobile|e-?mail|salary|ctc|basic|hra|"
                       r"pf|esi|passport|gross|net[\s_]pay)(?:$|[\s_])", re.I)
ROLES = (
    ("paid_date", re.compile(r"paid[\s_]?on|payment[\s_]?date|paid[\s_]?date|date[\s_]of[\s_]payment", re.I)),
    ("filed_date", re.compile(r"filed[\s_]?on|filing[\s_]?date|date[\s_]filed", re.I)),
    ("due_date", re.compile(r"(?:^|[\s_])due", re.I)),
    ("end_date", re.compile(r"(?:^|[\s_])(?:end|expir\w*|valid[\s_](?:till|to|until)|lapse|termination|till)(?:$|[\s_])", re.I)),
    ("start_date", re.compile(r"(?:^|[\s_])(?:start|commenc\w*|doj|joining|from|effective)(?:$|[\s_])", re.I)),
    ("review_date", re.compile(r"review", re.I)),
    ("status", re.compile(r"(?:^|[\s_])status$", re.I)),     # not 'State' - that is a place
    ("name", re.compile(r"(?:^|[\s_])(?:name|title)$|^vendor[\s_]name$|^lessor$", re.I)),
)
ACTIVE = {"active", "valid", "in force", "current", "running", "open", "live", "occupied"}
DONE = {"paid": "paid_date", "filed": "filed_date", "cleared": "paid_date", "settled": "paid_date"}
GENERIC_NAME_WORDS = {"master", "register", "index", "sample", "data", "sheet", "list", "table", "record", "records",
                      "of", "the", "and", "for", "01", "02", "03", "04", "05", "details"}
SYNONYMS = {"staff": "employee", "people": "employee", "headcount": "employee", "workforce": "employee",
            "worker": "employee", "personnel": "employee", "supplier": "vendor", "contractor": "vendor",
            "site": "property", "building": "property", "premise": "property", "premises": "property",
            "challan": "tds", "salary": "ctc", "salaries": "ctc", "pay": "ctc", "wage": "ctc", "wages": "ctc",
            "rental": "rent", "rents": "rent", "expiry": "expir", "expire": "expir", "expires": "expir",
            "expired": "expir", "expiring": "expir", "expiration": "expir", "ending": "end", "ends": "end",
            "gstr": "gst", "itr": "income", "noc": "noc", "licence": "license", "tenancy": "lease",
            "tenancies": "lease", "renting": "rent", "hire": "join", "hired": "join", "joined": "join",
            "joining": "join", "doj": "join"}
INTENT = re.compile(r"\b(?:which|list|show|how\s+many|number\s+of|count|total|sum|average|mean|median|highest|lowest|"
                    r"largest|smallest|maximum|minimum|max|min|top|bottom|most|least|expir\w*|due|overdue|unpaid|"
                    r"outstanding|pending|active|inactive|between|before|after|since|until|next|last|past|per|each|"
                    r"wise|all|any|every|biggest|cheapest|costliest|oldest|newest|latest)\b", re.I)
MONTHS = {m.lower(): i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9


# ------------------------------------------------------------------ parsing --

_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y",
                 "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d.%m.%Y", "%d-%B-%Y")


def parse_date(text: Any) -> Optional[dt.date]:
    if isinstance(text, dt.datetime):
        return text.date()
    if isinstance(text, dt.date):
        return text
    s = str(text or "").strip()
    if not s or len(s) > 30:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_/\\]+", " ", str(text or "")).strip().lower())


def _stem(word: str) -> str:
    w = word.lower()
    if w in SYNONYMS:
        return SYNONYMS[w]
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"                                      # properties -> property
    elif len(w) > 4 and w.endswith(("sses", "xes", "ches", "shes", "zes")):
        w = w[:-2]                                            # addresses, taxes, branches
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]                                            # leases -> lease, contracts -> contract
    return SYNONYMS.get(w, w)


def tokens(text: str) -> List[str]:
    parts = re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or "")).lower())
    return [_stem(p) for p in parts if p]


def _number(text: Any) -> Optional[float]:
    return tables.parse_number(text)


# ------------------------------------------------------------------ profile --

def profile(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> Optional[Dict[str, Any]]:
    """Is this table a register? -> its columns, types and roles, or None."""
    headers = [str(h or "").strip() for h in headers]
    body = [list(r) + [""] * (len(headers) - len(r)) for r in rows if any(str(c).strip() for c in r)]
    if len(headers) < MIN_COLS or len(body) < MIN_ROWS or sum(1 for h in headers if h) < MIN_COLS:
        return None
    if sum(1 for h in headers if tables.period_of(h)["year"] is not None) >= 2:
        return None                                   # periods across the columns: a time table
    columns = []
    for index, header in enumerate(headers):
        values = [str(r[index]).strip() for r in body if str(r[index]).strip()]
        filled = len(values)
        distinct = len(set(v.lower() for v in values))
        kind = "text"
        if filled:
            if sum(1 for v in values if parse_date(v)) >= 0.8 * filled:
                kind = "date"
            elif sum(1 for v in values if _number(v) is not None) >= 0.8 * filled:
                kind = "number"
            elif sum(1 for v in values if CODE.match(v)) >= 0.8 * filled:
                kind = "code"
            elif distinct <= max(12, int(0.6 * len(body))) and distinct < len(body):
                kind = "category"
        role = next((name for name, pattern in ROLES if pattern.search(header)), None)
        if role and role.endswith("_date") and kind != "date":
            role = None
        if role == "status" and kind not in ("category", "text"):
            role = None
        columns.append({"name": header, "index": index, "type": kind, "role": role,
                        "filled": filled, "unique": filled > 0 and distinct == filled,
                        "sensitive": bool(SENSITIVE.search(header)),
                        "period": bool(PERIOD_HEADER.match(_norm(header))),
                        "values": sorted(set(values))[:40] if kind == "category" else []})
    key = next((c for c in columns if c["unique"] and c["filled"] >= 0.9 * len(body)
                and (KEY_HEADER.search(c["name"]) or c["type"] == "code")), None)
    composite = None
    if key is None:
        coded = [c for c in columns if c["type"] == "code"]
        if coded:
            other = next((c for c in columns if c is not coded[0] and c["type"] in ("category", "text", "date")), None)
            composite = [coded[0]["index"]] + ([other["index"]] if other else [])
        else:
            # An event list ('Date | Milestone | Category'): a dated row with its own title.
            dated = next((c for c in columns if c["type"] == "date"), None)
            titled = next((c for c in columns if c["type"] == "text" and c["unique"]), None)
            if not (dated and titled):
                return None
            composite = [dated["index"], titled["index"]]
        if len({tuple(str(r[i]).strip() for i in composite) for r in body}) < 0.9 * len(body):
            composite.append(-1)                       # still not unique: add the row number
    typed = sum(1 for c in columns if c["role"] or c["type"] in ("date", "number"))
    if typed < 2:
        return None
    return {"key": key["name"] if key else None, "key_index": key["index"] if key else None,
            "composite": composite, "columns": columns,
            "has_period": any(c["period"] for c in columns),
            "sensitive": [c["name"] for c in columns if c["sensitive"]]}


def is_event_register(prof: Dict[str, Any]) -> bool:
    """A dated list of happenings ('1998-09-01 | Mumbai commercial hub opens'). Its rows
    are narrative - they stay findable by passage search, unlike ledger rows."""
    composite = prof.get("composite") or []
    cols = {c["index"]: c for c in prof["columns"]}
    return bool(composite) and composite[0] in cols and cols[composite[0]]["type"] == "date"


def record_key(prof: Dict[str, Any], row: Sequence[str], row_index: int) -> str:
    if prof.get("key_index") is not None:
        return str(row[prof["key_index"]]).strip()
    parts = []
    for i in prof.get("composite") or []:
        parts.append(f"row {row_index + 1}" if i == -1 else str(row[i]).strip())
    return " · ".join(p for p in parts if p) or f"row {row_index + 1}"


def record_text(headers: Sequence[str], row: Sequence[str]) -> str:
    return " · ".join(f"{h}: {str(v).strip()}" for h, v in zip(headers, row) if str(v).strip())


def store_register(conn, register_id: str, document_id: str, table_node: str, name: str,
                   headers: Sequence[str], rows: Sequence[Sequence[str]], prof: Dict[str, Any],
                   add_record_node) -> int:
    """Persist the register and one record (and one evidence node) per row."""
    conn.execute("INSERT OR REPLACE INTO kf_registers(register_id, document_id, table_node, name, key_column, "
                 "columns, n_records) VALUES (?,?,?,?,?,?,?)",
                 (register_id, document_id, table_node, name, prof.get("key"),
                  store.dumps(prof["columns"]), len(rows)))
    stored = 0
    for index, row in enumerate(rows[:5000]):
        cells = [str(c).strip() for c in row] + [""] * (len(headers) - len(row))
        if not any(cells):
            continue
        key = record_key(prof, cells, index)
        text = record_text(headers, cells)
        node_id = add_record_node(text, f"{name} {key}".strip(), "event" if is_event_register(prof) else "record")
        codes = sorted({c for c in cells if CODE.match(c)})
        conn.execute("INSERT OR REPLACE INTO kf_records(register_id, row_index, record_key, node_id, codes, data) "
                     "VALUES (?,?,?,?,?,?)",
                     (register_id, index, key, node_id, " ".join(codes),
                      store.dumps({h: c for h, c in zip(headers, cells)})))
        stored += 1
    return stored


# ------------------------------------------------------------ data quality --

def quality(reg: Dict[str, Any], records: Iterable[Dict[str, Any]], today: dt.date) -> List[Dict[str, Any]]:
    """Rows that contradict themselves or the calendar - checked, never assumed."""
    cols = reg["columns"]
    role = {c["role"]: c["name"] for c in cols if c["role"]}
    tenure = next((c["name"] for c in cols if "tenure" in c["name"].lower() and c["type"] == "number"), None)
    findings, seen_keys = [], {}
    for rec in records:
        data, key = rec["data"], rec["record_key"]
        if key in seen_keys and reg.get("key_column"):
            findings.append(_finding(reg, rec, "duplicate_key", f"{reg['key_column']} {key} appears on two rows"))
        seen_keys[key] = True
        status = (data.get(role.get("status", ""), "") or "").strip()
        low = status.lower()
        end = parse_date(data.get(role.get("end_date", "")))
        start = parse_date(data.get(role.get("start_date", "")))
        if status and low in ACTIVE and end and end < today:
            findings.append(_finding(reg, rec, "status_contradicts_date",
                                     f"{role['status']} says '{status}' but {role['end_date']} {end.isoformat()} has passed"))
        window = re.search(r"expir\w*[\s_]*(\d+)\s*d", low)
        if window and end and not (today <= end <= today + dt.timedelta(days=int(window.group(1)))):
            findings.append(_finding(reg, rec, "flag_contradicts_date",
                                     f"flagged '{status}' but {role['end_date']} is {end.isoformat()} "
                                     f"({'already past' if end < today else 'further out'})"))
        for word, date_role in DONE.items():
            if low == word and date_role in role and not (data.get(role[date_role]) or "").strip():
                findings.append(_finding(reg, rec, "done_without_date",
                                         f"marked '{status}' but no {role[date_role]} is recorded"))
        for date_role in ("paid_date", "filed_date"):
            when = parse_date(data.get(role.get(date_role, "")))
            if when and when > today:
                findings.append(_finding(reg, rec, "future_completion",
                                         f"{role[date_role]} {when.isoformat()} is after today ({today.isoformat()})"))
        if start and end and start > end:
            findings.append(_finding(reg, rec, "start_after_end",
                                     f"{role['start_date']} {start.isoformat()} is after {role['end_date']} {end.isoformat()}"))
        if tenure and start and end:
            stated = _number(data.get(tenure))
            months = (end.year - start.year) * 12 + (end.month - start.month) + (1 if end.day > start.day else 0)
            if stated is not None and abs(stated - months) > 1:
                findings.append(_finding(reg, rec, "tenure_mismatch",
                                         f"{tenure} says {stated:g} but {start.isoformat()} to {end.isoformat()} "
                                         f"is about {months} months"))
    return findings


def _finding(reg: Dict[str, Any], rec: Dict[str, Any], check: str, detail: str) -> Dict[str, Any]:
    return {"type": "record", "check": check, "register": reg["name"], "title": reg.get("title"),
            "document_id": reg.get("document_id"), "record_key": rec["record_key"], "node_id": rec.get("node_id"),
            "detail": detail}


# ------------------------------------------------------------------ query ----

@dataclass
class RegisterAnswer:
    register: Dict[str, Any]
    mode: str                                    # lookup | list | count | sum | average | max | min | group
    conditions: List[str]
    records: List[Dict[str, Any]]                # every matching record
    shown_columns: List[str]
    calculations: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    as_of: str = ""
    field_asked: Optional[str] = None
    measure: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        reg = {k: v for k, v in self.register.items() if k != "columns"}
        return {"register": reg, "mode": self.mode, "conditions": self.conditions,
                "records": [{"record_key": r["record_key"], "node_id": r.get("node_id"),
                             "data": {c: r["data"].get(c, "") for c in self.shown_columns},
                             "evidence_id": r.get("evidence_id")} for r in self.records],
                "total_matching": len(self.records), "shown_columns": self.shown_columns,
                "findings": self.findings, "as_of": self.as_of, "field_asked": self.field_asked,
                "measure": self.measure, "notes": self.notes}


def accessible_registers(conn, ctx) -> List[Dict[str, Any]]:
    access_sql, params = ctx.clause("d")
    out = []
    for row in conn.execute(
            "SELECT r.register_id, r.document_id, r.table_node, r.name, r.key_column, r.columns, r.n_records, "
            "d.title, d.filename, d.origin, d.authority, d.confidentiality, d.document_type, d.status, d.year, "
            f"d.source_hash FROM kf_registers r JOIN kf_documents d ON d.document_id = r.document_id WHERE {access_sql}",
            params):
        reg = dict(row)
        reg["columns"] = store.loads(reg["columns"], [])
        out.append(reg)
    return out


def event_label(text: str) -> str:
    """'Date: 2020-04-01 · Milestone: COVID ... · Category: Risk · Impact: Volume shock'
    -> '2020-04-01 COVID continuity protocol (Risk; impact: volume shock)'."""
    fields = {}
    for part in (text or "").split(" · "):
        if ": " in part:
            key, value = part.split(": ", 1)
            fields[key.strip().lower()] = value.strip()
    date = next((v for k, v in fields.items() if "date" in k), "")
    title = next((v for k, v in fields.items() if k in ("milestone", "event", "title", "description", "name")), "")
    extra = [fields[k] for k in ("category", "type") if fields.get(k)]
    if fields.get("impact"):
        extra.append(f"impact: {fields['impact']}")
    if not title:
        flat = re.sub(r"\s+", " ", text or "").strip()
        return flat if len(flat) <= 140 else flat[:140].rsplit(" ", 1)[0] + " ..."
    return f"{date} {title}".strip() + (f" ({'; '.join(extra)})" if extra else "")


_NAME_COLUMNS = ("name", "city", "site name", "plant name", "unit name", "location", "facility name", "title")


def names_for_codes(ctx, codes: Iterable[str], conn=None) -> Dict[str, str]:
    """'PER' -> 'Perimerian': a code used in a ledger, named by the register whose
    records are keyed by it (Site_Openings: Site_Code -> City). Access-filtered;
    a code no register names stays as written."""
    wanted = sorted({str(c).strip() for c in codes if str(c or "").strip()})
    if not wanted:
        return {}
    if conn is None:
        with store.connection() as fresh:
            return names_for_codes(ctx, wanted, fresh)
    out: Dict[str, str] = {}
    by_id = {r["register_id"]: r for r in accessible_registers(conn, ctx)}
    if not by_id:
        return out
    for row in conn.execute(
            f"SELECT register_id, record_key, data FROM kf_records WHERE register_id IN ({store.placeholders(list(by_id))}) "
            f"AND record_key IN ({store.placeholders(wanted)})", list(by_id) + wanted):
        reg = by_id[row["register_id"]]
        column = next((c["name"] for name in _NAME_COLUMNS for c in reg["columns"]
                       if _norm(c["name"]) == name and c["name"] != reg.get("key_column")), None)
        value = (store.loads(row["data"], {}) or {}).get(column) if column else None
        if value and str(value).strip() and str(value).strip() != row["record_key"]:
            out.setdefault(row["record_key"], str(value).strip())
    return out


def _load_records(conn, register_id: str) -> List[Dict[str, Any]]:
    return [{"row_index": r["row_index"], "record_key": r["record_key"], "node_id": r["node_id"],
             "data": store.loads(r["data"], {})}
            for r in conn.execute("SELECT row_index, record_key, node_id, data FROM kf_records WHERE register_id = ? "
                                  "ORDER BY row_index", (register_id,))]


def _name_tokens(reg: Dict[str, Any]) -> set:
    words = set(tokens(reg["name"])) | set(tokens(reg.get("title") or ""))
    return {w for w in words if w not in GENERIC_NAME_WORDS and not w.isdigit()}


def _category_hits(reg: Dict[str, Any], question_norm: str) -> List[Tuple[Dict[str, Any], str]]:
    hits = []
    for col in reg["columns"]:
        if col["type"] != "category" or col["sensitive"]:
            continue
        for value in col["values"]:
            v = _norm(value)
            if len(v) < 2 or v in ("yes", "no", "na", "n a", "-", "nil", "none"):
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", question_norm):
                hits.append((col, value))
    # "Property Tax" also contains "tax": keep the longest value matched per phrase
    hits.sort(key=lambda cv: -len(cv[1]))
    kept, spans = [], []
    for col, value in hits:
        v = _norm(value)
        if any(v in s for s in spans):
            continue
        spans.append(v)
        kept.append((col, value))
    return kept


def _score(reg: Dict[str, Any], q_tokens: set, question_norm: str) -> Tuple[float, List]:
    name = _name_tokens(reg)
    header = {t for c in reg["columns"] for t in tokens(c["name"])}
    cats = _category_hits(reg, question_norm)
    score = 3 * len(name & q_tokens) + 1 * len((header - name) & q_tokens) + 2 * len(cats)
    return score, cats


def _when(phrase: str, today: dt.date) -> Optional[Tuple[dt.date, dt.date]]:
    """'June 2026' / '2026' / '30 June 2026' / '2026-06-30' -> (first day, last day)."""
    p = phrase.strip().lower().rstrip(".?,")
    exact = parse_date(phrase.strip().rstrip(".?,"))
    if exact:
        return exact, exact
    m = re.match(r"^(\d{1,2})\s+([a-z]+)\s+((?:19|20)\d{2})$", p)
    if m and m.group(2) in MONTHS:
        day = dt.date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
        return day, day
    m = re.match(r"^([a-z]+)[\s,-]+((?:19|20)\d{2})$", p)
    if m and m.group(1) in MONTHS:
        first = dt.date(int(m.group(2)), MONTHS[m.group(1)], 1)
        nxt = dt.date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return first, nxt - dt.timedelta(days=1)
    m = re.match(r"^((?:19|20)\d{2})$", p)
    if m:
        return dt.date(int(p), 1, 1), dt.date(int(p), 12, 31)
    if p in ("today", "now"):
        return today, today
    return None


_WHEN = r"((?:\d{1,2}\s+)?[A-Za-z]+[\s,-]+(?:19|20)\d{2}|(?:19|20)\d{2}-\d{2}-\d{2}|\d{1,2}[-/]\d{1,2}[-/](?:19|20)\d{2}|(?:19|20)\d{2}|today)"


def _date_window(question: str, today: dt.date) -> Optional[Tuple[dt.date, dt.date, str]]:
    q = question.lower()
    m = re.search(r"\b(?:next|coming|within(?:\s+the\s+next)?|in\s+the\s+next)\s+(\d+)\s+(day|week|month|year)s?", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
        return today, today + dt.timedelta(days=days), f"between {today.isoformat()} and {(today + dt.timedelta(days=days)).isoformat()} (the next {n} {unit}s)"
    m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s+(day|week|month|year)s?", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
        return today - dt.timedelta(days=days), today, f"between {(today - dt.timedelta(days=days)).isoformat()} and {today.isoformat()}"
    m = re.search(rf"\bbetween\s+{_WHEN}\s+and\s+{_WHEN}", question, re.I)
    if m:
        a, b = _when(m.group(1), today), _when(m.group(2), today)
        if a and b:
            return a[0], b[1], f"between {a[0].isoformat()} and {b[1].isoformat()}"
    m = re.search(rf"\b(before|by|until|till|up\s+to)\s+{_WHEN}", question, re.I)
    if m:
        w = _when(m.group(2), today)
        if w:
            if m.group(1).lower() == "before":
                return dt.date.min, w[0] - dt.timedelta(days=1), f"before {w[0].isoformat()}"
            return dt.date.min, w[1], f"on or before {w[1].isoformat()}"
    m = re.search(rf"\b(after|since|from)\s+{_WHEN}", question, re.I)
    if m:
        w = _when(m.group(2), today)
        if w:
            if m.group(1).lower() == "after":
                return w[1] + dt.timedelta(days=1), dt.date.max, f"after {w[1].isoformat()}"
            return w[0], dt.date.max, f"on or after {w[0].isoformat()}"
    m = re.search(rf"\b(?:in|during)\s+{_WHEN}", question, re.I)
    if m:
        w = _when(m.group(1), today)
        if w:
            return w[0], w[1], f"between {w[0].isoformat()} and {w[1].isoformat()}"
    if re.search(r"\bthis\s+year\b", q):
        return dt.date(today.year, 1, 1), dt.date(today.year, 12, 31), f"in {today.year}"
    if re.search(r"\bthis\s+month\b", q):
        first = today.replace(day=1)
        nxt = dt.date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return first, nxt - dt.timedelta(days=1), f"in {first.strftime('%B %Y')}"
    return None


def _pick_column(reg: Dict[str, Any], q_tokens: set, kinds: Tuple[str, ...], exclude=()) -> Optional[Dict[str, Any]]:
    best, best_score = None, 0
    q_tokens = {t for t in q_tokens if t not in store._STOPWORDS and len(t) > 1}   # 'in' is not Lock_In_Months
    for col in reg["columns"]:
        if col["type"] not in kinds or col["name"] in exclude:
            continue
        overlap = len(set(tokens(col["name"])) & q_tokens - {"inr", "rs", "amount", "date"})
        if overlap > best_score:
            best, best_score = col, overlap
    return best


def _date_column(reg: Dict[str, Any], question: str, q_tokens: set) -> Optional[Dict[str, Any]]:
    named = _pick_column(reg, q_tokens, ("date",))
    if named:
        return named
    q = question.lower()
    by_role = {c["role"]: c for c in reg["columns"] if c["role"] and c["type"] == "date"}
    for pattern, role in ((r"expir|\bend|lapse|renew|terminat", "end_date"), (r"\bdue\b|overdue", "due_date"),
                          (r"\bfil(?:ed|ing)\b", "filed_date"), (r"\bpaid\b|payment", "paid_date"),
                          (r"\bjoin|hire|doj", "start_date"), (r"\bstart|commenc|begin", "start_date"),
                          (r"review", "review_date")):
        if re.search(pattern, q) and role in by_role:
            return by_role[role]
    for role in ("end_date", "due_date", "start_date"):
        if role in by_role:
            return by_role[role]
    return next((c for c in reg["columns"] if c["type"] == "date"), None)


_AMOUNT = re.compile(r"\b(above|over|more\s+than|greater\s+than|exceeding|at\s+least|below|under|less\s+than|at\s+most)\s*"
                     r"(?:inr|rs\.?|₹)?\s*([\d,.]+)\s*(lakhs?|lacs?|crores?|cr|k|thousand|million|mn)?", re.I)
_SCALE = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7, "k": 1e3,
          "thousand": 1e3, "million": 1e6, "mn": 1e6}


def _holds_measures(reg: Dict[str, Any], question: str) -> bool:
    """A question naming a business measure (revenue, profit, downtime ...) is a
    register question only when the register has a column for that measure."""
    from knowledge_fabric import planner
    low = (question or "").lower()
    asked = [(phrase, metric) for phrase, metric in planner._METRIC_WORDS.items()
             if re.search(rf"\b{re.escape(phrase)}\b", low)]
    if not asked:
        return True
    columns = set()
    for col in reg.get("columns", []):
        columns |= set(tokens(col.get("name", "").replace("_", " ")))
    for phrase, metric in asked:
        need = (set(tokens(phrase)) | set(tokens(metric))) - {"net", "total"}
        if need & columns:
            return True
    return False


def answer(question: str, ctx, today: Optional[dt.date] = None, conn=None) -> Optional[RegisterAnswer]:
    """The records the question asks for, from the best-matching register -
    or None when the question is not about register records."""
    if conn is None:
        with store.connection() as fresh:
            return answer(question, ctx, today, fresh)
    today = today or dt.date.today()
    question = (question or "").strip()
    if not question or not getattr(ctx, "authenticated", False):
        return None
    registers = accessible_registers(conn, ctx)
    if not registers:
        return None
    q_norm = _norm(question)
    q_tokens = {t for t in tokens(question) if t not in store._STOPWORDS}
    by_id = {r["register_id"]: r for r in registers}

    # ---- a record named by its code: look it up ------------------------------
    codes = [c for c in CODE_IN_TEXT.findall(question)]
    if codes:
        hits = []
        for code in codes:
            for row in conn.execute(
                    "SELECT register_id, row_index, record_key, node_id, data FROM kf_records "
                    f"WHERE register_id IN ({store.placeholders(list(by_id))}) AND "
                    "(record_key = ? OR (' ' || codes || ' ') LIKE ?)", list(by_id) + [code, f"% {code} %"]):
                hits.append((row, code))
        if hits:
            # prefer the register where the code is the record's own key
            hits.sort(key=lambda h: (h[0]["record_key"] != h[1], -_score(by_id[h[0]["register_id"]], q_tokens, q_norm)[0]))
            reg = by_id[hits[0][0]["register_id"]]
            recs = [{"row_index": h[0]["row_index"], "record_key": h[0]["record_key"], "node_id": h[0]["node_id"],
                     "data": store.loads(h[0]["data"], {})} for h in hits if h[0]["register_id"] == reg["register_id"]]
            asked = _pick_column(reg, q_tokens - set(tokens(" ".join(codes))), ("number", "date", "category", "text", "code"),
                                 exclude=(reg.get("key_column"),))
            shown = _shown(reg, [asked["name"]] if asked else [], question, lookup=True)
            result = RegisterAnswer(reg, "lookup", [f"{reg.get('key_column') or 'record'} = {', '.join(codes)}"], recs, shown,
                                    as_of=today.isoformat(), field_asked=asked["name"] if asked else None)
            result.findings = quality(_reg_meta(reg), recs, today)
            return result

    if not INTENT.search(question):
        return None
    scored = sorted(((_score(r, q_tokens, q_norm), r) for r in registers), key=lambda s: -s[0][0])
    (best_score, cats), reg = scored[0]
    if best_score < 3 or not (_name_tokens(reg) & q_tokens):
        return None                                   # the question does not name this kind of record
    if not _holds_measures(reg, question):
        return None                                   # 'which site had the highest revenue' asks for figures this register lacks

    records = _load_records(conn, reg["register_id"])
    role = {c["role"]: c for c in reg["columns"] if c["role"]}
    conditions: List[str] = []
    matched = records

    # ---- category values named in the question ------------------------------
    by_col: Dict[str, List[str]] = {}
    for col, value in cats:
        by_col.setdefault(col["name"], []).append(value)
    for name, values in by_col.items():
        wanted = {v.lower() for v in values}
        matched = [r for r in matched if (r["data"].get(name) or "").strip().lower() in wanted]
        conditions.append(f"{name} = {' or '.join(values)}")

    # ---- not paid / not filed / overdue -------------------------------------------
    q = question.lower()
    status_col = role.get("status")
    named_status = bool(status_col and status_col["name"] in by_col)       # 'Unpaid' was matched as a value
    if status_col and not named_status and re.search(r"\b(?:unpaid|not\s+paid|outstanding|pending\s+payment)\b", q):
        matched = [r for r in matched if (r["data"].get(status_col["name"]) or "").strip().lower() not in ("paid", "cleared", "settled")]
        conditions.append(f"{status_col['name']} is not Paid")
    if status_col and not named_status and re.search(r"\b(?:unfiled|not\s+filed|pending\s+filing)\b", q):
        matched = [r for r in matched if (r["data"].get(status_col["name"]) or "").strip().lower() != "filed"]
        conditions.append(f"{status_col['name']} is not Filed")
    if re.search(r"\boverdue\b", q):
        due = role.get("due_date") or role.get("end_date")
        if status_col and any(v.lower() == "overdue" for v in status_col["values"]) and not by_col.get(status_col["name"]):
            matched = [r for r in matched if (r["data"].get(status_col["name"]) or "").strip().lower() == "overdue"]
            conditions.append(f"{status_col['name']} = Overdue")
        elif due:
            done = {"paid", "filed", "compliant", "completed", "closed", "cleared"}
            matched = [r for r in matched if (d := parse_date(r["data"].get(due["name"]))) and d < today
                       and (not status_col or (r["data"].get(status_col["name"]) or "").strip().lower() not in done)]
            conditions.append(f"{due['name']} before {today.isoformat()} and not completed")
    if re.search(r"\b(?:already\s+)?expired\b", q) and role.get("end_date"):
        end = role["end_date"]["name"]
        matched = [r for r in matched if (d := parse_date(r["data"].get(end))) and d < today]
        conditions.append(f"{end} before {today.isoformat()}")

    # ---- a date window --------------------------------------------------------------
    window = _date_window(question, today)
    date_col = None
    if window:
        date_col = _date_column(reg, question, q_tokens)
        if date_col:
            lo, hi, words = window
            matched = [r for r in matched if (d := parse_date(r["data"].get(date_col["name"]))) and lo <= d <= hi]
            conditions.append(f"{date_col['name']} {words}")

    # ---- an amount threshold -----------------------------------------------------------
    amount = _AMOUNT.search(question)
    amount_col = None
    if amount:
        amount_col = _pick_column(reg, q_tokens, ("number",)) or next((c for c in reg["columns"] if c["type"] == "number"), None)
        value = float(amount.group(2).replace(",", "") or 0) * _SCALE.get((amount.group(3) or "").lower(), 1)
        op = amount.group(1).lower()
        above = op.startswith(("above", "over", "more", "greater", "exceed", "at least"))
        if amount_col:
            def keep(r):
                v = _number(r["data"].get(amount_col["name"]))
                return v is not None and (v > value if above and "least" not in op else v >= value if above
                                          else v < value if "most" not in op else v <= value)
            matched = [r for r in matched if keep(r)]
            conditions.append(f"{amount_col['name']} {op} {value:,.0f}")

    # ---- what to compute -----------------------------------------------------------------
    mode, measure = "list", None
    measure_col = _pick_column(reg, q_tokens - _NOT_A_MEASURE, ("number",))
    if re.search(r"\b(?:how\s+many|number\s+of|count)\b", q):
        mode = "count"
    if re.search(r"\b(?:total|sum|overall|combined|aggregate)\b", q) and measure_col:
        mode, measure = "sum", measure_col
    elif re.search(r"\b(?:average|mean|avg)\b", q) and measure_col:
        mode, measure = "average", measure_col
    elif re.search(r"\b(?:highest|largest|maximum|max|biggest|costliest|most\s+expensive|top)\b", q) and measure_col:
        mode, measure = "max", measure_col
    elif re.search(r"\b(?:lowest|smallest|minimum|min|cheapest|least)\b", q) and measure_col:
        mode, measure = "min", measure_col
    group_col = None
    g = re.search(r"\b(?:by|per|each|across)\s+([a-z_ ]{3,30}?)(?:\s+(?:and|with|for|in|of)\b|[?.,]|$)|\b([a-z]+)[\s-]wise\b", q)
    if g:
        group_col = _pick_column(reg, set(tokens(g.group(1) or g.group(2) or "")), ("category", "text"))
        if group_col and group_col["unique"]:
            group_col = None
    if group_col and mode in ("count", "list", "sum"):
        mode = "group"
    if mode == "list" and measure_col:                # 'unpaid property tax' -> also total the tax
        measure = measure_col

    shown = _shown(reg, [c for c in (date_col["name"] if date_col else None,
                                     amount_col["name"] if amount_col else None,
                                     measure["name"] if measure else None,
                                     group_col["name"] if group_col else None) if c]
                   + list(by_col), question)
    result = RegisterAnswer(reg, mode, conditions, matched, shown, as_of=today.isoformat(),
                            measure=measure["name"] if measure else None)
    result.calculations = _calculations(result, reg, matched, measure, group_col, mode)
    result.findings = _relevant_findings(reg, records, matched, conditions, date_col, today, q)
    if not conditions and mode == "list":
        result.notes.append(f"no condition was recognised in the question - all {len(records)} records are listed")
    return result


def _reg_meta(reg: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": reg["name"], "title": reg.get("title"), "document_id": reg.get("document_id"),
            "columns": reg["columns"], "key_column": reg.get("key_column")}


def _relevant_findings(reg, records, matched, conditions, date_col, today, q) -> List[Dict[str, Any]]:
    """Quality problems in the matching rows, plus rows the register itself
    flags for what was asked but the dates do not support (and vice versa)."""
    all_findings = quality(_reg_meta(reg), records, today)
    keys = {r["record_key"] for r in matched}
    out = [f for f in all_findings if f["record_key"] in keys]
    if date_col or re.search(r"expir|unpaid|paid|overdue|due|active", q):
        out += [f for f in all_findings if f["record_key"] not in keys
                and f["check"] in ("flag_contradicts_date", "done_without_date", "status_contradicts_date",
                                   "future_completion")]
    unique, seen = [], set()
    for f in out:
        k = (f["record_key"], f["check"])
        if k not in seen:
            seen.add(k)
            unique.append(f)
    return unique


_NOT_A_NAME = re.compile(r"path|file|doc|remark|note|summary|comment|clause|address|folder", re.I)
# Words of a date window or of the question's grammar - not the name of an amount.
_NOT_A_MEASURE = {"day", "week", "month", "year", "next", "last", "date", "due", "inr", "rs", "amount", "the", "of"}


def _shown(reg: Dict[str, Any], wanted: List[str], question: str, lookup: bool = False) -> List[str]:
    """The columns an answer shows: key, a name, what the question used, status.
    Personal identifiers only when the question names them."""
    q_tokens = set(tokens(question))
    cols = reg["columns"]

    def allowed(c):
        return not c["sensitive"] or (set(tokens(c["name"])) & q_tokens)

    order: List[str] = []
    key = reg.get("key_column") or next((c["name"] for c in cols if c["type"] == "code"), None)
    if key:
        order.append(key)
    name = next((c["name"] for c in cols if c["role"] == "name" and allowed(c)), None) or \
        next((c["name"] for c in cols if c["type"] == "code" and c["name"] != key and allowed(c)), None) or \
        next((c["name"] for c in cols if c["type"] == "text" and c["unique"] and allowed(c) and c["name"] != key
              and not _NOT_A_NAME.search(c["name"])), None)
    if name:
        order.append(name)
    for w in wanted:
        col = next((c for c in cols if c["name"] == w), None)
        if col and allowed(col) and w not in order:
            order.append(w)
    status = next((c["name"] for c in cols if c["role"] == "status"), None)
    if status and status not in order:
        order.append(status)
    if lookup:                                        # one record: show it whole, minus identifiers
        for c in cols:
            if len(order) >= 9:
                break
            if c["name"] not in order and allowed(c):
                order.append(c["name"])
    else:
        for role in ("end_date", "due_date", "start_date"):
            col = next((c["name"] for c in cols if c["role"] == role), None)
            if col and col not in order and len(order) < 6:
                order.append(col)
    return order[:9]


def _calculations(result: RegisterAnswer, reg, matched, measure, group_col, mode) -> List[Dict[str, Any]]:
    label = f"{reg['name']}"
    inputs = [{"title": r["record_key"], "year": "", "node_id": r.get("node_id")} for r in matched]
    calcs = [{"name": f"{label}: records matching", "value": len(matched), "unit": "records",
              "method": "count of rows meeting: " + ("; ".join(result.conditions) or "all rows"),
              "inputs": inputs[:200], "notes": [], "ok": True}]
    if measure:
        values = [(r, _number(r["data"].get(measure["name"]))) for r in matched]
        values = [(r, v) for r, v in values if v is not None]
        skipped = len(matched) - len(values)
        notes = [f"{skipped} matching record(s) have no {measure['name']} value"] if skipped else []
        if values and mode in ("sum", "group", "list"):
            calcs.append({"name": f"{label}: total {measure['name']}", "value": round(sum(v for _, v in values), 2),
                          "unit": _unit(measure["name"]), "method": f"sum of {measure['name']} over the matching records",
                          "inputs": [{"title": r["record_key"], "year": "", "value": v, "node_id": r.get("node_id")} for r, v in values][:200],
                          "notes": notes, "ok": True})
        elif values and mode == "average":
            calcs.append({"name": f"{label}: average {measure['name']}", "value": round(sum(v for _, v in values) / len(values), 2),
                          "unit": _unit(measure["name"]), "method": f"mean of {measure['name']} over {len(values)} records",
                          "inputs": [{"title": r["record_key"], "year": "", "value": v, "node_id": r.get("node_id")} for r, v in values][:200],
                          "notes": notes, "ok": True})
        elif values and mode in ("max", "min"):
            pick = max(values, key=lambda rv: rv[1]) if mode == "max" else min(values, key=lambda rv: rv[1])
            ties = [r["record_key"] for r, v in values if v == pick[1]]
            calcs.append({"name": f"{label}: {'highest' if mode == 'max' else 'lowest'} {measure['name']}",
                          "value": pick[1], "unit": _unit(measure["name"]),
                          "method": f"{mode} of {measure['name']} over {len(values)} records - {', '.join(ties[:5])}",
                          "inputs": [{"title": k, "year": ""} for k in ties[:20]], "notes": notes, "ok": True})
            result.records = sorted(matched, key=lambda r: -(_number(r["data"].get(measure["name"])) or float("-inf"))
                                    if mode == "max" else (_number(r["data"].get(measure["name"])) or float("inf")))
        elif not values and mode in ("sum", "average", "max", "min"):
            calcs.append({"name": f"{label}: {mode} {measure['name']}", "value": None, "unit": None,
                          "method": f"no {measure['name']} values among the matching records", "inputs": [],
                          "notes": notes, "ok": False})
    if group_col and mode == "group":
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in matched:
            groups.setdefault((r["data"].get(group_col["name"]) or "(blank)").strip() or "(blank)", []).append(r)
        for gname, rows in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:15]:
            entry = {"name": f"{label}: {group_col['name']} = {gname}", "value": len(rows), "unit": "records",
                     "method": f"count of matching records with {group_col['name']} = {gname}",
                     "inputs": [{"title": r["record_key"], "year": ""} for r in rows][:100], "notes": [], "ok": True}
            calcs.append(entry)
            if measure:
                vals = [v for v in (_number(r["data"].get(measure["name"])) for r in rows) if v is not None]
                if vals:
                    calcs.append({"name": f"{label}: total {measure['name']} for {group_col['name']} = {gname}",
                                  "value": round(sum(vals), 2), "unit": _unit(measure["name"]),
                                  "method": f"sum of {measure['name']} over {len(vals)} records",
                                  "inputs": [{"title": r["record_key"], "year": ""} for r in rows][:100],
                                  "notes": [], "ok": True})
    return calcs


def _unit(column: str) -> Optional[str]:
    low = column.lower()
    if "inr" in low or "rent" in low or "amount" in low or "value" in low or "ctc" in low or "deposit" in low or "tax" in low:
        return "INR"
    if "pct" in low or "percent" in low:
        return "%"
    if "month" in low:
        return "months"
    if "day" in low:
        return "days"
    return None


# ---------------------------------------------------------------- rendering --

def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"
    return str(value)


def render(ans: RegisterAnswer, calc_ids: List[str]) -> str:
    """The answer as markdown. Every row carries its evidence id; every
    computed number carries its calculation id."""
    reg = ans.register
    source = f"{reg['name']} in {reg.get('title') or reg.get('filename')}"
    lines: List[str] = []
    n = len(ans.records)
    count_id = calc_ids[0] if calc_ids else ""
    if ans.mode == "lookup":
        rec = ans.records[0] if ans.records else None
        if rec and ans.field_asked:
            lines.append(f"**{rec['record_key']}** - {ans.field_asked}: **{rec['data'].get(ans.field_asked) or '(blank)'}** "
                         f"[{rec['evidence_id']}] (from {source}).")
        elif rec:
            lines.append(f"**{rec['record_key']}** in {source} [{rec['evidence_id']}]:")
    else:
        where = "; ".join(ans.conditions) if ans.conditions else "all records"
        lines.append(f"**{n} record{'s' if n != 1 else ''}** in {source} match: {where} [{count_id}]. "
                     f"Dates are compared with today, {ans.as_of}.")
        for calc, cid in list(zip(ans.calculations, calc_ids))[1:]:
            if calc.get("ok") and calc.get("value") is not None:
                unit = f" {calc['unit']}" if calc.get("unit") and calc["unit"] not in ("records",) else ""
                name = calc["name"].split(": ", 1)[-1]
                lines.append(f"- {name}: **{_fmt(calc['value'])}{unit}** [{cid}]")
            elif not calc.get("ok"):
                lines.append(f"- {calc['method']}.")
    if ans.records and ans.mode != "group":
        cols = ans.shown_columns
        lines.append("")
        lines.append("| " + " | ".join(cols) + " | Evidence |")
        lines.append("| " + " | ".join("---" for _ in cols) + " | --- |")
        for rec in ans.records[:MAX_SHOWN]:
            cells = [str(rec["data"].get(c, "")).replace("|", "/") or "-" for c in cols]
            lines.append("| " + " | ".join(cells) + f" | [{rec['evidence_id']}] |")
        if n > MAX_SHOWN:
            lines.append(f"\n... and {n - MAX_SHOWN} more - every matching record is in the Excel export.")
    if ans.findings:
        lines.append("")
        one = len(ans.findings) == 1
        lines.append(f"**Data quality - {len(ans.findings)} record{'' if one else 's'} "
                     f"{'contradicts itself' if one else 'contradict themselves'} or the calendar** (reported, not corrected):")
        for f in ans.findings[:8]:
            lines.append(f"- {f['record_key']}: {f['detail']}" + (f" [{f['evidence_id']}]" if f.get("evidence_id") else ""))
        if len(ans.findings) > 8:
            lines.append(f"- ... {len(ans.findings) - 8} more in the Conflicts sheet of the Excel export.")
    for note in ans.notes:
        lines.append(f"\n_{note}._")
    return "\n".join(lines)
