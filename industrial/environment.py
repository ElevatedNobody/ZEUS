"""
Phase A - Industrial environment audit.

Inspects the machine ZEUS is actually installed on and writes a compact
report to config/industrial_environment.json. Read-only: it installs nothing,
downloads nothing, and never contacts anything except the local Ollama daemon
on loopback (and only if it is already running).

Used by:
  scripts/audit_environment.py     (standalone audit)
  scripts/setup_industrial_env.py  (before/after install)
  scripts/offline_test.py          (readiness gate)
  start_medha_industrial.bat      (startup readiness report)
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from industrial import ENVIRONMENT_REPORT_PATH, ROOT, ensure_directories

# --------------------------------------------------------------------------
# What the industrial workbench cares about. `required` entries block
# readiness; `optional` entries degrade a capability but never break ZEUS.
# --------------------------------------------------------------------------

REQUIRED_LIBRARIES: Dict[str, str] = {
    # import name -> pip name
    "fitz": "PyMuPDF",
    "PIL": "Pillow",
    "numpy": "numpy",
    "docx": "python-docx",
    "openpyxl": "openpyxl",
    "pptx": "python-pptx",
    "pandas": "pandas",
    "httpx": "httpx",
}

OPTIONAL_LIBRARIES: Dict[str, str] = {
    "pytesseract": "pytesseract",
    "paddleocr": "paddleocr",
    "paddle": "paddlepaddle",
    "cv2": "opencv-python-headless",
    "easyocr": "easyocr",
    "pypdf": "pypdf",
    "unstructured": "unstructured",
    "reportlab": "reportlab",
    "PySide6": "PySide6",
    "pyttsx3": "pyttsx3",
    "faster_whisper": "faster-whisper",
    "sounddevice": "sounddevice",
    "chromadb": "chromadb",
    "FlagEmbedding": "FlagEmbedding",
    "sentence_transformers": "sentence-transformers",
}

# Grouped views used in the report and by the startup script.
OCR_ENGINE_LIBS = ["pytesseract", "paddleocr", "easyocr"]
DOCUMENT_PROCESSOR_LIBS = ["fitz", "pypdf", "docx", "openpyxl", "pptx", "pandas", "unstructured"]
IMAGE_LIBS = ["PIL", "cv2", "numpy"]

VISION_MODEL_NAMES = ["qwen2.5vl", "moondream", "llava", "minicpm-v", "granite3.2-vision"]
EMBEDDING_MODEL_NAMES = ["bge-m3", "nomic-embed-text", "mxbai-embed", "snowflake-arctic-embed"]
RERANKER_MODEL_NAMES = ["bge-reranker"]


def _module_version(import_name: str) -> Optional[str]:
    """Best-effort version lookup that does NOT import the module (importing
    paddle/easyocr is slow and can print banners)."""
    from importlib import metadata
    pip_name = REQUIRED_LIBRARIES.get(import_name) or OPTIONAL_LIBRARIES.get(import_name) or import_name
    for candidate in (pip_name, import_name, import_name.replace("_", "-")):
        try:
            return metadata.version(candidate)
        except Exception:
            continue
    return None


def _has_module(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def detect_python() -> dict:
    in_venv = (hasattr(sys, "real_prefix") or sys.base_prefix != sys.prefix
               or bool(os.environ.get("VIRTUAL_ENV")))
    return {
        "version": platform.python_version(),
        "version_info": list(sys.version_info[:3]),
        "executable": sys.executable,
        "implementation": platform.python_implementation(),
        "architecture": platform.architecture()[0],
        "meets_minimum": sys.version_info >= (3, 10),
        "virtual_environment": {
            "active": in_venv,
            "path": os.environ.get("VIRTUAL_ENV") or (sys.prefix if in_venv else None),
        },
    }


def detect_platform() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "is_windows": os.name == "nt",
    }


def detect_libraries() -> dict:
    installed, missing = {}, []
    for import_name, pip_name in REQUIRED_LIBRARIES.items():
        if _has_module(import_name):
            installed[pip_name] = _module_version(import_name) or "unknown"
        else:
            missing.append({"package": pip_name, "import": import_name, "severity": "required"})
    optional_missing = []
    for import_name, pip_name in OPTIONAL_LIBRARIES.items():
        if _has_module(import_name):
            installed[pip_name] = _module_version(import_name) or "unknown"
        else:
            optional_missing.append({"package": pip_name, "import": import_name, "severity": "optional"})
    return {"installed": dict(sorted(installed.items())),
            "missing_required": missing,
            "missing_optional": optional_missing}


def detect_tesseract_binary() -> dict:
    """Tesseract needs the native binary, not just the python wrapper. On
    Windows it is usually installed to Program Files and may not be on PATH,
    so check the common locations too."""
    exe = shutil.which("tesseract")
    candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ROOT / "models" / "tesseract" / "tesseract.exe",
    ]
    if not exe:
        for c in candidates:
            try:
                if c.is_file():
                    exe = str(c)
                    break
            except OSError:
                continue
    if not exe:
        return {"available": False, "path": None, "version": None, "languages": []}

    version, languages = None, []
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        version = (r.stdout or r.stderr or "").splitlines()[0].strip() if (r.stdout or r.stderr) else None
    except Exception:
        pass
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=15)
        languages = [ln.strip() for ln in (r.stdout or "").splitlines()[1:] if ln.strip()]
    except Exception:
        pass
    return {"available": True, "path": exe, "version": version, "languages": languages}


def detect_paddle_models() -> dict:
    """PaddleOCR downloads its detection/recognition/layout models on first
    use. For a disconnected machine they must already be in the local cache -
    report whether that cache exists so setup can warn before the network is
    pulled out."""
    home = Path.home()
    candidates = [home / ".paddleocr", home / ".paddlex", ROOT / "models" / "paddleocr"]
    found = []
    for c in candidates:
        try:
            if c.is_dir():
                sub = [p.name for p in c.rglob("*") if p.is_dir()][:40]
                found.append({"path": str(c), "entries": len(sub)})
        except OSError:
            continue
    return {"model_cache_present": bool(found), "locations": found,
            "note": "PaddleOCR must have its models cached locally before the machine is disconnected."}


def detect_ocr_engines() -> dict:
    tess = detect_tesseract_binary()
    engines = {
        "tesseract": {
            "python_binding": _has_module("pytesseract"),
            "binary": tess,
            "usable": _has_module("pytesseract") and tess["available"],
            "role": "reliable local OCR fallback",
        },
        "paddleocr": {
            "python_binding": _has_module("paddleocr"),
            "backend_installed": _has_module("paddle"),
            "models": detect_paddle_models(),
            "usable": _has_module("paddleocr") and _has_module("paddle"),
            "role": "document OCR + layout/structure analysis",
        },
        "easyocr": {
            "python_binding": _has_module("easyocr"),
            "usable": _has_module("easyocr"),
            "role": "existing ZEUS screen OCR (preserved, unchanged)",
        },
    }
    engines["available"] = [name for name in ("paddleocr", "tesseract", "easyocr")
                            if engines[name]["usable"]]
    return engines


def detect_ollama(timeout: float = 3.0) -> dict:
    """Loopback-only check of the local Ollama daemon. Never pulls anything."""
    info = {"binary_on_path": shutil.which("ollama") is not None,
            "daemon_reachable": False, "host": None, "models": [], "error": None}
    try:
        from core.ollama_client import ollama_client
        info["host"] = ollama_client.host
        if ollama_client.is_available(force=True):
            info["daemon_reachable"] = True
            info["models"] = sorted(ollama_client.list_models())
    except Exception as e:  # pragma: no cover - defensive
        info["error"] = str(e)
    return info


def _match_models(models: List[str], needles: List[str]) -> List[str]:
    return sorted({m for m in models for n in needles if m.lower().startswith(n.lower())})


def detect_model_capabilities(models: List[str]) -> dict:
    return {
        "vision_models": _match_models(models, VISION_MODEL_NAMES),
        "embedding_models": _match_models(models, EMBEDDING_MODEL_NAMES),
        "reranker_models": _match_models(models, RERANKER_MODEL_NAMES),
        "text_models": sorted(m for m in models
                              if not any(m.lower().startswith(n.lower())
                                         for n in VISION_MODEL_NAMES + EMBEDDING_MODEL_NAMES
                                         + RERANKER_MODEL_NAMES)),
    }


def detect_vector_store() -> dict:
    """ZEUS's vector store is local SQLite - deliberately not a server."""
    from memory.database import DB_PATH
    stats, error = {}, None
    try:
        from memory.database import init_db, industrial_kb_stats
        init_db()
        stats = industrial_kb_stats()
    except Exception as e:
        error = str(e)
    return {
        "type": "sqlite (local file, in-process)",
        "path": str(DB_PATH),
        "exists": DB_PATH.exists(),
        "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "server_required": False,
        "chromadb_installed": _has_module("chromadb"),
        "chromadb_used": False,
        "industrial_kb": stats,
        "error": error,
    }


def detect_embedding_system(models: List[str]) -> dict:
    """What this machine can embed and rerank with, as verified by a probe.

    `embedding_available` used to mean "a model name appears in the installed
    list", which is a guess about an interface. It now means an embedding call
    actually returned a vector, and `reranker_available` means the reranker
    actually ordered a probe correctly - so neither can claim READY on a
    machine where the path is broken.
    """
    from core.capabilities import embedding_provider, rerank_provider

    embed_report = embedding_provider.check()
    rerank_report = rerank_provider.check()
    caps = detect_model_capabilities(models)
    return {
        "embedding_model": embed_report.model or "none resolved",
        "embedding_available": embed_report.ok,
        "embedding_health": embed_report.health.value,
        "embedding_detail": embed_report.detail,
        "embedding_dimension": embed_report.evidence.get("dimension"),
        "embedding_candidates": caps["embedding_models"],
        "reranker_model": rerank_report.model or "hybrid local scorer",
        "reranker_available": rerank_report.ok,
        "reranker_health": rerank_report.health.value,
        "reranker_detail": rerank_report.detail,
        "reranker_candidates": caps["reranker_models"],
        "local_cross_encoder": {
            "FlagEmbedding": _has_module("FlagEmbedding"),
            "sentence_transformers": _has_module("sentence_transformers"),
            "local_model_dir": str(ROOT / "models" / "reranker"),
            "local_model_present": (ROOT / "models" / "reranker").is_dir(),
        },
    }


def detect_existing_medha() -> dict:
    """Confirms the pre-existing subsystems are present and untouched."""
    def exists(rel: str) -> bool:
        return (ROOT / rel).exists()

    tools_count, tool_names = 0, []
    try:
        from core.tool_registry import registry
        # Plugin tools register on discovery, exactly as main.py does it. Without
        # this the audit under-reports the tool count and a healthy install looks
        # like it lost a tool.
        try:
            from plugins.plugin_loader import discover_plugins
            discover_plugins()
        except Exception:
            pass
        tool_names = sorted(t.name for t in registry.list_tools())
        tools_count = len(tool_names)
    except Exception:
        pass

    agents = []
    try:
        from core.agents import AGENT_REGISTRY
        agents = sorted(AGENT_REGISTRY.keys())
    except Exception:
        pass

    return {
        "entry_point": {
            "main.py": exists("main.py") or exists("User Editable Files/main.py"),
            "launcher.py": exists("launcher.py") or exists("User Editable Files/launcher.py"),
        },
        "ui": {"framework": "PySide6 (native desktop, no browser)",
               "dashboard": exists("ui/dashboard.py"), "hud_widgets": exists("ui/hud_widgets.py"),
               "pyside6_installed": _has_module("PySide6")},
        "voice": {
            "tts_module": exists("voice/text_to_speech.py"),
            "tts_engine": "Chatterbox TTS (local weights, models/chatterbox)",
            "tts_fallback": "pyttsx3 (offline SAPI on Windows)",
            "pyttsx3_installed": _has_module("pyttsx3"),
            "chatterbox_engine": exists("voice/chatterbox_engine.py"),
            "chatterbox_installed": _has_module("chatterbox"),
            "stt_module": exists("voice/speech_to_text.py"),
            "stt_engine": "faster-whisper (local weights, models/whisper)",
            "faster_whisper_installed": _has_module("faster_whisper"),
            "offline_enforcement": exists("voice/offline.py"),
            "note": "Voice is one layer over the existing command processor and "
                    "memory - no cloud speech services, no separate voice memory.",
        },
        "memory": {"database": exists("memory/database.py"),
                   "memory_manager": exists("memory/memory_manager.py"),
                   "store": "sqlite (database/medha.db)"},
        "model_layer": {"ollama_client": exists("core/ollama_client.py"),
                        "model_router": exists("core/model_router.py"),
                        "model_activity": exists("core/model_activity.py")},
        "orchestration": {"decision_engine": exists("core/decision_engine.py"),
                          "planner": exists("core/planner.py"),
                          "task_manager": exists("core/task_manager.py"),
                          "security_manager": exists("core/security_manager.py")},
        "tools": {"registry": exists("core/tool_registry.py"), "count": tools_count,
                  "names": tool_names},
        "agents": {"registry": exists("core/agents.py"), "ids": agents},
        "file_document_manager": {
            "file_tools": ["search_files", "read_file", "write_file", "copy_file", "move_file",
                           "create_folder", "delete_file", "convert_file"],
            "generation_tools": ["write_document", "create_presentation", "create_spreadsheet"],
        },
        "startup_scripts": {
            "START.bat": exists("START.bat"),
            "FIX_PACKAGES.bat": exists("FIX_PACKAGES.bat"),
            "BUILD_EXE.bat": exists("BUILD_EXE.bat"),
            "start_medha_industrial.bat": exists("start_medha_industrial.bat"),
        },
        "config": {"config.ini": exists("config.ini"),
                   "industrial_taxonomy": exists("config/industrial/taxonomy.json"),
                   "industrial_schemas": exists("config/industrial/document_schemas.json"),
                   "industrial_model_registry": exists("config/industrial/model_registry.json")},
    }


def detect_knowledge_assets() -> dict:
    knowledge = ROOT / "knowledge" / "industrial_history"
    sources = ROOT / "knowledge_sources"
    domains = []
    if knowledge.is_dir():
        for d in sorted(knowledge.iterdir()):
            if d.is_dir():
                domains.append({"domain": d.name,
                                "files": len([f for f in d.rglob("*") if f.is_file()])})
    source_files = len([f for f in sources.rglob("*") if f.is_file()]) if sources.is_dir() else 0
    return {"corpus_root": str(knowledge), "corpus_present": knowledge.is_dir(),
            "domains": domains, "domain_count": len(domains),
            "knowledge_sources_dir": str(sources), "knowledge_sources_present": sources.is_dir(),
            "knowledge_source_files": source_files}


def _readiness(report: dict) -> dict:
    blockers, warnings = [], []

    if not report["python"]["meets_minimum"]:
        blockers.append("Python 3.10+ required")
    for m in report["libraries"]["missing_required"]:
        blockers.append(f"missing required package: {m['package']}")

    ocr = report["ocr_engines"]
    if not ocr["available"]:
        blockers.append("no OCR engine usable (need Tesseract or PaddleOCR or EasyOCR)")
    elif "tesseract" not in ocr["available"]:
        warnings.append("Tesseract fallback not usable - OCR has no independent second engine")
    if "paddleocr" not in ocr["available"]:
        warnings.append("PaddleOCR not usable - document layout/structure analysis will be "
                        "limited to the heuristic layout analyser")

    ollama = report["ollama"]
    if not ollama["daemon_reachable"]:
        warnings.append("Ollama daemon not reachable - start it with 'ollama serve'")
    else:
        if not report["embedding"]["embedding_available"]:
            blockers.append("no embedding model pulled (bge-m3) - knowledge base cannot be built")
        if not report["embedding"]["reranker_available"]:
            warnings.append("bge-reranker-v2-m3 not pulled - evidence ranking falls back to the "
                            "deterministic hybrid scorer")
        if not report["models"]["vision_models"]:
            warnings.append("no vision model pulled - multimodal page understanding disabled")

    if not report["knowledge"]["corpus_present"]:
        warnings.append("industrial knowledge corpus directory missing")

    status = "READY" if not blockers else "NOT_READY"
    if not blockers and warnings:
        status = "READY_WITH_WARNINGS"
    return {"status": status, "blockers": blockers, "warnings": warnings}


def audit(write: bool = True, check_ollama: bool = True) -> dict:
    """Run the full Phase A audit. Returns the report dict and (by default)
    writes it to config/industrial_environment.json."""
    ensure_directories()

    ollama = detect_ollama() if check_ollama else {
        "binary_on_path": shutil.which("ollama") is not None, "daemon_reachable": False,
        "host": None, "models": [], "error": "skipped"}
    models = ollama.get("models", [])

    report = {
        "schema_version": "1.0.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_at_epoch": time.time(),
        "project_root": str(ROOT),
        "problem_statement": "SIH26117 - Sovereign On-Premise Agentic AI Workbench",
        "offline_architecture": {
            "network_services": "none",
            "web_frameworks": "none (no FastAPI/Flask/REST/websocket)",
            "model_runtime": "ollama (loopback only)",
            "vector_store": "local sqlite",
            "telemetry": "none",
        },
        "platform": detect_platform(),
        "python": detect_python(),
        "libraries": detect_libraries(),
        "ocr_engines": detect_ocr_engines(),
        "document_processors": {
            pip_name: (_module_version(imp) or "installed") if _has_module(imp) else None
            for imp, pip_name in ((i, REQUIRED_LIBRARIES.get(i) or OPTIONAL_LIBRARIES.get(i, i))
                                  for i in DOCUMENT_PROCESSOR_LIBS)
        },
        "image_processing": {
            pip_name: (_module_version(imp) or "installed") if _has_module(imp) else None
            for imp, pip_name in ((i, REQUIRED_LIBRARIES.get(i) or OPTIONAL_LIBRARIES.get(i, i))
                                  for i in IMAGE_LIBS)
        },
        "ollama": ollama,
        "models": detect_model_capabilities(models),
        "embedding": detect_embedding_system(models),
        "vector_store": detect_vector_store(),
        "existing_medha": detect_existing_medha(),
        "knowledge": detect_knowledge_assets(),
    }
    report["missing_dependencies"] = (report["libraries"]["missing_required"]
                                      + report["libraries"]["missing_optional"])
    report["readiness"] = _readiness(report)
    report["environment_status"] = report["readiness"]["status"]

    if write:
        ENVIRONMENT_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENVIRONMENT_REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def load_last_report() -> Optional[dict]:
    if ENVIRONMENT_REPORT_PATH.exists():
        try:
            return json.loads(ENVIRONMENT_REPORT_PATH.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def summarise(report: dict) -> str:
    """Compact human-readable summary used by the BAT file and CLI."""
    lines = [
        "INDUSTRIAL ENVIRONMENT AUDIT",
        f"  Python           : {report['python']['version']} "
        f"({'venv' if report['python']['virtual_environment']['active'] else 'system'})",
        f"  Platform         : {report['platform']['system']} {report['platform']['release']}",
        f"  OCR engines      : {', '.join(report['ocr_engines']['available']) or 'NONE'}",
        f"  Ollama           : {'reachable' if report['ollama']['daemon_reachable'] else 'not reachable'}"
        f" ({len(report['ollama']['models'])} models)",
        f"  Embedding model  : {report['embedding']['embedding_model']}"
        f" {'OK' if report['embedding']['embedding_available'] else 'MISSING'}",
        f"  Reranker model   : {report['embedding']['reranker_model']}"
        f" {'OK' if report['embedding']['reranker_available'] else 'fallback'}",
        f"  Vision models    : {', '.join(report['models']['vision_models']) or 'none'}",
        f"  Knowledge domains: {report['knowledge']['domain_count']}",
        f"  Industrial KB    : {report['vector_store']['industrial_kb'].get('documents', 0)} docs / "
        f"{report['vector_store']['industrial_kb'].get('chunks', 0)} chunks",
        f"  Existing tools   : {report['existing_medha']['tools']['count']}",
        f"  STATUS           : {report['environment_status']}",
    ]
    for b in report["readiness"]["blockers"]:
        lines.append(f"    [BLOCKER] {b}")
    for w in report["readiness"]["warnings"]:
        lines.append(f"    [WARN]    {w}")
    return "\n".join(lines)
