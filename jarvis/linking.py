"""
Activity entity resolution: which schedule activity does a field note mean?

"Pile 17 concreting completed" and "P-017 Foundation Pile Concrete Pour" are the
same thing; "P17 excavation complete" is NOT the P17 concreting activity. Each
candidate activity is scored from independent signals, each recorded as a
reason the operator can read:

    code        the note names the activity code exactly
    alias       a mapping confirmed earlier (or supplied by the schedule file)
    number      element number agrees (P17 / pier 17 / pile no. 17 / P-017)
    element     the element word agrees (pier, pile, abutment, span ...)
    work        the work type agrees (concreting, excavation, PCC ...)
    name        textual similarity of the note and the activity name
    dates       the note's date is plausible for the activity's window
    party       contractor or location agree
    model       the local extraction model named this code
    semantic    embedding similarity (bge-m3), when enabled and installed

Contradictions subtract (a different pier number, a different kind of work).
A link is applied automatically only when the best score is high AND clearly
ahead of the next one; otherwise it waits for an authorised person to confirm
or correct it - and that decision is remembered as an alias.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import re
import sqlite3
from typing import Any, Dict, List, Optional

from jarvis import dates, store
from jarvis.extraction import Observation, element_refs, _work_types

AUTO_LINK = 0.70
MARGIN = 0.12
SUGGEST = 0.40

_LETTER_ELEMENTS = {"pier": {"pier", "pile", "p"}, "abutment": {"abutment", "a"}, "span": {"span", "s"}}
_ELEMENT_WORDS = ("pier", "pile", "abutment", "span", "girder", "culvert", "column", "footing", "well", "deck", "cap",
                  "drain", "wall", "foundation")


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1}


def activity_profile(activity: Dict[str, Any]) -> Dict[str, Any]:
    text = f"{activity.get('code', '')} {activity.get('name', '')} {activity.get('wbs_code', '') or ''}"
    refs = element_refs(text)
    words = {w for w in _ELEMENT_WORDS if re.search(rf"\b{w}s?\b", text.lower())}
    work = set(_work_types(f"{activity.get('name', '')} {activity.get('work_category') or ''}"))
    return {"refs": refs, "numbers": {r["number"] for r in refs}, "element_words": words, "work": work,
            "code_norm": norm(activity.get("code", "")), "tokens": _tokens(activity.get("name", ""))}


def alias_keys(obs: Observation) -> List[str]:
    keys = [norm(r) for r in obs.refs if norm(r)]
    structural = sorted(f"{r['element']}{r['number']}" for r in obs.element_refs)
    if structural:
        keys.append("+".join(structural + sorted(obs.work_types)))
    return list(dict.fromkeys(keys))


def candidates_for(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    from jarvis.forecast import current_version
    version = current_version(conn, project_id)
    if version is None:
        rows = conn.execute("SELECT * FROM jv_activities WHERE project_id = ?", (project_id,)).fetchall()
        return [dict(r) for r in rows]
    rows = conn.execute("SELECT a.*, v.planned_start, v.planned_finish, v.actual_start, v.actual_finish "
                        "FROM jv_activities a JOIN jv_activity_versions v ON v.activity_id = a.activity_id "
                        "WHERE v.version_id = ?", (version["version_id"],)).fetchall()
    return [dict(r) for r in rows]


def score(obs: Observation, activity: Dict[str, Any], aliases: Dict[str, List[Dict]], reference: _dt.date,
          semantic: Optional[float] = None) -> Dict[str, Any]:
    prof = activity_profile(activity)
    reasons: List[str] = []
    total = 0.0

    # code / alias
    obs_norms = {norm(r) for r in obs.refs}
    if prof["code_norm"] and prof["code_norm"] in obs_norms:
        total += 0.6
        reasons.append(f"code {activity['code']} named in the note (+0.60)")
    alias_hit = False
    for key in alias_keys(obs):
        for mapping in aliases.get(key, []):
            if mapping["activity_id"] == activity["activity_id"]:
                weight = 0.55 if mapping["source"] == "confirmed" else 0.4
                total += weight
                reasons.append(f"{mapping['source']} alias '{mapping['alias']}' (+{weight:.2f})")
                alias_hit = True
                break
        if alias_hit:
            break
    if not alias_hit:
        # a field name the schedule (or an earlier confirmation) gave this activity,
        # written inside the note: "side drains", "P17 dhalai"
        haystack = f"{norm(obs.text)}|{norm(obs.reading)}"
        for mapping in aliases.get("__by_activity__", {}).get(activity["activity_id"], []):
            key = mapping["alias_norm"]
            if len(key) >= 5 and key != prof["code_norm"] and key in haystack:
                weight = 0.5 if mapping["source"] == "confirmed" else 0.4
                total += weight
                reasons.append(f"field name '{mapping['alias']}' appears in the note (+{weight:.2f})")
                break

    # element number and word
    obs_numbers = {r["number"] for r in obs.element_refs} | {r["number_to"] for r in obs.element_refs if r["number_to"]}
    if obs_numbers and prof["numbers"]:
        if obs_numbers & prof["numbers"]:
            total += 0.40
            reasons.append(f"element number {sorted(obs_numbers & prof['numbers'])[0]} agrees (+0.40)")
        else:
            total -= 0.40
            reasons.append(f"element number differs ({sorted(obs_numbers)} vs {sorted(prof['numbers'])}) (-0.40)")
    obs_words = {r["element"] for r in obs.element_refs if r.get("explicit_word")}
    if obs_words:
        activity_words = set(prof["element_words"])
        for r in prof["refs"]:
            activity_words |= _LETTER_ELEMENTS.get(r["element"], {r["element"]}) if not r.get("explicit_word") \
                else {r["element"]}
        if obs_words & activity_words:
            total += 0.15
            reasons.append(f"element '{sorted(obs_words & activity_words)[0]}' agrees (+0.15)")
        elif prof["element_words"]:
            total -= 0.15
            reasons.append(f"element differs ({', '.join(sorted(obs_words))} vs "
                           f"{', '.join(sorted(prof['element_words']))}) (-0.15)")

    # work type
    obs_work = set(obs.work_types)
    if obs_work and prof["work"]:
        shared = obs_work & prof["work"]
        if shared:
            total += 0.20
            reasons.append(f"work type '{sorted(shared)[0]}' agrees (+0.20)")
        else:
            total -= 0.12
            reasons.append(f"work type differs ({', '.join(sorted(obs_work))} vs "
                           f"{', '.join(sorted(prof['work']))}) (-0.12)")

    # textual similarity of the reading and the name
    ratio = difflib.SequenceMatcher(None, obs.reading.lower()[:200], (activity.get("name") or "").lower()).ratio()
    overlap = len(_tokens(obs.reading) & prof["tokens"]) / max(1, len(prof["tokens"]))
    name_score = round(min(0.15, 0.08 * ratio + 0.12 * overlap), 3)
    if name_score >= 0.02:
        total += name_score
        reasons.append(f"name similarity (+{name_score:.2f})")

    # dates
    day = dates.from_iso(obs.occurred_on) or reference
    start, finish = dates.from_iso(activity.get("planned_start")), dates.from_iso(activity.get("planned_finish"))
    if start and finish:
        if start - _dt.timedelta(days=10) <= day <= finish + _dt.timedelta(days=30):
            total += 0.05
            reasons.append("date fits the activity's window (+0.05)")
        elif day < start - _dt.timedelta(days=45) or day > finish + _dt.timedelta(days=120):
            total -= 0.10
            reasons.append("date far outside the activity's window (-0.10)")

    # contractor / location / model / semantic
    if obs.contractor and activity.get("contractor") and obs.contractor.lower() in activity["contractor"].lower():
        total += 0.05
        reasons.append("contractor agrees (+0.05)")
    if obs.location and activity.get("location") and norm(obs.location) in norm(activity["location"]):
        total += 0.05
        reasons.append("location agrees (+0.05)")
    if obs.model_guess and obs.model_guess == activity.get("code"):
        total += 0.15
        reasons.append("the extraction model named this activity (+0.15)")
    if semantic is not None and semantic > 0.5:
        bonus = round(min(0.15, (semantic - 0.5) * 0.3), 3)
        total += bonus
        reasons.append(f"semantic similarity {semantic:.2f} (+{bonus:.2f})")
    return {"activity_id": activity["activity_id"], "code": activity.get("code"), "name": activity.get("name"),
            "score": round(max(0.0, min(1.0, total)), 3), "reasons": reasons}


def load_aliases(conn: sqlite3.Connection, project_id: str) -> Dict[str, List[Dict]]:
    out: Dict[str, Any] = {"__by_activity__": {}}
    for row in conn.execute("SELECT * FROM jv_entity_mappings WHERE project_id = ?", (project_id,)):
        out.setdefault(row["alias_norm"], []).append(dict(row))
        out["__by_activity__"].setdefault(row["activity_id"], []).append(dict(row))
    return out


def resolve(conn: sqlite3.Connection, project_id: str, obs: Observation, reference: _dt.date,
            use_embeddings: bool = False) -> Dict[str, Any]:
    acts = candidates_for(conn, project_id)
    if not acts:
        return {"status": "unlinked", "activity_id": None, "confidence": 0.0, "candidates": [],
                "reason": "the project has no schedule activities yet"}
    aliases = load_aliases(conn, project_id)
    semantic = _semantic(obs, acts) if use_embeddings else {}
    scored = sorted((score(obs, a, aliases, reference, semantic.get(a["activity_id"])) for a in acts),
                    key=lambda s: -s["score"])
    top = scored[0]
    second = scored[1]["score"] if len(scored) > 1 else 0.0
    margin = round(top["score"] - second, 3)
    if top["score"] >= AUTO_LINK and margin >= MARGIN:
        status, reason = "linked", f"score {top['score']:.2f}, {margin:.2f} ahead of the next candidate"
    elif top["score"] >= SUGGEST:
        status = "needs_confirmation"
        reason = (f"best score {top['score']:.2f} is below {AUTO_LINK}" if top["score"] < AUTO_LINK
                  else f"two candidates are too close ({top['score']:.2f} vs {second:.2f})")
    else:
        status, reason = "unlinked", f"no activity scored {SUGGEST} or more (best {top['score']:.2f})"
    return {"status": status, "activity_id": top["activity_id"] if status == "linked" else None,
            "confidence": top["score"], "margin": margin, "reason": reason,
            "candidates": [s for s in scored[:5] if s["score"] > 0]}


def remember(conn: sqlite3.Connection, project_id: str, obs: Observation, activity_id: str, source: str = "confirmed",
             confidence: float = 1.0) -> List[str]:
    """Store the note's references as aliases of the activity (a learned mapping)."""
    added = []
    for key in alias_keys(obs):
        cur = conn.execute("INSERT OR IGNORE INTO jv_entity_mappings (mapping_id, project_id, alias_norm, alias, "
                           "activity_id, confidence, source, confirmed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (store.new_id("MAP"), project_id, key, ", ".join(obs.refs) or key, activity_id, confidence,
                            source, store.current_user(), store.now()))
        if cur.rowcount:
            added.append(key)
    return added


def _semantic(obs: Observation, acts: List[Dict[str, Any]]) -> Dict[str, float]:
    """Cosine similarity with bge-m3 embeddings - only if the model answers."""
    try:
        import numpy as np
        from core.capabilities import embedding_provider
        query = embedding_provider.embed(obs.reading[:500], timeout=30)
        if not query:
            return {}
        q = np.asarray(query, dtype=float)
        out = {}
        for a in acts[:200]:
            vector = embedding_provider.embed(f"{a.get('code')} {a.get('name')}", timeout=30)
            if vector:
                v = np.asarray(vector, dtype=float)
                out[a["activity_id"]] = float(q @ v / (np.linalg.norm(q) * np.linalg.norm(v) + 1e-9))
        return out
    except Exception:
        return {}
