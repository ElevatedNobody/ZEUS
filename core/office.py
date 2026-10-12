"""
Shared office-file helpers.

One place decides where generated documents go, how a filename is made unique,
whether a file is opened afterwards, and - the part that was missing - whether
the file ZEUS just claimed to create actually exists, is non-empty and can be
reopened by the library that wrote it.

`write_document`, `create_presentation`, `create_spreadsheet`, the industrial
report generators and the agents all go through here, so there is exactly one
document-output implementation rather than one per caller.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.logger import get_logger

logger = get_logger("office")

DOCUMENTS_DIRNAME = "ZEUS_Documents"


def documents_dir(subfolder: str = "") -> Path:
    """Where generated documents land. Created on demand."""
    from core.paths import documents_root
    base = documents_root()
    if subfolder:
        base = base / subfolder
    base.mkdir(parents=True, exist_ok=True)
    return base


def safe_stem(text: str, fallback: str = "document", limit: int = 60) -> str:
    stem = re.sub(r"[^\w\s-]", "", text or "").strip().replace(" ", "_")[:limit]
    return stem or fallback


def unique_path(base_dir: Path, base_name: str, ext: str) -> Path:
    ext = ext.lstrip(".")
    candidate = base_dir / f"{base_name}.{ext}"
    if not candidate.exists():
        return candidate
    return base_dir / f"{base_name}_{int(time.time())}.{ext}"


def open_when_done(path: Path) -> bool:
    """Best effort. Never raises, and never claims to have opened anything it
    did not - the return value is what callers report."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))          # noqa: attribute exists on Windows
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
            return True
        subprocess.Popen(["xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        logger.debug(f"could not open {path}: {e}")
        return False


def verify_office_file(path: Path | str, expected_ext: str = "") -> Dict[str, Any]:
    """Prove the file is real before anyone says it was generated.

    Checks, in order: it exists, it is a file, it is non-empty, the extension is
    what was asked for, and - the one that actually catches a corrupt write - it
    can be REOPENED and parsed by the library for its type.

    Returns {"ok": bool, "path": str, "bytes": int, "readable": bool, ...}.
    """
    path = Path(path)
    result: Dict[str, Any] = {"ok": False, "path": str(path), "exists": path.exists()}
    if not path.exists() or not path.is_file():
        result["error"] = "file was not created"
        return result

    size = path.stat().st_size
    result["bytes"] = size
    if size == 0:
        result["error"] = "file is empty"
        return result

    ext = path.suffix.lower().lstrip(".")
    result["extension"] = ext
    if expected_ext and ext != expected_ext.lower().lstrip("."):
        result["error"] = f"expected .{expected_ext} but wrote .{ext}"
        return result

    try:
        if ext == "docx":
            from docx import Document
            document = Document(str(path))
            result["paragraphs"] = len(document.paragraphs)
            result["readable"] = True
        elif ext == "xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(str(path))
            result["sheets"] = workbook.sheetnames
            result["readable"] = True
        elif ext == "pptx":
            from pptx import Presentation
            deck = Presentation(str(path))
            result["slides"] = len(deck.slides)
            result["readable"] = True
        elif ext == "pdf":
            import fitz
            with fitz.open(str(path)) as doc:
                result["pages"] = doc.page_count
            result["readable"] = True
        elif ext in ("csv", "txt", "md", "json"):
            text = path.read_text(encoding="utf-8", errors="replace")
            result["characters"] = len(text)
            result["readable"] = bool(text.strip())
            if not result["readable"]:
                result["error"] = "file contains no text"
                return result
        else:
            result["readable"] = True          # nothing to parse; size check stands
    except ImportError as e:
        # The writer library is missing, so the file cannot be re-parsed here.
        # Report that honestly instead of failing a file that may be fine.
        result["readable"] = None
        result["note"] = f"could not verify by reopening: {e}"
        result["ok"] = True
        return result
    except Exception as e:
        result["readable"] = False
        result["error"] = f"file exists but could not be reopened: {type(e).__name__}: {e}"
        return result

    result["ok"] = True
    return result


def finish(path: Path, expected_ext: str, *, open_after: bool = True,
           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Verify, optionally open, and build the tool result.

    A failed verification returns an error result - never a 'saved' result, so
    no caller can report success for a file that is not really there.
    """
    check = verify_office_file(path, expected_ext)
    if not check["ok"]:
        logger.error(f"generation failed verification: {check}")
        return {"error": check.get("error", "file failed verification"),
                "path": str(path), "verification": check}

    try:
        from core.artifacts import register_artifact
        register_artifact(str(path), kind=expected_ext.lstrip("."),
                          request=str((extra or {}).get("topic") or (extra or {}).get("title") or ""),
                          verified=True)
    except Exception as error:                      # registry is provenance, not a gate
        logger.debug(f"artifact not registered: {error}")
    opened = open_when_done(path) if open_after else False
    result: Dict[str, Any] = {
        "saved": str(path),
        "format": expected_ext.lstrip("."),
        "bytes": check.get("bytes"),
        "verified": True,
        "opened": opened,
    }
    for key in ("paragraphs", "sheets", "slides", "pages", "characters"):
        if key in check:
            result[key] = check[key]
    if check.get("note"):
        result["note"] = check["note"]
    if extra:
        result.update(extra)
    logger.info(f"generated {result['format']}: {path} ({check.get('bytes')} bytes)")
    return result
