"""
Entities: what the company's documents are ABOUT, with canonical ids.

Extraction is deterministic (patterns + a local gazetteer + the industrial
taxonomy's tag patterns); resolution is conservative:

  * "Unit-5", "U5", "Unit 5", "unit #5" are the SAME unit - identical number,
    same site - so they share one canonical id (UNIT:5, or UNIT:5@site).
  * "Hydrogen Unit 5" is linked to UNIT:5 as a descriptive alias only because it
    contains "Unit 5"; a DIFFERENT descriptor for the same number at the same site
    ("Crude Unit 5") marks the entity AMBIGUOUS for a human to settle.
  * A name-to-name equivalence ("the Hydrogen Generation Unit (HGU)") is recorded
    only when a document states it - with that sentence as evidence.
  * Similar spelling is never a merge: fuzzy matches are returned as SUGGESTIONS.
  * Human corrections (merge / alias / split / rename / retype) are logged and win.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from knowledge_fabric import store

TYPES = ("COMPANY", "SUBSIDIARY", "DEPARTMENT", "PLANT", "REFINERY", "UNIT", "EQUIPMENT",
         "PROJECT", "PERSON_ROLE", "VENDOR", "CUSTOMER", "PRODUCT", "PROCESS", "INCIDENT",
         "MAINTENANCE", "INSPECTION", "REPORT", "POLICY", "SOP", "FINANCIAL_METRIC", "KPI",
         "YEAR", "LOCATION",
         # a stated cause ("failed due to bearing wear") - not a named thing, but
         # what repeated-failure and root-cause questions are about
         "CAUSE")

# Things a figure can be ABOUT (a row in "maintenance cost by unit"). Metrics,
# processes, report types and causes describe a question; they do not narrow
# which rows of a table belong to it.
SUBJECT_TYPES = ("COMPANY", "SUBSIDIARY", "DEPARTMENT", "PLANT", "REFINERY", "UNIT", "EQUIPMENT",
                 "PROJECT", "VENDOR", "CUSTOMER", "PRODUCT", "LOCATION")

GAZETTEER_PATH =Path(__file__).resolve().parent.parent / "config" / "knowledge_fabric" / "entities.json"


def norm(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[‐-―]", "-", text)
    text = re.sub(r"[^a-z0-9#&\-\. ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48]


@dataclass
class Mention:
    entity_id: str
    type: str
    canonical: str
    surface: str
    start: int
    end: int
    confidence: float = 1.0
    source: str = "pattern"
    descriptor: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------- gazetteer ----

_gazetteer_cache: Optional[Dict[str, Any]] = None


def gazetteer() -> Dict[str, Any]:
    global _gazetteer_cache
    if _gazetteer_cache is None:
        try:
            _gazetteer_cache = json.loads(GAZETTEER_PATH.read_text(encoding="utf-8"))
        except Exception:
            _gazetteer_cache = {}
    return _gazetteer_cache


def reload_gazetteer() -> None:
    global _gazetteer_cache
    _gazetteer_cache = None


def _gazetteer_patterns() -> List[Tuple[re.Pattern, str, str, str]]:
    """(compiled pattern, type, canonical name, alias) for every gazetteer term."""
    out = []
    for type_name, entries in gazetteer().items():
        if type_name.startswith("_") or type_name not in TYPES:
            continue
        for entry in entries:
            if isinstance(entry, str):
                entry = {"name": entry}
            canonical = entry["name"]
            for alias in [canonical] + list(entry.get("aliases", [])):
                if len(alias) < 2:
                    continue
                flags = 0 if (alias.isupper() and len(alias) <= 5) else re.IGNORECASE
                pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", flags)
                out.append((pattern, type_name, canonical, alias))
    return out


# ------------------------------------------------------------- patterns ----

_UNIT = re.compile(r"(?<![A-Za-z0-9])(unit|u)[ \-]?#?[ \-]?(\d{1,3}[A-Z]?)(?![A-Za-z0-9])", re.IGNORECASE)
# A descriptor is a proper name right before a capitalised "Unit": "Hydrogen
# Unit 5". Case-sensitive on purpose - "work on unit 5" has no descriptor.
_DESCRIPTOR = re.compile(r"((?:[A-Z][a-z]+[ \-]){1,2})$")
_NOT_DESCRIPTORS = {"the", "this", "that", "each", "every", "one", "per", "at", "on", "in", "for",
                    "of", "from", "to", "and", "with", "by", "our", "their", "its", "new", "old",
                    "main", "same", "all", "both", "any", "no", "which", "when", "where", "during"}
# A project name stays on one line ("Unicorn=NO\nThis project record" names no project).
_PROJECT = re.compile(r"\b[Pp]roject[ \t]+([A-Z][\w\-]*(?:[ \t]+[A-Z][\w\-]*){0,3})|"
                      r"\b([A-Z][\w\-]*(?:[ \t]+[A-Z][\w\-]*){0,3})[ \t]+[Pp]roject\b")
_NOT_A_PROJECT_NAME = {"yes", "no", "na", "n/a", "true", "false", "none", "nil"}
_SITE = re.compile(r"\b((?:[A-Z][a-z]+\s){1,3})(Refinery|Plant|Complex|Terminal|Depot|Works)\b")
_SITE_TYPE = {"Refinery": "REFINERY", "Plant": "PLANT", "Complex": "PLANT", "Terminal": "PLANT",
              "Depot": "PLANT", "Works": "PLANT"}
_SOP = re.compile(r"\bSOP[\s\-#]?([A-Z]{0,4}[\s\-]?\d{1,4}[A-Z]?)\b")
_POLICY = re.compile(r"\b((?:[A-Z][a-z]+\s){1,4}Policy)\b")
_INCIDENT = re.compile(r"\b(?:INC|IR|INCIDENT)[\s\-#]?(\d{2,4}(?:[\-/]\d{1,4})?)\b", re.IGNORECASE)
_ORG = re.compile(r"\b((?:[A-Z][A-Za-z&\.]+\s){0,4}[A-Z][A-Za-z&\.]+)\s+"
                  r"(Ltd\.?|Limited|Pvt\.?\s+Ltd\.?|Private Limited|Inc\.?|Corporation|Corp\.?|LLC|GmbH|PLC|Co\.)(?![A-Za-z])")
_VENDOR_CUE = re.compile(r"(supplied by|supplier|vendor|contractor|procured from|purchased from|OEM)\s*[:\-]?\s*$", re.IGNORECASE)
_CUSTOMER_CUE = re.compile(r"(customer|client|sold to|offtaker|buyer)\s*[:\-]?\s*$", re.IGNORECASE)
_MEASURE = re.compile(
    r"^(?:rs\.?|inr|usd|eur|₹|\$|%|per ?cent|cr|crores?|lakhs?|mn|million|bn|billion|kbbl|bbl|mmt|mt|tpa|"
    r"tonnes?|tons?|kl|kg|days?|hrs?|hours?|mins?|mw|kw|kwh|mwh|nm3|scm|bar|psi|°c|deg ?c|ppm|mm|m3|"
    r"no\.?|nos\.?|numbers?|units?|kbpd|bpd|mbpd|mmscfd|kg/cm2)(?:[\s/]+\S+){0,3}$", re.IGNORECASE)
_EQUIVALENCE = re.compile(
    r"(?P<a>[A-Z][\w\- ]{1,60}?)\s*\(\s*(?:also\s+)?(?:known as|called|a\.?k\.?a\.?|formerly|hereafter|i\.e\.)?\s*"
    r"(?P<b>[A-Z][\w\- ]{1,40}?)\s*\)")
# "Unit 5, also called the Hydrogen Generation Unit or U5, ..."
_EQUIVALENCE_PHRASE = re.compile(
    r"(?P<a>[A-Z][\w\- ]{1,60}?),?\s+(?:also\s+(?:called|known\s+as)|a\.?k\.?a\.?|formerly(?:\s+called)?)\s+"
    r"(?:the\s+)?(?P<b>[A-Z][\w\-]*(?:\s+[A-Z][\w\-]*){0,4})"
    r"(?:\s+or\s+(?:the\s+)?(?P<c>[A-Z][\w\-]*(?:\s+[A-Z][\w\-]*){0,3}))?")


class Extractor:
    """Finds entity mentions in text. One instance per ingestion run."""

    def __init__(self):
        self._gazetteer = _gazetteer_patterns()
        try:
            from industrial.taxonomy import get_taxonomy
            self._taxonomy = get_taxonomy()
        except Exception:
            self._taxonomy = None

    def mentions(self, text: str, location: Optional[str] = None) -> List[Mention]:
        if not text:
            return []
        found: List[Mention] = []
        site = slug(location) if location else ""

        for m in _UNIT.finditer(text):
            word, number = m.group(1), m.group(2).upper()
            if word.lower() == "u" and word != "U":
                continue                       # the short form is written "U5" / "U-5"
            descriptor = None
            start = m.start()
            if word == "Unit":
                d = _DESCRIPTOR.search(text[max(0, m.start() - 40):m.start()])
                if d:
                    words = [w for w in re.split(r"[ \-]", d.group(1).strip()) if w]
                    while words and words[0].lower() in _NOT_DESCRIPTORS:
                        words.pop(0)
                    if words and all(w.lower() not in _NOT_DESCRIPTORS for w in words):
                        descriptor = " ".join(words)
                        start = m.start() - len(d.group(1)) + d.group(1).find(words[0])
            key = f"UNIT:{number}" + (f"@{site}" if site else "")
            found.append(Mention(key, "UNIT", f"Unit {number}", text[start:m.end()].strip(), start, m.end(),
                                 0.95, "pattern", descriptor=descriptor))

        if self._taxonomy is not None:
            try:
                for tag in self._taxonomy.detect_equipment_tags(text, limit=60):
                    value = tag.get("tag") or tag.get("text") or ""
                    start = text.find(value)
                    if value and start >= 0:
                        canonical = value.upper().replace(" ", "")
                        found.append(Mention(f"EQUIPMENT:{canonical}", "EQUIPMENT", canonical, value,
                                             start, start + len(value), 0.95, "taxonomy"))
            except Exception:
                pass

        for m in _PROJECT.finditer(text):
            name = (m.group(1) or m.group(2) or "").strip()
            words = name.split()
            if m.group(2) and words and words[-1].lower() in _NOT_DESCRIPTORS | {"a", "an"}:
                continue                         # "This project", "Each Project" - a determiner, not a name
            if any(w.lower() in _NOT_A_PROJECT_NAME for w in words):
                continue
            while words and words[0].lower() in _NOT_DESCRIPTORS:
                words = words[1:]                # "The Coker Revamp Project" -> "Coker Revamp"
            name = " ".join(words)
            if name and name.lower() not in ("the", "this", "a", "new", "each", "our"):
                found.append(Mention(f"PROJECT:{slug(name)}", "PROJECT", f"Project {name}", m.group(0),
                                     m.start(), m.end(), 0.8))
        for m in _SITE.finditer(text):
            words = [w for w in m.group(1).split() if w.lower() not in _NOT_DESCRIPTORS]
            if words and words[0] not in ("The", "This", "Our", "Each"):
                name = " ".join(words + [m.group(2)])
                type_name = _SITE_TYPE[m.group(2)]
                found.append(Mention(f"{type_name}:{slug(name)}", type_name, name, m.group(0),
                                     m.start(), m.end(), 0.85))
        for m in _SOP.finditer(text):
            code = re.sub(r"[\s]+", "-", m.group(1).upper()).strip("-")
            found.append(Mention(f"SOP:{code}", "SOP", f"SOP-{code}", m.group(0), m.start(), m.end(), 0.95))
        for m in _POLICY.finditer(text):
            found.append(Mention(f"POLICY:{slug(m.group(1))}", "POLICY", m.group(1), m.group(0),
                                 m.start(), m.end(), 0.8))
        for m in _INCIDENT.finditer(text):
            found.append(Mention(f"INCIDENT:{m.group(1)}", "INCIDENT", f"Incident {m.group(1)}", m.group(0),
                                 m.start(), m.end(), 0.9))
        for m in _ORG.finditer(text):
            name = f"{m.group(1)} {m.group(2)}".strip()
            before = text[max(0, m.start() - 40):m.start()]
            type_name = ("VENDOR" if _VENDOR_CUE.search(before)
                         else "CUSTOMER" if _CUSTOMER_CUE.search(before) else "COMPANY")
            key = slug(re.sub(r"\b(ltd|limited|pvt|private|inc|corporation|corp|llc|gmbh|plc|co)\b\.?", "",
                              name.lower()))
            found.append(Mention(f"ORG:{key}", type_name, name, m.group(0), m.start(), m.end(), 0.85))

        for pattern, type_name, canonical, alias in self._gazetteer:
            for m in pattern.finditer(text):
                prefix = "ORG" if type_name in ("COMPANY", "SUBSIDIARY", "VENDOR", "CUSTOMER") else type_name
                found.append(Mention(f"{prefix}:{slug(canonical)}", type_name, canonical, m.group(0),
                                     m.start(), m.end(), 0.9, "gazetteer"))

        # Keep the most specific mention per span (a unit inside an org name etc.)
        found.sort(key=lambda x: (x.start, -(x.end - x.start), -x.confidence))
        kept: List[Mention] = []
        for mention in found:
            if any(k.start <= mention.start and mention.end <= k.end and k.entity_id != mention.entity_id
                   and (k.end - k.start) > (mention.end - mention.start) for k in kept):
                continue
            if any(k.entity_id == mention.entity_id and k.start == mention.start for k in kept):
                continue
            kept.append(mention)
        return kept

    def equivalences(self, text: str, location: Optional[str] = None) -> List[Tuple[Mention, str]]:
        """'Unit 5 (Hydrogen Unit)' -> [(mention of Unit 5, 'Hydrogen Unit')]: a
        document itself saying two names are one thing. Brackets that hold a UNIT
        OF MEASURE ('Maintenance cost (Rs Cr)') or follow a metric are not names."""
        out = []
        pairs = [(m.group("a"), m.group("b")) for m in _EQUIVALENCE.finditer(text or "")]
        for m in _EQUIVALENCE_PHRASE.finditer(text or ""):
            pairs += [(m.group("a"), name) for name in (m.group("b"), m.group("c")) if name]
        for left_text, right in pairs:
            right = right.strip()
            right_mentions = self.mentions(right, location)
            if _MEASURE.match(right) or (not re.search(r"[A-Za-z]{3,}", right) and not right_mentions):
                continue                           # a unit of measure, or a code that names nothing
            left = self.mentions(left_text, location)
            if left and left[-1].type in ("FINANCIAL_METRIC", "KPI", "YEAR"):
                continue
            if right_mentions and right_mentions[0].type in ("FINANCIAL_METRIC", "KPI"):
                continue
            if left and right_mentions and right_mentions[0].entity_id == left[-1].entity_id:
                continue
            if left and right_mentions and left[-1].source == "gazetteer" and right_mentions[0].source != "gazetteer":
                # "Hydrogen Generation Unit (U5)": the numbered unit is the identity,
                # the generic process name becomes its evidence-backed alias.
                out.append((right_mentions[0], left[-1].surface))
            elif left and len(right) >= 2:
                out.append((left[-1], right))
            elif right_mentions and left_text.strip():
                out.append((right_mentions[0], left_text.strip()))
        return out


# ------------------------------------------------------------ persistence ----

def upsert_mentions(conn, node_id: str, mentions: Iterable[Mention], evidence_node: str = "") -> List[str]:
    ids = []
    for mention in mentions:
        existing = conn.execute("SELECT attributes, status FROM kf_entities WHERE entity_id = ?",
                                (mention.entity_id,)).fetchone()
        attributes = store.loads(existing["attributes"], {}) if existing else {}
        if mention.descriptor:
            descriptors = attributes.setdefault("descriptors", {})
            descriptors[mention.descriptor] = descriptors.get(mention.descriptor, 0) + 1
        status = existing["status"] if existing else "active"
        if status == "active" and len(attributes.get("descriptors", {})) > 1 and not attributes.get("resolved_by_human"):
            status = "ambiguous"
        if existing:
            conn.execute("UPDATE kf_entities SET attributes = ?, status = ? WHERE entity_id = ?",
                         (store.dumps(attributes), status, mention.entity_id))
        else:
            conn.execute("INSERT INTO kf_entities(entity_id, type, canonical_name, attributes, status, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (mention.entity_id, mention.type, mention.canonical, store.dumps(attributes),
                          status, store.now()))
        for alias, confidence, source in ((mention.surface, mention.confidence, mention.source),
                                          (mention.canonical, 1.0, "canonical")):
            conn.execute("INSERT OR IGNORE INTO kf_aliases(alias_norm, alias, entity_id, confidence, source, evidence_node) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (norm(alias), alias, mention.entity_id, confidence, source, evidence_node or node_id))
        if mention.descriptor:
            descriptive = f"{mention.descriptor} {mention.canonical}"
            conn.execute("INSERT OR IGNORE INTO kf_aliases(alias_norm, alias, entity_id, confidence, source, evidence_node) "
                         "VALUES (?, ?, ?, 0.8, 'descriptor', ?)",
                         (norm(descriptive), descriptive, mention.entity_id, node_id))
        conn.execute("INSERT OR REPLACE INTO kf_mentions(node_id, entity_id, confidence, surface) VALUES (?, ?, ?, ?)",
                     (node_id, mention.entity_id, mention.confidence, mention.surface[:120]))
        ids.append(mention.entity_id)
    return ids


def record_equivalence(conn, mention: Mention, other_name: str, node_id: str) -> None:
    """A document said `other_name` IS this entity: an evidence-backed alias."""
    conn.execute("INSERT OR IGNORE INTO kf_entities(entity_id, type, canonical_name, attributes, status, created_at) "
                 "VALUES (?, ?, ?, '{}', 'active', ?)", (mention.entity_id, mention.type, mention.canonical, store.now()))
    conn.execute("INSERT OR REPLACE INTO kf_aliases(alias_norm, alias, entity_id, confidence, source, evidence_node) "
                 "VALUES (?, ?, ?, 0.9, 'evidence', ?)", (norm(other_name), other_name, mention.entity_id, node_id))


def resolve(conn, surface: str, type_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """Entities a phrase refers to, best first. Exact alias matches only; if the
    phrase contains a unit number, the unit pattern is applied first."""
    results: Dict[str, Dict[str, Any]] = {}
    key = norm(surface)
    for row in conn.execute("SELECT a.entity_id, a.confidence, a.source, e.type, e.canonical_name, e.status "
                            "FROM kf_aliases a JOIN kf_entities e ON e.entity_id = a.entity_id "
                            "WHERE a.alias_norm = ? AND e.status != 'merged'", (key,)):
        if type_hint and row["type"] != type_hint:
            continue
        current = results.get(row["entity_id"])
        if not current or row["confidence"] > current["confidence"]:
            results[row["entity_id"]] = dict(row)
    if not results:
        for mention in Extractor().mentions(surface):
            if type_hint and mention.type != type_hint:
                continue
            for row in conn.execute("SELECT entity_id, type, canonical_name, status FROM kf_entities "
                                    "WHERE (entity_id = ? OR entity_id LIKE ?) AND status != 'merged'",
                                    (mention.entity_id, mention.entity_id + "@%")):
                results[row["entity_id"]] = {**dict(row), "confidence": mention.confidence, "source": "pattern"}
    return sorted(results.values(), key=lambda r: -r["confidence"])


def suggestions(conn, surface: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Similar names - offered to a human, never merged automatically."""
    key = norm(surface)
    candidates = [dict(r) for r in conn.execute(
        "SELECT DISTINCT a.alias, a.entity_id, e.canonical_name, e.type FROM kf_aliases a "
        "JOIN kf_entities e ON e.entity_id = a.entity_id WHERE e.status != 'merged'")]
    scored = []
    for row in candidates:
        ratio = difflib.SequenceMatcher(None, key, norm(row["alias"])).ratio()
        if ratio >= 0.72 and norm(row["alias"]) != key:
            scored.append({**row, "similarity": round(ratio, 3), "merge": "suggestion only"})
    scored.sort(key=lambda r: -r["similarity"])
    seen, out = set(), []
    for row in scored:
        if row["entity_id"] not in seen:
            seen.add(row["entity_id"])
            out.append(row)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------- human corrections ----

def correct(action: str, payload: Dict[str, Any], username: str = "") -> Dict[str, Any]:
    """merge {keep, remove} | alias {entity_id, alias} | unalias {entity_id, alias}
    | rename {entity_id, name} | retype {entity_id, type} | resolve {entity_id}"""
    with store.connection() as conn:
        if action == "merge":
            keep, remove = payload["keep"], payload["remove"]
            if keep == remove:
                raise ValueError("cannot merge an entity with itself")
            for table, column in (("kf_mentions", "entity_id"), ("kf_facts", "entity_id"),
                                  ("kf_relations", "src"), ("kf_relations", "dst")):
                conn.execute(f"UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?", (keep, remove))
            conn.execute("UPDATE OR IGNORE kf_aliases SET entity_id = ?, source = 'human' WHERE entity_id = ?",
                         (keep, remove))
            conn.execute("UPDATE kf_entities SET status = 'merged', attributes = json_set(COALESCE(attributes,'{}'), "
                         "'$.merged_into', ?) WHERE entity_id = ?", (keep, remove))
        elif action == "alias":
            conn.execute("INSERT OR REPLACE INTO kf_aliases(alias_norm, alias, entity_id, confidence, source, evidence_node) "
                         "VALUES (?, ?, ?, 1.0, 'human', NULL)", (norm(payload["alias"]), payload["alias"], payload["entity_id"]))
        elif action == "unalias":
            conn.execute("DELETE FROM kf_aliases WHERE alias_norm = ? AND entity_id = ?",
                         (norm(payload["alias"]), payload["entity_id"]))
        elif action == "rename":
            conn.execute("UPDATE kf_entities SET canonical_name = ? WHERE entity_id = ?",
                         (payload["name"], payload["entity_id"]))
        elif action == "retype":
            if payload["type"] not in TYPES:
                raise ValueError(f"unknown entity type {payload['type']}")
            conn.execute("UPDATE kf_entities SET type = ? WHERE entity_id = ?", (payload["type"], payload["entity_id"]))
        elif action == "resolve":
            row = conn.execute("SELECT attributes FROM kf_entities WHERE entity_id = ?", (payload["entity_id"],)).fetchone()
            attributes = store.loads(row["attributes"], {}) if row else {}
            attributes["resolved_by_human"] = username or True
            conn.execute("UPDATE kf_entities SET status = 'active', attributes = ? WHERE entity_id = ?",
                         (store.dumps(attributes), payload["entity_id"]))
        else:
            raise ValueError(f"unknown correction '{action}'")
        conn.execute("INSERT INTO kf_corrections(action, payload, username, at) VALUES (?, ?, ?, ?)",
                     (action, store.dumps(payload), username, store.now()))
    return {"ok": True, "action": action, **payload}


def ambiguous_entities(conn) -> List[Dict[str, Any]]:
    return [{**dict(r), "attributes": store.loads(r["attributes"], {})}
            for r in conn.execute("SELECT * FROM kf_entities WHERE status = 'ambiguous'")]
