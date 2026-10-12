"""
ZEUS Industrial Workbench (SIH26117)
======================================

Sovereign, on-premise, offline-first industrial document intelligence layered
on top of the existing ZEUS desktop assistant.

Design rules this package obeys (non-negotiable):

* No web framework, no REST/HTTP server, no socket server, no cloud SDK, no
  telemetry. Every module here is a plain Python module called in-process by
  the existing tool registry / decision engine.
* The only network traffic is loopback to the local Ollama daemon
  (http://localhost:11434) - the same runtime ZEUS already used.
* Nothing downloads a model or a package at runtime. If something is missing,
  the affected feature reports it clearly and the rest keeps working.
* Existing ZEUS behaviour is never replaced. The industrial knowledge base
  lives in its own SQLite tables beside the original `document_chunks` RAG
  store, which continues to serve the existing index_folder /
  ask_knowledge_base tools unchanged.

Module map
----------
environment      Phase A - environment audit -> config/industrial_environment.json
ocr_engines      Phase B - layered OCR (PaddleOCR / Tesseract / EasyOCR)
doc_pipeline     Phase C - file type detection, layered extraction, layout,
                 tables, images, metadata
extraction       Phase C - normalisation, metadata harvesting, provenance-preserving
                 chunking
taxonomy         Phase F - industrial terminology, synonyms, abbreviations
schemas          Phase G - industrial document schemas: classify, extract, validate
knowledge_base   Phase D - embedding (BGE-M3), storage, retrieval, evidence
reranker         Phase D - evidence ranking (BGE-Reranker-v2-M3 or hybrid fallback)
provenance       Phase D - evidence objects and citation enforcement
corpus           Phase E - local industrial knowledge corpus loader
model_registry   Phase J - model capabilities + local health
agent_interfaces Phase K - agent contracts for the industrial agent layer
tools            Registers industrial tools into the existing tool registry
"""

from pathlib import Path

__version__ = "1.0.0"

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
INDUSTRIAL_CONFIG_DIR = CONFIG_DIR / "industrial"
KNOWLEDGE_DIR = ROOT / "knowledge"
KNOWLEDGE_SOURCES_DIR = ROOT / "knowledge_sources"
from core.paths import data_dir as _data_dir, log_dir as _log_dir  # noqa: E402
LOGS_DIR = _log_dir()
DATABASE_DIR = _data_dir()
ASSETS_DIR = DATABASE_DIR / "industrial_assets"

TAXONOMY_PATH = INDUSTRIAL_CONFIG_DIR / "taxonomy.json"
SCHEMAS_PATH = INDUSTRIAL_CONFIG_DIR / "document_schemas.json"
MODEL_REGISTRY_PATH = INDUSTRIAL_CONFIG_DIR / "model_registry.json"
MODEL_HEALTH_PATH = INDUSTRIAL_CONFIG_DIR / "model_health.json"
ENVIRONMENT_REPORT_PATH = CONFIG_DIR / "industrial_environment.json"
INGESTION_LOG_PATH = LOGS_DIR / "knowledge_ingestion.log"


def ensure_directories() -> None:
    """Create every directory the industrial layer writes to. Safe to call
    repeatedly; used by the setup script, the ingestion tool and the offline
    test so none of them fail on a fresh checkout."""
    for d in (CONFIG_DIR, INDUSTRIAL_CONFIG_DIR, KNOWLEDGE_DIR, KNOWLEDGE_SOURCES_DIR,
              LOGS_DIR, DATABASE_DIR, ASSETS_DIR):
        d.mkdir(parents=True, exist_ok=True)


__all__ = [
    "ROOT", "CONFIG_DIR", "INDUSTRIAL_CONFIG_DIR", "KNOWLEDGE_DIR",
    "KNOWLEDGE_SOURCES_DIR", "LOGS_DIR", "DATABASE_DIR", "ASSETS_DIR",
    "TAXONOMY_PATH", "SCHEMAS_PATH", "MODEL_REGISTRY_PATH", "MODEL_HEALTH_PATH",
    "ENVIRONMENT_REPORT_PATH", "INGESTION_LOG_PATH", "ensure_directories",
    "__version__",
]
