"""
The ZEUS NETWORK operations the interface and the command bar use.

Authorisation is ZEUS's own: the bridge methods below ask
security/policy.authorize() with a tool name that the existing name-classes
already place correctly (crudex_* = READ, viewer+; set_/remove_/create_ = WRITE,
operator+), and the registered tools in crudex_net/tools.py pass through
registry.execute like every other tool. The server address is configuration:
it additionally needs an administrator (stricter, never looser). Nothing here
takes an API key through a tool call - the key reaches Python only through the
dedicated bridge method, goes straight to Credential Manager, and is never
logged, audited or returned.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from crudex_net import identity, keystore, settings, store
from crudex_net.client import NetError, client
from crudex_server import protocol as P


class Refused(Exception):
    pass


def _authorize(tool_name: str, admin: bool = False) -> str:
    from security import policy
    decision = policy.authorize(tool_name, 0, {}, origin="ui")
    if not decision.allowed:
        raise Refused(decision.reason)
    if admin and decision.identity.role != "admin":
        raise Refused("Only an administrator can change the ZEUS server address.")
    return decision.identity.username


def _user() -> str:
    from auth.session_manager import session_manager
    return session_manager.username or ""


def local_status() -> Dict[str, Any]:
    """What works with no internet at all - measured, not assumed."""
    try:
        from core.ollama_client import ollama_client
        up = ollama_client.is_available()
        models = len(ollama_client.list_models()) if up else 0
    except Exception:
        up, models = False, 0
    return {"local_processing": True, "local_model": "READY" if up else "UNAVAILABLE",
            "local_models": models,
            "local_model_detail": f"{models} local model(s) on this machine" if up
            else "the local model runtime (Ollama) is not running"}


# -------------------------------------------------------------- bridge ----
def state() -> Dict[str, Any]:
    _authorize("crudex_network_status")
    user = _user()
    key = None
    key_error = ""
    try:
        key = keystore.load_api_key(user)
    except keystore.KeystoreError as error:
        key_error = str(error)
    url, source = settings.server_url_source()
    status = client.status()
    try:
        from security.policy import current_identity
        role = current_identity().role
    except Exception:
        role = ""
    return {**status, "user": user, "has_key": bool(key), "masked_key": keystore.masked(key) if key else "",
            "key_storage": "Windows Credential Manager" if keystore.available() else
            "unavailable - the key is kept in memory for this session only", "key_error": key_error,
            "server_url": url, "server_source": source, "ca_file": settings.ca_file(),
            "can_configure_server": role == "admin" and source != "environment",
            "local": local_status(), "revoked": bool(store.get("revoked")),
            "network_name": "ZEUS NETWORK"}


def set_mode(mode: str) -> Dict[str, Any]:
    _authorize("set_crudex_mode")
    previous = settings.mode()
    settings.set_mode(mode)
    store.audit("mode_set", {"from": previous, "to": mode, "user": _user()})
    if mode == "online":
        client.start()
        client.connect_now()
    else:
        client.disconnect()
        client._manual_off = False            # offline is the mode, not a manual disconnect
        client.wake()
    return state()


def save_key(api_key: str) -> Dict[str, Any]:
    user = _authorize("set_crudex_api_key")
    try:
        saved = keystore.save_api_key(user, api_key)
    except ValueError as error:
        raise Refused(str(error)) from None
    store.put("revoked", False)
    store.audit("api_key_saved", {"user": user, "fingerprint": saved["fingerprint"], "storage": saved["stored"]})
    client.settings_changed()
    return {**state(), "saved": True}


def remove_key() -> Dict[str, Any]:
    user = _authorize("remove_crudex_api_key")
    removed = keystore.remove_api_key(user)
    store.audit("api_key_removed", {"user": user, "removed": removed})
    client.disconnect()
    return {**state(), "removed": removed}


def connect() -> Dict[str, Any]:
    _authorize("set_crudex_connection")
    if settings.mode() != "online":
        settings.set_mode("online")
    client.start()
    client.connect_now()
    store.audit("connect_requested", {"user": _user()})
    return state()


def disconnect() -> Dict[str, Any]:
    _authorize("set_crudex_connection")
    client.disconnect()
    store.audit("disconnect_requested", {"user": _user()})
    return state()


def set_server(url: str, ca_file: str) -> Dict[str, Any]:
    _authorize("set_crudex_server", admin=True)
    if settings.server_url_source()[1] == "environment":
        raise Refused("CRUDEX_SERVER_URL is set in the environment - it overrides config.ini")
    try:
        written = settings.set_server_url(url, ca_file)
    except ValueError as error:
        raise Refused(str(error)) from None
    store.audit("server_set", {"server_url": written["server_url"], "ca_file": written["ca_file"], "user": _user()})
    client.settings_changed()
    return state()


# --------------------------------------------------------------- devices ----
def _need_connection() -> None:
    if not client.connected():
        raise NetError("This device is not connected - switch to Online mode and connect first", "offline")


def devices() -> Dict[str, Any]:
    live = client.connected()
    rows = client.refresh_devices() if live else client.cached_devices()
    if not live:
        for row in rows:
            row["online"] = None
    for row in rows:
        row["grants_given"] = row.get("grants_given") or []
        row["grants_received"] = row.get("grants_received") or []
    return {"ok": True, "live": live, "state": client.status()["state"], "devices": rows,
            "this_device": {"device_id": identity.device_id(), "name": identity.device_name()},
            "permissions": [{"name": p, "text": P.PERMISSION_TEXT[p], "default": p in P.DEFAULT_GRANTS,
                             "dangerous": p in P.DANGEROUS} for p in P.PERMISSIONS]}


def resolve_device(reference: str) -> str:
    reference = (reference or "").strip()
    rows = client.cached_devices()
    for row in rows:
        if row.get("device_id") == reference:
            return reference
    matches = [r for r in rows if (r.get("name") or "").lower() == reference.lower()]
    if len(matches) == 1:
        return matches[0]["device_id"]
    if len(matches) > 1:
        raise ValueError(f"more than one device is called '{reference}' - use its device id")
    raise ValueError(f"no known device '{reference}' - pair it first (Devices → + ADD ZEUS DEVICE)")


def paired_device(reference: str) -> str:
    device_id = resolve_device(reference)
    row = next((r for r in client.cached_devices() if r.get("device_id") == device_id), {})
    if not row.get("paired"):
        raise PermissionError("that device is not paired with this one")
    if row.get("status") == "revoked":
        raise PermissionError("that device was revoked")
    return device_id


def pairing_code() -> Dict[str, Any]:
    _need_connection()
    reply = client.request({"t": "pair_create"})
    store.audit("pairing_code_created", {})
    return {"ok": True, "code": reply["code"], "expires_at": reply["expires_at"]}


def join(code: str) -> Dict[str, Any]:
    _need_connection()
    reply = client.request({"t": "pair_join", "code": str(code).strip()})
    client.refresh_devices()
    store.audit("paired", {"with": reply["device"]["device_id"]})
    return {"ok": True, "device": reply["device"]}


def unpair(reference: str) -> Dict[str, Any]:
    _need_connection()
    device_id = resolve_device(reference)
    client.request({"t": "unpair", "device_id": device_id})
    client.refresh_devices()
    store.audit("unpaired", {"device": device_id})
    return {"ok": True, "device_id": device_id}


def revoke(reference: str) -> Dict[str, Any]:
    _need_connection()
    device_id = resolve_device(reference)
    if device_id == identity.device_id():
        raise ValueError("a device cannot revoke itself - remove the saved key instead")
    client.request({"t": "revoke", "device_id": device_id})
    client.refresh_devices()
    store.audit("device_revoked", {"device": device_id})
    return {"ok": True, "device_id": device_id, "revoked": True}


def set_grants(reference: str, grants: List[str]) -> Dict[str, Any]:
    _need_connection()
    device_id = paired_device(reference)
    unknown = [g for g in grants if g not in P.PERMISSIONS]
    if unknown:
        raise ValueError(f"unknown permission(s): {', '.join(unknown)}")
    reply = client.request({"t": "grants", "device_id": device_id, "grants": grants})
    client.refresh_devices()
    store.audit("grants_set", {"device": device_id, "grants": reply["grants"]})
    return {"ok": True, "device_id": device_id, "grants": reply["grants"]}


def rename(name: str) -> Dict[str, Any]:
    name = identity.set_device_name(name)
    if client.connected():
        client.request({"t": "rename", "name": name})
    return {"ok": True, "name": name}


# ------------------------------------------------------------- messages ----
def send_message(reference: str, text: str, notification: bool = False) -> Dict[str, Any]:
    import time
    device_id = paired_device(reference)
    text = (text or "").strip()
    if not text:
        raise ValueError("the message is empty")
    if len(text) > 20000:
        raise ValueError("the message is too long - send it as a file")
    import uuid
    kind = "notification" if notification else "message"
    msg_id = f"{kind[:4]}-{uuid.uuid4().hex}"
    # The conversation row first: the receipt can arrive before queue_event returns.
    store.x("INSERT INTO messages (msg_id, peer, direction, kind, text, created_at, state) VALUES (?,?,?,?,?,?,?)",
            (msg_id, device_id, "out", kind, text, time.time(), "pending"))
    client.queue_event(device_id, kind, {"text": text}, msg_id=msg_id)
    return {"ok": True, "msg_id": msg_id, "state": "pending" if not client.connected() else "sending",
            "note": "" if client.connected() else "queued - it is sent when this device is online"}


def messages(reference: str = "", limit: int = 200) -> Dict[str, Any]:
    if reference:
        device_id = resolve_device(reference)
        rows = store.q("SELECT * FROM messages WHERE peer = ? ORDER BY created_at DESC LIMIT ?", (device_id, limit))
    else:
        rows = store.q("SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,))
    return {"ok": True, "messages": list(reversed(rows))}


# --------------------------------------------------------------- files ----
def send_file(reference: str, path: str, confirmed: bool = False, file_kind: str = "file",
              meta: Optional[Dict[str, Any]] = None, name: Optional[str] = None) -> Dict[str, Any]:
    from crudex_net import sharing
    device_id = paired_device(reference)
    sharing.check_category(sharing.category_for_file(file_kind), confirmed)
    target, _size = sharing.check_file(path)
    queued = client.queue_file(device_id, target, file_kind, meta, name)
    return {"ok": True, **queued, "note": "" if client.connected() else "queued - it is sent when this device is online"}


def send_artifact(reference: str, artifact_id: int, confirmed: bool = False) -> Dict[str, Any]:
    from core import artifacts
    row = artifacts.get_artifact(int(artifact_id))
    if not row or not row.get("exists"):
        raise ValueError("no such artifact (or its file was removed)")
    return send_file(reference, row["path"], confirmed, "artifact", {"artifact_name": row["name"],
                                                                     "sha256": row.get("sha256")})


def send_project(reference: str, confirmed: bool = False) -> Dict[str, Any]:
    import tempfile
    from jarvis import sync
    from crudex_net import sharing
    sharing.check_category("document", confirmed)
    exported = sync.export_bundle(tempfile.mkdtemp(prefix="crudex_bundle_"), all_events=True)
    if not exported.get("ok"):
        raise RuntimeError(exported.get("error") or "the project update could not be exported")
    return send_file(reference, exported["bundle"], True, "project_bundle", {"events": exported["events"]})


def transfers(limit: int = 100) -> Dict[str, Any]:
    from crudex_net.client import _transfer_view
    rows = store.q("SELECT * FROM transfers ORDER BY created_at DESC LIMIT ?", (limit,))
    return {"ok": True, "transfers": [_transfer_view(r) | {"created_at": r["created_at"], "peer_name":
                                                           client.peer_name(r["peer"])} for r in rows]}
