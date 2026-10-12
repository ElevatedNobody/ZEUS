"""
ZEUS NETWORK settings.

    mode         "offline" (default) | "online"        - per installation, crudex_net.db
    server URL   CRUDEX_SERVER_URL, else config.ini [crudex] server_url. There is
                 NO built-in server address: an installation that was not given
                 one says "no server configured" and stays offline.
    CA file      CRUDEX_CA_FILE, else config.ini [crudex] ca_file - an extra
                 certificate authority to trust (a self-hosted server). The
                 Windows trust store (and certifi) are always used as well;
                 certificate and host-name verification cannot be switched off.
    sharing      what may leave this device, per kind of data:
                 LOCAL_ONLY | SHARED | ASK (ask every time)

Only TLS URLs are accepted (wss://, or https:// which means the same server):
the API key must never cross a network in clear text.
"""

from __future__ import annotations

import configparser
import os
import re
import ssl
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from crudex_net import store

MODES = ("offline", "online")
LOCAL_ONLY, SHARED, ASK = "LOCAL_ONLY", "SHARED", "ASK"
SHARING_CHOICES = (LOCAL_ONLY, SHARED, ASK)
SHARING_CATEGORIES = ("conversation", "task", "artifact", "document", "knowledge", "memory", "preferences")
DEFAULT_SHARING: Dict[str, str] = {"conversation": ASK, "task": SHARED, "artifact": ASK, "document": ASK,
                                   "knowledge": ASK, "memory": LOCAL_ONLY, "preferences": LOCAL_ONLY}


# ------------------------------------------------------------------- mode ----
def mode() -> str:
    value = store.get("mode", "offline")
    return value if value in MODES else "offline"


def set_mode(value: str) -> str:
    if value not in MODES:
        raise ValueError("mode is 'online' or 'offline'")
    store.put("mode", value)
    return value


# ------------------------------------------------------------- the server ----
def _config() -> configparser.ConfigParser:
    from core.paths import config_path
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path(), encoding="utf-8")
    except Exception:
        pass
    return parser


def server_url_source() -> Tuple[str, str]:
    """(url, where it came from: 'environment' | 'config.ini' | '')."""
    env = os.environ.get("CRUDEX_SERVER_URL", "").strip()
    if env:
        return env, "environment"
    value = _config().get("crudex", "server_url", fallback="").strip()
    return (value, "config.ini") if value else ("", "")


def ca_file() -> str:
    env = os.environ.get("CRUDEX_CA_FILE", "").strip()
    return env or _config().get("crudex", "ca_file", fallback="").strip()


def normalise_url(url: str) -> str:
    """https://host[:port][/path] or wss://... -> wss://host:port/path. Anything else is refused."""
    text = (url or "").strip()
    if not text:
        raise ValueError("no server address")
    parsed = urlparse(text)
    if parsed.scheme not in ("wss", "https"):
        raise ValueError("ZEUS connects only over TLS - the address must start with wss:// or https://")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("the address needs a host name and must not contain a user name or password")
    try:
        port = parsed.port or 443
    except ValueError:
        raise ValueError("the port in the address is not valid")
    host = parsed.hostname
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    path = parsed.path if parsed.path not in ("", "/") else "/v1"
    return urlunparse(("wss", netloc, path, "", "", ""))


def endpoint() -> Optional[Dict[str, object]]:
    url, source = server_url_source()
    if not url:
        return None
    ws_url = normalise_url(url)
    parsed = urlparse(ws_url)
    return {"url": ws_url, "host": parsed.hostname, "port": parsed.port or 443, "source": source}


def set_server_url(url: str, ca: Optional[str] = None) -> Dict[str, str]:
    """Write config.ini [crudex] (administrator only - checked by the caller).
    Comments and every other line of config.ini are kept as they are."""
    from core.paths import config_path
    value = normalise_url(url) if url.strip() else ""
    if ca is not None and ca.strip() and not Path(ca.strip()).is_file():
        raise ValueError("the CA file does not exist")
    path = config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = _ini_set(text, "crudex", "server_url", value)
    if ca is not None:
        text = _ini_set(text, "crudex", "ca_file", ca.strip())
    path.write_text(text, encoding="utf-8")
    return {"server_url": value, "ca_file": ca_file()}


def _ini_set(text: str, section: str, key: str, value: str) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == f"[{section}]"), None)
    if start is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{key} = {value}\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = f"{key} = {value}"
            return "\n".join(lines) + "\n"
    lines.insert(end, f"{key} = {value}")
    return "\n".join(lines) + "\n"


def ssl_context() -> ssl.SSLContext:
    """Certificate verification and host-name checking always on; TLS 1.2+."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    extra = ca_file()
    if extra:
        if not Path(extra).is_file():
            raise ValueError(f"the configured CA file does not exist: {extra}")
        context.load_verify_locations(cafile=extra)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


# ---------------------------------------------------------------- sharing ----
def sharing_policy() -> Dict[str, str]:
    saved = store.get("sharing_policy", {}) or {}
    return {category: saved.get(category, default) if saved.get(category, default) in SHARING_CHOICES else default
            for category, default in DEFAULT_SHARING.items()}


def set_sharing(category: str, choice: str) -> Dict[str, str]:
    if category not in SHARING_CATEGORIES or choice not in SHARING_CHOICES:
        raise ValueError(f"category is one of {', '.join(SHARING_CATEGORIES)}; choice one of {', '.join(SHARING_CHOICES)}")
    policy = sharing_policy()
    policy[category] = choice
    store.put("sharing_policy", policy)
    store.audit("sharing_policy_set", {"category": category, "choice": choice})
    return policy


def received_folder() -> Path:
    from core.paths import documents_root
    folder = documents_root() / "ZEUS Received"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
