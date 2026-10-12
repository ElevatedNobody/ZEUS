"""
Where ZEUS keeps things on disk, in one place.

Every location defaults to exactly where it has always been (inside the
install folder, and the generated-documents folder under ~/Documents), so an
existing installation sees no change. Each one can be moved with an
environment variable, which is what lets the test suite run against a
throw-away directory instead of the operator's real data. The CRUDEX_* name is
read first; the AGNI_* name of the same variable is still honoured, so scripts
and shortcuts written before the rename keep working:

    CRUDEX_DATA_DIR      (AGNI_DATA_DIR)       database, vault, industrial assets   (default <install>/database)
    CRUDEX_LOG_DIR       (AGNI_LOG_DIR)        application, audit and security logs (default <install>/logs)
    CRUDEX_DOCUMENTS_DIR (AGNI_DOCUMENTS_DIR)  generated documents  (default ~/Documents/ZEUS_Documents,
                                                or ZEUS_Documents where that folder already exists)

Nothing here imports anything beyond the standard library, so it is safe to
use from the earliest startup code.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Generated documents. A NEW installation gets ZEUS_Documents. An existing
# one keeps the ZEUS_Documents folder it already has: the artifact registry
# records absolute paths into it, so moving it would orphan every record.
DOCUMENTS_DIRNAME = "ZEUS_Documents"
LEGACY_DOCUMENTS_DIRNAME = "ZEUS_Documents"

_ENV = {
    "data": ("CRUDEX_DATA_DIR", "AGNI_DATA_DIR"),
    "log": ("CRUDEX_LOG_DIR", "AGNI_LOG_DIR"),
    "documents": ("CRUDEX_DOCUMENTS_DIR", "AGNI_DOCUMENTS_DIR"),
}


def _from_env(names, default: Path) -> Path:
    for name in (names if isinstance(names, (tuple, list)) else (names,)):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return default


def data_dir() -> Path:
    return _from_env(_ENV["data"], ROOT / "database")


def log_dir() -> Path:
    return _from_env(_ENV["log"], ROOT / "logs")


def default_documents_root(home: Path = None) -> Path:
    documents = (home or Path.home()) / "Documents"
    legacy = documents / LEGACY_DOCUMENTS_DIRNAME
    if legacy.is_dir() and not (documents / DOCUMENTS_DIRNAME).exists():
        return legacy
    return documents / DOCUMENTS_DIRNAME


def documents_root() -> Path:
    return _from_env(_ENV["documents"], default_documents_root())


def config_path() -> Path:
    return ROOT / "config.ini"


def protected_roots() -> list:
    """Locations that no AI-invoked file tool may read, write or delete.

    These hold credentials, the encrypted vault, the audit trail, the
    security configuration and ZEUS's own code. The subsystems that own them
    use them directly; a tool call routed from a prompt never may.
    """
    return [
        data_dir(),
        log_dir(),
        config_path(),
        ROOT / "auth",
        ROOT / "core",
        ROOT / "memory",
        ROOT / "security",
        ROOT / "config",
        ROOT / "ui",
        ROOT / "voice",
        ROOT / "industrial",
        ROOT / "nodes",
        ROOT / "crudex_net",
        ROOT / "crudex_server",
        ROOT / ".env",
        ROOT / "main.py",
        ROOT / "User Editable Files" / "main.py",
    ]
