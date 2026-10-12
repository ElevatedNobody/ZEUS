"""
AGNI_DIAGNOSTIC_REPORT.txt / .json - written by TEST_AGNI.bat all.

Each run is also kept as logs/diagnostics/AGNI_DIAGNOSTIC_REPORT_<stamp>.*,
so the copy in the install folder is always the latest and no earlier
evidence is overwritten.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from agni_ops.status import PASS, Result, summarize, worst

ROOT = Path(__file__).resolve().parent.parent

# Report section -> the result groups it collects.
SECTIONS = [
    ("SYSTEM", ["system"]), ("OLLAMA", ["ollama"]), ("MODELS", ["models"]),
    ("WHISPER", ["whisper"]), ("VOICE / MICROPHONE", ["voice"]), ("TTS", ["tts"]),
    ("VISION", ["vision", "ocr", "documents"]), ("INSIGHTFACE / FACE LOGIN", ["face"]),
    ("MEMORY", ["memory"]), ("HERMES", ["hermes"]), ("FIREWALL", ["firewall"]),
    ("SECURITY (AUTH / RBAC / VAULT / AUDIT)", ["security"]), ("RAG", ["rag"]),
    ("AGENTS", ["agents"]), ("TOOLS", ["tools"]), ("ARTIFACTS", ["artifacts"]),
    ("REGRESSION (TEST RESULTS)", ["regression"]),
]


def _section_of(result: Result) -> str:
    for title, groups in SECTIONS:
        if result.group in groups:
            return title
    return "OTHER"


def write(results: List[Result], system: Dict, model_table: str = "", command: str = "all",
          started: Optional[float] = None) -> Dict[str, Path]:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    counts = summarize(results)
    overall = worst([r.status for r in results])
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "command": f"TEST_AGNI.bat {command}",
               "duration_s": round(time.time() - started, 1) if started else None,
               "overall": overall, "counts": counts, "system": system,
               "results": [r.to_dict() for r in results]}

    lines = ["=" * 78, "ZEUS DIAGNOSTIC REPORT", "=" * 78,
             f"Generated : {payload['generated_at']}   ({payload['command']})",
             f"Overall   : {overall}   " + "  ".join(f"{k}={v}" for k, v in counts.items() if v),
             "Rule      : PASS means the component's real function ran and was validated.",
             "            Present / imported / downloaded / checksum-only is never PASS.", ""]
    memory = system.get("memory", {})
    gpus = ", ".join(f"{g['name']} {g['vram_total_gb']} GB" for g in system.get("gpus", [])) or "none"
    lines += ["SYSTEM", "-" * 78,
              f"  OS         {system.get('os')}   admin={system.get('admin')}",
              f"  PYTHON     {system.get('python')} {system.get('arch')}  {system.get('python_exe')}",
              f"  CPU        {system.get('cpu')} ({system.get('cores')} cores / {system.get('threads')} threads)",
              f"  RAM        {memory.get('ram_total_gb')} GB; commit {memory.get('commit_available_gb')} GB "
              f"free of {memory.get('commit_limit_gb')} GB",
              f"  GPU        {gpus}", f"  DISK       {system.get('disk_free_gb')} GB free",
              "  PACKAGES   " + ", ".join(f"{m}={p['version'] or ('yes' if p['present'] else 'MISSING')}"
                                          for m, p in (system.get("packages") or {}).items()), ""]
    if model_table:
        lines += ["MODEL TEST MATRIX", "-" * 78, model_table, ""]

    by_section: Dict[str, List[Result]] = {}
    for result in results:
        by_section.setdefault(_section_of(result), []).append(result)
    for title, _groups in SECTIONS + [("OTHER", [])]:
        items = by_section.get(title)
        if not items:
            continue
        lines += [f"{title}   [{worst([r.status for r in items])}]", "-" * 78]
        for r in items:
            lines.append(f"  {r.status:<14} {r.component}" + (f" - {r.reason}" if r.reason else ""))
            if r.status != PASS:
                for label, value in (("WHERE", r.where), ("EVIDENCE", r.evidence),
                                     ("FIX ATTEMPTED", r.fix_attempted),
                                     ("CURRENT STATE", r.current_state), ("FALLBACK", r.fallback)):
                    if value:
                        text = str(value).replace("\n", "\n                   ")
                        lines.append(f"                 {label}: {text[:1200]}")
        lines.append("")

    open_items = [r for r in results if r.status not in (PASS,)]
    lines += ["WHAT IS NOT PASS - AND WHAT TO DO", "-" * 78]
    for r in open_items:
        lines.append(f"  [{r.status}] {r.component}: {r.reason}")
    if not open_items:
        lines.append("  nothing - every test passed")
    lines.append("")

    folder = ROOT / "logs" / "diagnostics"
    try:
        from core.paths import log_dir
        folder = log_dir() / "diagnostics"
    except Exception:
        pass
    folder.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    out = {}
    for target in (folder / f"AGNI_DIAGNOSTIC_REPORT_{stamp}", ROOT / "AGNI_DIAGNOSTIC_REPORT"):
        target.with_suffix(".txt").write_text(text, encoding="utf-8")
        target.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        out[str(target.parent)] = target.with_suffix(".txt")
    return {"txt": ROOT / "AGNI_DIAGNOSTIC_REPORT.txt", "json": ROOT / "AGNI_DIAGNOSTIC_REPORT.json",
            "archived": folder / f"AGNI_DIAGNOSTIC_REPORT_{stamp}.txt"}
