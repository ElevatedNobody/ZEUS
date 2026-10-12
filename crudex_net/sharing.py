"""
What may leave this device, decided before anything is queued.

1. The sharing category (settings.sharing_policy):
       LOCAL_ONLY  never leaves this device
       SHARED      may be sent to a paired device the user chose
       ASK         only with the user's confirmation for this one send
2. Secrets never leave, whatever the category:
       files      private keys and credential stores (by name, and by content:
                  a "-----BEGIN ... PRIVATE KEY-----" block, a ZEUS / cloud
                  API key), and anything inside ZEUS's protected folders
                  (its database, vault, logs, configuration and code - the same
                  security/pathguard.py rule every file tool obeys)
       text       memory / knowledge / conversation text that holds a password,
                  key or token (memory/write_policy.py's detector - the rule the
                  memory store itself applies)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

from crudex_net import settings

SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".kdb", ".ppk", ".jks", ".keystore", ".ovpn"}
_SECRET_NAME = re.compile(r"^(id_(rsa|dsa|ecdsa|ed25519)(?!.*\.pub$).*|\.env|.*\.env|\.netrc|_netrc|\.git-credentials|"
                          r"credentials(\.[a-z]+)?|secrets?\.(json|ya?ml|toml|ini)|wallet\.dat|.*\.crdownload)$", re.I)
_SECRET_CONTENT = [
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bcrx_[A-Za-z0-9_\-]{20,}"),
    re.compile(rb"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_\-]{20,}"),
]


class NeedsConfirmation(Exception):
    """The category is ASK and this send was not confirmed by the user."""


def check_category(category: str, confirmed: bool) -> None:
    choice = settings.sharing_policy().get(category, settings.LOCAL_ONLY)
    if choice == settings.LOCAL_ONLY:
        raise PermissionError(f"Sharing '{category}' is set to LOCAL ONLY on this device - change it in "
                              f"ZEUS NETWORK → Sharing to send it.")
    if choice == settings.ASK and not confirmed:
        raise NeedsConfirmation(f"Sharing '{category}' is set to ASK BEFORE SHARING - confirm this send.")


def check_file(path: str) -> Tuple[Path, int]:
    """(resolved path, size) of a file that may be sent - or PermissionError / FileNotFoundError."""
    from security import pathguard
    ok, why = pathguard.check(path)
    if not ok:
        raise PermissionError(why)
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"{target} is not a file")
    if target.suffix.lower() in SECRET_EXTENSIONS or _SECRET_NAME.match(target.name):
        raise PermissionError(f"{target.name} looks like a key or credential file - ZEUS never sends those")
    with target.open("rb") as handle:
        head = handle.read(64 * 1024)
    for pattern in _SECRET_CONTENT:
        if pattern.search(head):
            raise PermissionError(f"{target.name} contains a private key or an API key - ZEUS never sends secrets")
    return target, target.stat().st_size


def check_text(text: str, what: str) -> None:
    from memory.write_policy import secret_kind
    kind = secret_kind(text or "")
    if kind:
        raise PermissionError(f"This {what} looks like it contains a {kind.replace('_', ' ')} - "
                              f"ZEUS never shares secrets")
    for pattern in _SECRET_CONTENT:
        if pattern.search((text or "").encode("utf-8", "ignore")):
            raise PermissionError(f"This {what} contains a key - ZEUS never shares secrets")


def category_for_file(file_kind: str) -> str:
    return {"artifact": "artifact", "project_bundle": "document", "sync_file": "document"}.get(file_kind, "document")


def category_for_item(item_type: str) -> Optional[str]:
    return {"memory": "memory", "knowledge": "knowledge", "conversation": "conversation",
            "preferences": "preferences", "workspace": "preferences"}.get(item_type)
