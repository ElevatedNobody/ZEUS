"""
This installation's device identity on the ZEUS NETWORK.

    device_id    dev_ + 24 random characters, created once, kept in crudex_net.db.
                 Not an IP address, not a host name: it survives a new network,
                 a renamed PC and a changed address.
    device_name  the computer name at first run; the user can change it.
    platform     e.g. "Windows 11 (10.0.26200)"
    version      the ZEUS version
    key pair     Ed25519. The public key is registered with the server the first
                 time this device connects; the private key never leaves this
                 machine (Windows Credential Manager). Every connection signs the
                 server's fresh nonce with it, so an API key alone cannot
                 impersonate this device.
"""

from __future__ import annotations

import base64
import platform
import secrets
import socket
import string
import threading
from typing import Dict

from crudex_net import keystore, store

_lock = threading.Lock()
_ALPHABET = string.ascii_lowercase + string.digits


def _platform() -> str:
    system = platform.system()
    if system == "Windows":
        version = platform.version()
        build = int(version.split(".")[-1]) if version.split(".")[-1].isdigit() else 0
        return f"Windows {'11' if build >= 22000 else platform.release()} ({version})"
    return f"{system} {platform.release()}"


def device_id() -> str:
    with _lock:
        value = store.get("device_id")
        if not value:
            value = "dev_" + "".join(secrets.choice(_ALPHABET) for _ in range(24))
            store.put("device_id", value)
            store.put("device_name", socket.gethostname()[:60] or "ZEUS device")
            store.audit("device_identity_created", {"device_id": value})
        return value


def device_name() -> str:
    device_id()
    return store.get("device_name") or socket.gethostname()


def set_device_name(name: str) -> str:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("a device name is needed")
    store.put("device_name", name)
    return name


def _private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with _lock:
        encoded = keystore.load_device_key()
        if encoded:
            return Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded))
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        keystore.save_device_key(base64.b64encode(raw).decode())
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        store.put("device_public_key", base64.b64encode(public).decode())
        return key


def public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    key = _private_key()
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                          serialization.PublicFormat.Raw)).decode()


def sign(data: bytes) -> str:
    return base64.b64encode(_private_key().sign(data)).decode()


def describe() -> Dict[str, str]:
    from core import branding
    return {"device_id": device_id(), "name": device_name(), "platform": _platform(),
            "version": branding.APP_VERSION, "public_key": public_key_b64()}
