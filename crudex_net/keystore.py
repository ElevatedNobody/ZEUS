"""
Secrets for the ZEUS NETWORK, kept in Windows Credential Manager.

Two secrets exist, and neither is ever written to a file, the database, a log,
an error message or the page:

    the API key            per ZEUS user on this installation
                           target  ZEUS/<install>/api-key/<user>
    the device private key one per installation (Ed25519)
                           target  ZEUS/<install>/device-key

<install> is a short hash of this installation's data folder, so two ZEUS
installations on one Windows account (and the two-device test) never read each
other's secrets. Credential Manager keeps generic credentials encrypted with the
Windows user's logon secret (DPAPI) and scoped to that Windows user; they are
stored with CRED_PERSIST_LOCAL_MACHINE, so they do not roam to other machines.

Where Credential Manager is unavailable (not Windows, pywin32 missing) the key
is NOT persisted anywhere: `available()` says so and the interface tells the
user - there is no plain-text fallback.
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Optional

_SERVICE = "ZEUS"
_KEY_FORMAT = re.compile(r"^crx_[A-Za-z0-9_\-]{20,200}$")
_session_keys = {}                  # only when Credential Manager is unavailable: memory, this run only
_lock = threading.Lock()


class KeystoreError(Exception):
    pass


def available() -> bool:
    try:
        import win32cred  # noqa: F401
        return True
    except Exception:
        return False


def install_id() -> str:
    from core.paths import data_dir
    return hashlib.sha256(str(data_dir()).lower().encode("utf-8")).hexdigest()[:12]


def _target(kind: str, user: str = "") -> str:
    base = f"{_SERVICE}/{install_id()}/{kind}"
    return f"{base}/{user.strip().lower()}" if user else base


def _write(target: str, secret: str, comment: str) -> None:
    import win32cred
    win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": target, "UserName": "crudex",
                         "CredentialBlob": secret, "Comment": comment,
                         "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)


def _read(target: str) -> Optional[str]:
    import pywintypes
    import win32cred
    try:
        blob = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)["CredentialBlob"]
    except pywintypes.error as error:
        if error.winerror == 1168:          # ERROR_NOT_FOUND
            return None
        raise KeystoreError(f"Credential Manager could not be read (error {error.winerror})") from None
    if isinstance(blob, bytes):
        return blob.decode("utf-16-le")
    return str(blob)


def _delete(target: str) -> bool:
    import pywintypes
    import win32cred
    try:
        win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        return True
    except pywintypes.error as error:
        if error.winerror == 1168:
            return False
        raise KeystoreError(f"Credential Manager could not delete the entry (error {error.winerror})") from None


# ------------------------------------------------------------------ API key --

def normalise_key(text: str) -> str:
    """The key as typed/pasted, trimmed. Raises ValueError WITHOUT echoing it."""
    key = (text or "").strip()
    if not _KEY_FORMAT.match(key):
        raise ValueError("That does not look like a ZEUS API key (it starts with 'crx_' and has no spaces).")
    return key


def fingerprint(key: str) -> str:
    """A non-reversible 8-character id of a key, for the audit trail."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def save_api_key(user: str, key: str) -> dict:
    key = normalise_key(key)
    if not user:
        raise KeystoreError("no one is signed in")
    if available():
        _write(_target("api-key", user), key, "ZEUS NETWORK API key (Online mode)")
        stored = "windows-credential-manager"
    else:
        with _lock:
            _session_keys[user.lower()] = key
        stored = "memory-only (secure storage unavailable - not saved)"
    return {"stored": stored, "masked": masked(key), "fingerprint": fingerprint(key)}


def load_api_key(user: str) -> Optional[str]:
    if not user:
        return None
    if available():
        return _read(_target("api-key", user))
    with _lock:
        return _session_keys.get(user.lower())


def remove_api_key(user: str) -> bool:
    if not user:
        return False
    if available():
        return _delete(_target("api-key", user))
    with _lock:
        return _session_keys.pop(user.lower(), None) is not None


def masked(key: Optional[str]) -> str:
    from crudex_server.protocol import masked as _masked
    return _masked(key or "") if key else ""


# ------------------------------------------------------- device private key --

def load_device_key() -> Optional[str]:
    if available():
        return _read(_target("device-key"))
    with _lock:
        return _session_keys.get("__device__")


def save_device_key(private_b64: str) -> None:
    if available():
        _write(_target("device-key"), private_b64, "ZEUS NETWORK device identity (Ed25519 private key)")
    else:
        with _lock:
            _session_keys["__device__"] = private_b64


def remove_device_key() -> bool:
    if available():
        return _delete(_target("device-key"))
    with _lock:
        return _session_keys.pop("__device__", None) is not None
