"""
Build Word/Excel from the installed ZEUS knowledge pack with REAL data.

When the user asks for company history, deals, P&L, etc., we do not invent
filler paragraphs — we load the markdown / original xlsx from
knowledge/JARVIS_PACK and format them into professional deliverables.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger("knowledge_docs")

PACK = Path(__file__).resolve().parent.parent / "knowledge" / "JARVIS_PACK"
MD_DIR = PACK / "markdown"
ORIG_DIR = PACK / "originals"


def pack_available() -> bool:
    return MD_DIR.is_dir() and any(MD_DIR.glob("*.md"))


# The pack is used ONLY when the request names it. Substring keywords such as
# "pl" (for P&L), "qa", "model" or "task" used to match unrelated topics -
# "planets" returned Nexus data and "test budget" returned the ZEUS budget
# tracker - which is a wrong answer delivered confidently.
_PACK_ANCHORS = re.compile(
    r"\b(nexus|precision engineering|nexus precision|jarvis|sih|knowledge pack|"
    r"company history|budget tracker|kpi dashboard|task tracker|project task tracker|"
    r"inventory register|contacts directory|qa (?:test )?checklist|model inventory|"
    r"product roadmap|live demo script|multi[- ]model architecture|industrial workbench deck)\b",
    re.IGNORECASE)


def pack_relevant(topic: str) -> bool:
    return bool(_PACK_ANCHORS.search(topic or ""))


def _docs_dir() -> Path:
    from core.paths import documents_root
    d = documents_root()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    return path.with_name(f"{path.stem}_{int(time.time())}{path.suffix}")


def _topic_key(topic: str) -> str:
    t = (topic or "").lower()
    t = re.sub(r"\(grounded\)", "", t)
    t = re.sub(r"^(document about|write|create|make|report on|essay on)\s+", "", t)
    return t.strip()


def match_knowledge_files(topic: str) -> List[Path]:
    """Pick the most relevant markdown files for this topic."""
    if not MD_DIR.is_dir():
        return []
    t = _topic_key(topic)
    scored: List[Tuple[int, Path]] = []
    keywords = {
        "nexus": 5, "precision": 4, "company": 2, "history": 4, "timeline": 3,
        "deal": 4, "revenue": 3, "p&l": 4, "pl": 2, "profit": 3, "fy": 2,
        "staff": 3, "employee": 3, "machinery": 3, "machine": 2, "lease": 3,
        "jarvis": 3, "task": 2, "model": 2, "budget": 3, "kpi": 3,
        "inventory": 3, "contact": 3, "qa": 2, "sih": 3, "roadmap": 2,
        "architecture": 2, "workbench": 2, "pitch": 2, "demo": 2,
    }
    for path in MD_DIR.glob("*.md"):
        if path.name.startswith("00_"):
            continue
        name = path.stem.lower().replace("_", " ")
        body = path.read_text(encoding="utf-8", errors="replace")[:4000].lower()
        score = 0
        for kw, w in keywords.items():
            if kw in t and (kw in name or kw in body):
                score += w
            elif kw in t and kw in name:
                score += w
        # strong match on nexus pack
        if "nexus" in t and "nexus" in name:
            score += 10
        if "history" in t and "history" in name:
            score += 8
        if "deal" in t and "deal" in body:
            score += 5
        if score:
            scored.append((score, path))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored:
        return [p for _, p in scored[:4]]
    # default: full company history if nexus-ish
    if any(k in t for k in ("nexus", "company", "history", "precision")):
        primary = MD_DIR / "Nexus_Precision_Company_History_FULL.md"
        if primary.exists():
            return [primary]
    return []


def _parse_md_sections(md: str) -> List[Tuple[str, str]]:
    parts = re.split(r"^##\s+(.+)$", md, flags=re.MULTILINE)
    if len(parts) < 3:
        return [("Content", md)]
    out = []
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        out.append((title, body))
    return out


def _md_table_to_rows(block: str) -> List[List[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.match(r"^:?-+:?$", c or "") for c in cells):
            continue  # separator
        rows.append(cells)
    return rows


def build_word_from_knowledge(topic: str, filename: str = "") -> Optional[Dict[str, Any]]:
    """Return office.finish dict or None if pack has nothing relevant."""
    if not pack_relevant(topic):
        return None
    files = match_knowledge_files(topic)
    if not files:
        return None

    from core.rich_docs import polish_word_document
    from core import office

    sections: List[Tuple[str, str]] = []
    callouts: List[str] = []

    # Title section from first file intro
    intro_bits = []
    for path in files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        # strip first H1
        raw = re.sub(r"^#\s+.*\n", "", raw, count=1)
        for title, body in _parse_md_sections(raw):
            # Convert markdown tables to readable prose + keep table text
            if "|" in body and "---" in body:
                rows = _md_table_to_rows(body)
                if not rows:
                    continue
                # Profile-style key-value (2 columns)
                if len(rows[0]) <= 2 or (len(rows) > 1 and len(rows[0]) == 2):
                    lines = []
                    for r in rows[:40]:
                        if len(r) >= 2 and r[0]:
                            lines.append(f"**{r[0]}:** {r[1]}")
                        elif r[0]:
                            lines.append(r[0])
                    prose = "\n".join(lines)
                else:
                    # multi-column table — render as readable lines
                    header = rows[0]
                    lines = []
                    for r in rows[1:25]:
                        pairs = []
                        for h, v in zip(header, r):
                            if v and h:
                                pairs.append(f"{h}={v}")
                        if pairs:
                            lines.append(" • ".join(pairs))
                    prose = "\n".join(lines) if lines else body[:2000]
                sections.append((title.replace("Sheet: ", ""), prose))
                if len(callouts) < 3:
                    callouts.append(title.replace("Sheet: ", "")[:40])
            else:
                clean = re.sub(r"\n{3,}", "\n\n", body).strip()
                if clean:
                    sections.append((title.replace("Sheet: ", ""), clean[:4000]))

    if not sections:
        return None

    # Clean topic for title
    title = _topic_key(topic).title()
    if "nexus" in title.lower() and "precision" not in title.lower():
        title = "Nexus Precision — Company Report"
    if len(title) < 5:
        title = "Nexus Precision Company History"

    safe = filename.strip() or re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:50]
    out_path = _unique(_docs_dir() / f"{safe}.docx")

    polish_word_document(
        title,
        sections,
        out_path,
        subtitle="Grounded in local ZEUS knowledge pack (offline)",
        callouts=callouts or ["Company data", "Local RAG", "Sample dataset"],
    )
    return office.finish(out_path, "docx", extra={
        "topic": title,
        "sources": [p.name for p in files],
        "quality": "knowledge_pack",
        "sections": len(sections),
    })


def build_excel_from_knowledge(topic: str, filename: str = "") -> Optional[Dict[str, Any]]:
    """Prefer copying the real original xlsx; else rebuild from markdown tables."""
    from core import office
    if not pack_relevant(topic):
        return None
    t = _topic_key(topic)

    # Direct original files
    candidates = []
    if ORIG_DIR.is_dir():
        for p in ORIG_DIR.rglob("*.xlsx"):
            name = p.stem.lower().replace("_", " ")
            score = 0
            if "nexus" in t and "nexus" in name:
                score += 10
            if "history" in t and "history" in name:
                score += 8
            if "budget" in t and "budget" in name:
                score += 8
            if "kpi" in t and "kpi" in name:
                score += 8
            if "inventory" in t and "inventory" in name:
                score += 8
            if "contact" in t and "contact" in name:
                score += 8
            if "task" in t and "task" in name:
                score += 8
            if "model" in t and "model" in name:
                score += 6
            if "qa" in t and "qa" in name:
                score += 6
            if score:
                candidates.append((score, p))
    candidates.sort(key=lambda x: x[0], reverse=True)

    if candidates:
        src = candidates[0][1]
        safe = filename.strip() or src.stem
        dest = _unique(_docs_dir() / f"{safe}.xlsx")
        shutil.copy2(src, dest)
        return office.finish(dest, "xlsx", extra={
            "topic": topic,
            "source": src.name,
            "quality": "knowledge_pack_original",
            "note": "Copied from installed ZEUS data pack (real tables)",
        })

    # Rebuild from markdown tables using rich_docs
    files = match_knowledge_files(topic)
    if not files:
        return None

    headers: List[str] = []
    rows: List[Dict[str, Any]] = []
    for path in files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        for title, body in _parse_md_sections(raw):
            table = _md_table_to_rows(body)
            if len(table) < 2:
                continue
            headers = table[0]
            for r in table[1:80]:
                d = {}
                for h, v in zip(headers, r):
                    d[h] = v
                rows.append(d)
            if rows:
                break
        if rows:
            break

    if not headers or not rows:
        return None

    from core.rich_docs import polish_spreadsheet
    title = _topic_key(topic).title() or "Knowledge Data"
    safe = filename.strip() or re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:50]
    out_path = _unique(_docs_dir() / f"{safe}.xlsx")
    polish_spreadsheet(title, headers, rows, out_path)
    return office.finish(out_path, "xlsx", extra={
        "topic": title,
        "rows": len(rows),
        "columns": headers,
        "quality": "knowledge_pack",
    })
