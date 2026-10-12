"""
Phase F - local industrial knowledge taxonomy.

Loads config/industrial/taxonomy.json and turns it into something the rest of
the workbench can use:

* abbreviation expansion            DCS -> Distributed Control System
* synonym normalisation             "cat cracker" -> "fluid catalytic cracking"
* query expansion for retrieval     "FCC riser" -> also search "fluid catalytic
                                    cracking", "cat cracker", "regenerator"...
* entity/term detection in text     which units, equipment and disciplines a
                                    chunk is about
* equipment tag pattern matching    P-101A, E-2301, PSV-1204, 6"-PG-1201-A1A

Pure local data, no model call, no network. Everything here is deterministic
so the same document always tags the same way.
"""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from core.logger import get_logger
from industrial import TAXONOMY_PATH

logger = get_logger("industrial.taxonomy")

_LOCK = threading.Lock()
_INSTANCE: Optional["Taxonomy"] = None


class Taxonomy:
    def __init__(self, data: dict):
        self.data = data
        self.version = data.get("version", "0")
        self.categories: Dict[str, str] = data.get("categories", {})
        self.abbreviations: Dict[str, str] = data.get("abbreviations", {})
        self.terms: Dict[str, dict] = data.get("terms", {})
        self.claim_types: Dict[str, str] = data.get("claim_types", {})
        self.confidentiality_levels: List[str] = data.get("confidentiality_levels", [])

        # alias -> canonical term
        self._alias_index: Dict[str, str] = {}
        for canonical, spec in self.terms.items():
            self._alias_index[canonical.lower()] = canonical
            for alias in spec.get("aliases", []):
                self._alias_index[alias.lower()] = canonical
        # abbreviations also resolve to a canonical term when the expansion
        # matches one (DCS -> "Distributed Control System" -> "dcs" term)
        for abbr, expansion in self.abbreviations.items():
            self._alias_index.setdefault(abbr.lower(), self._alias_index.get(
                expansion.lower(), abbr))

        self._compiled_tags = []
        for pattern in data.get("equipment_tag_patterns", {}).get("patterns", []):
            try:
                self._compiled_tags.append({
                    "name": pattern["name"],
                    "equipment_type": pattern.get("equipment_type", ""),
                    "regex": pattern["regex"],
                    "compiled": re.compile(pattern["regex"]),
                })
            except re.error as e:
                logger.warning(f"bad tag pattern '{pattern.get('name')}': {e}")

        # one big alternation for fast term detection, longest alias first so
        # "crude distillation unit" wins over "unit"
        aliases = sorted(self._alias_index.keys(), key=len, reverse=True)
        escaped = [re.escape(a) for a in aliases if len(a) > 2]
        self._term_regex = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(escaped) + r")(?![A-Za-z0-9])",
                                      re.IGNORECASE) if escaped else None
        self._abbr_regex = re.compile(
            r"\b(" + "|".join(re.escape(a) for a in sorted(self.abbreviations, key=len, reverse=True))
            + r")\b") if self.abbreviations else None

    # ------------------------------------------------------------- lookups --

    def expand_abbreviation(self, token: str) -> Optional[str]:
        if token in self.abbreviations:
            return self.abbreviations[token]
        upper = token.upper()
        return self.abbreviations.get(upper)

    def canonical(self, phrase: str) -> Optional[str]:
        """Map a phrase or alias to its canonical taxonomy term."""
        if not phrase:
            return None
        key = phrase.strip().lower()
        if key in self._alias_index:
            return self._alias_index[key]
        expansion = self.expand_abbreviation(phrase.strip())
        if expansion:
            return self._alias_index.get(expansion.lower())
        return None

    def describe(self, term: str) -> Optional[dict]:
        canonical = self.canonical(term) or term
        spec = self.terms.get(canonical)
        if not spec:
            return None
        return {"term": canonical, **spec}

    def category_of(self, term: str) -> Optional[str]:
        spec = self.describe(term)
        return spec.get("category") if spec else None

    def terms_in_category(self, category: str) -> List[str]:
        return sorted(t for t, s in self.terms.items() if s.get("category") == category)

    def tag_patterns(self) -> List[dict]:
        return self._compiled_tags

    # ------------------------------------------------------------ detection --

    def detect_terms(self, text: str, limit: int = 40) -> List[dict]:
        """Which taxonomy terms appear in this text, canonicalised and
        de-duplicated, with the surface form that matched."""
        if not text or self._term_regex is None:
            return []
        seen: Dict[str, dict] = {}
        for m in self._term_regex.finditer(text):
            surface = m.group(1)
            canonical = self._alias_index.get(surface.lower())
            if not canonical or canonical in seen:
                continue
            spec = self.terms.get(canonical, {})
            seen[canonical] = {"term": canonical, "matched": surface,
                               "category": spec.get("category", "unknown")}
            if len(seen) >= limit:
                break
        return list(seen.values())

    def detect_abbreviations(self, text: str, limit: int = 30) -> List[dict]:
        if not text or self._abbr_regex is None:
            return []
        out: Dict[str, dict] = {}
        for m in self._abbr_regex.finditer(text):
            abbr = m.group(1)
            if abbr in out:
                continue
            out[abbr] = {"abbreviation": abbr, "expansion": self.abbreviations[abbr]}
            if len(out) >= limit:
                break
        return list(out.values())

    def detect_equipment_tags(self, text: str, limit: int = 30) -> List[dict]:
        found: Dict[str, dict] = {}
        for pattern in self._compiled_tags:
            for m in pattern["compiled"].finditer(text or ""):
                tag = m.group(0)
                if tag in found:
                    continue
                found[tag] = {"tag": tag, "equipment_type": pattern["equipment_type"],
                              "pattern": pattern["name"]}
                if len(found) >= limit:
                    return list(found.values())
        return list(found.values())

    # ------------------------------------------------------------ expansion --

    def expand_query(self, query: str, max_terms: int = 8) -> dict:
        """Build retrieval variants for a query: the original, abbreviation
        expansions, canonical terms and their aliases/related terms.

        Returned as a dict so the caller can decide how to use each part -
        the knowledge base embeds the enriched string and uses the term list
        for metadata boosting."""
        original = (query or "").strip()
        expansions: List[str] = []
        related: List[str] = []
        matched_terms: List[str] = []

        for abbr in self.detect_abbreviations(original):
            expansions.append(abbr["expansion"])

        for hit in self.detect_terms(original):
            canonical = hit["term"]
            matched_terms.append(canonical)
            spec = self.terms.get(canonical, {})
            if canonical.lower() != hit["matched"].lower():
                expansions.append(canonical)
            for alias in spec.get("aliases", [])[:3]:
                if alias.lower() not in original.lower():
                    expansions.append(alias)
            for rel in spec.get("related", [])[:3]:
                related.append(rel)

        # de-duplicate, preserve order, cap size
        def dedupe(items: Iterable[str]) -> List[str]:
            seen, out = set(), []
            for i in items:
                k = i.lower()
                if k and k not in seen and k not in original.lower():
                    seen.add(k)
                    out.append(i)
            return out[:max_terms]

        expansions = dedupe(expansions)
        related = dedupe(related)
        enriched = original
        if expansions:
            enriched = f"{original} ({'; '.join(expansions)})"
        return {"original": original, "enriched": enriched, "expansions": expansions,
                "related": related, "terms": matched_terms}

    def annotate(self, text: str) -> dict:
        """Everything the taxonomy can say about a piece of text - used to tag
        chunks at ingestion time and to enrich evidence at answer time."""
        return {"terms": self.detect_terms(text), "abbreviations": self.detect_abbreviations(text),
                "equipment_tags": self.detect_equipment_tags(text)}

    def stats(self) -> dict:
        return {"version": self.version, "terms": len(self.terms),
                "aliases": len(self._alias_index), "abbreviations": len(self.abbreviations),
                "categories": len(self.categories), "tag_patterns": len(self._compiled_tags)}


def load_taxonomy(path: Optional[Path] = None) -> Taxonomy:
    path = Path(path or TAXONOMY_PATH)
    if not path.exists():
        logger.warning(f"taxonomy file not found at {path} - using an empty taxonomy")
        return Taxonomy({"version": "0", "terms": {}, "abbreviations": {}})
    try:
        return Taxonomy(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        logger.error(f"could not load taxonomy: {e}")
        return Taxonomy({"version": "0", "terms": {}, "abbreviations": {}})


def get_taxonomy() -> Taxonomy:
    """Process-wide singleton (the JSON is parsed once)."""
    global _INSTANCE
    if _INSTANCE is None:
        with _LOCK:
            if _INSTANCE is None:
                _INSTANCE = load_taxonomy()
    return _INSTANCE


def reload_taxonomy() -> Taxonomy:
    global _INSTANCE
    with _LOCK:
        _INSTANCE = load_taxonomy()
    return _INSTANCE


@lru_cache(maxsize=512)
def expand(term: str) -> str:
    """Convenience: 'DCS' -> 'Distributed Control System' (or the input back)."""
    tax = get_taxonomy()
    return tax.expand_abbreviation(term) or term
