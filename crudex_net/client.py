"""
The ZEUS NETWORK client: this device's ONE outbound connection to the server.

States (what the interface shows - each one caused by something real):
    OFFLINE     Offline mode chosen, or nobody signed in, or Disconnect pressed
    CONNECTING  a connection attempt (or a reconnect after the network dropped)
    ONLINE      authenticated to the server, nothing waiting
    SYNCING     authenticated, and events or file bytes are moving
    ERROR       an attempt failed for a reason the user can act on (bad key,
                untrusted certificate, firewall, no server configured, revoked)

Guarantees
    * nothing leaves the device without a signed-in local session, and the
      connection closes when ZEUS locks or the user signs out
    * every outgoing event is written to the outbox first and sent until the
      server has stored it; the server dedupes a resend (same id)
    * every incoming delivery is stored locally BEFORE it is acknowledged, and a
      re-delivery of the same id is recognised and acknowledged without being
      applied twice
    * uploads and downloads resume from the last byte the other side confirmed;
      a file is only accepted when its SHA-256 matches
    * the API key is read from Credential Manager for the moment it is sent and
      is never logged, stored elsewhere or put in an error message
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import shutil
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from crudex_net import identity, keystore, settings, store
from crudex_server import protocol as P

logger = get_logger("crudex.net")

OFFLINE, CONNECTING, ONLINE, SYNCING, ERROR = "OFFLINE", "CONNECTING", "ONLINE", "SYNCING", "ERROR"
_FATAL_SEND = {"forbidden", "not_paired", "bad_kind", "revoked", "too_large", "unknown_device", "bad_request"}


class NetError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


class _AuthRefused(Exception):
    pass


class _Revoked(Exception):
    pass


def _now() -> float:
    return time.time()


def _sha256_file(path: Path, limit: Optional[int] = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as handle:
        while True:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if size == 0:
                break
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


class NetworkClient:
    def __init__(self):
        self._lock = threading.RLock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._wake = threading.Event()
        self._work = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._ws = None
        self._outq: "queue.Queue" = queue.Queue(maxsize=64)
        self._io_thread: Optional[threading.Thread] = None
        self._replies: Dict[str, Future] = {}
        self._manual_off = False
        self._pause_until_change = False
        self._approved_host: Optional[str] = None
        self._state = {"state": OFFLINE, "detail": "Offline mode - local processing only", "since": _now()}
        self._account: Dict[str, Any] = {}
        self._connected_at: Optional[float] = None
        self._up_acked: Dict[str, int] = {}
        self._up_resync: Dict[str, int] = {}
        self._up_cond = threading.Condition()
        self._download: Optional[Dict[str, Any]] = None
        self._last_progress = 0.0
        self._executor = None

    # ----------------------------------------------------------- lifecycle --
    def start(self) -> None:
        with self._lock:
            if self._running:
                self._wake.set()
                return
            self._running = True
            self._thread = threading.Thread(target=self._supervise, name="crudex-net", daemon=True)
            self._thread.start()
        try:
            from auth.session_manager import session_manager
            if not getattr(self, "_session_hooked", False):
                session_manager.on_change(self._on_session)
                self._session_hooked = True
        except Exception:
            pass

    def shutdown(self) -> None:
        self._running = False
        self._close_socket("shutting down")
        self._wake.set()

    def _on_session(self, event: str, _session) -> None:
        if event in ("lock", "logout"):
            self._close_socket(f"ZEUS {event}")
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    # -------------------------------------------------------------- events --
    def subscribe(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, event: Dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                logger.debug("listener failed", exc_info=True)

    def _set_state(self, state: str, detail: str) -> None:
        with self._lock:
            changed = (self._state["state"], self._state["detail"]) != (state, detail)
            if changed:
                self._state = {"state": state, "detail": detail, "since": _now()}
        if changed:
            logger.info(f"ZEUS NETWORK {state}: {detail}")
            self._emit({"type": "state", **self.status()})

    # -------------------------------------------------------------- status --
    def status(self) -> Dict[str, Any]:
        endpoint = None
        try:
            endpoint = settings.endpoint()
        except ValueError:
            pass
        with self._lock:
            state = dict(self._state)
            account = dict(self._account)
        pending = store.one("SELECT COUNT(*) AS n FROM outbox WHERE state = 'pending'")["n"]
        active = store.one("SELECT COUNT(*) AS n FROM transfers WHERE state IN ('queued', 'uploading', 'offered', "
                           "'downloading')")["n"]
        return {"state": state["state"], "detail": state["detail"], "since": state["since"],
                "mode": settings.mode(), "server": endpoint["host"] if endpoint else None,
                "server_port": endpoint["port"] if endpoint else None,
                "account": account.get("name"), "connected_at": self._connected_at,
                "last_sync_at": store.get("last_sync_at"), "pending_events": pending, "active_transfers": active,
                "device_id": identity.device_id(), "device_name": identity.device_name()}

    def _mark_synced(self) -> None:
        pending = store.one("SELECT COUNT(*) AS n FROM outbox WHERE state = 'pending'")["n"]
        active = store.one("SELECT COUNT(*) AS n FROM transfers WHERE state IN ('queued', 'uploading', "
                           "'downloading')")["n"]
        with self._lock:
            online = self._ws is not None and self._state["state"] in (ONLINE, SYNCING)
        if not online:
            return
        if pending == 0 and active == 0 and self._download is None:
            store.put("last_sync_at", _now())
            self._set_state(ONLINE, f"Connected to {self._account.get('server', 'the ZEUS server')} as "
                                    f"{self._account.get('name', '?')}")
            self._emit({"type": "synced", "last_sync_at": store.get("last_sync_at")})
        else:
            self._set_state(SYNCING, f"{pending} event(s) and {active} transfer(s) in progress")

    # ------------------------------------------------------------ controls --
    def connect_now(self) -> None:
        self._manual_off = False
        self._pause_until_change = False
        self._wake.set()

    def disconnect(self) -> None:
        self._manual_off = True
        self._close_socket("disconnected by the user")
        self._wake.set()

    def settings_changed(self) -> None:
        self._pause_until_change = False
        self._close_socket("settings changed")
        self._wake.set()

    def _drop(self) -> None:
        """Close the socket as a network failure would (the reconnect logic takes over)."""
        self._close_socket("connection lost")

    # --------------------------------------------------------- supervision --
    def _identity_user(self) -> Optional[str]:
        try:
            from security.policy import current_identity
            ident = current_identity()
            if ident.authenticated and not ident.locked and ident.username and not ident.system:
                return ident.username
        except Exception:
            pass
        return None

    def _blocked(self):
        """(state, reason) when this device must not be connected now, else None."""
        if settings.mode() != "online":
            return OFFLINE, "Offline mode - local processing only"
        user = self._identity_user()
        if not user:
            return OFFLINE, "ZEUS is locked or nobody is signed in - the network reconnects after sign-in"
        try:
            from security.lockdown import lockdown
            if lockdown.engaged:
                return ERROR, "Emergency lockdown is engaged - the ZEUS NETWORK is cut off"
        except Exception:
            pass
        try:
            from security.egress import egress_guard
            if egress_guard.installed and egress_guard.mode == "lockdown":
                return ERROR, "The network policy is LOCKDOWN - no connection leaves this machine"
        except Exception:
            pass
        if store.get("revoked"):
            return ERROR, "This device was revoked - remove the saved key and pair it again to rejoin"
        try:
            endpoint = settings.endpoint()
        except ValueError as error:
            return ERROR, f"The ZEUS server address is not usable: {error}"
        if endpoint is None:
            return ERROR, "No ZEUS server is configured (Settings → ZEUS NETWORK, or CRUDEX_SERVER_URL)"
        try:
            key = keystore.load_api_key(user)
        except keystore.KeystoreError as error:
            return ERROR, str(error)
        if not key:
            return ERROR, "No API key is saved for this account - enter it in the user menu"
        if self._manual_off:
            return OFFLINE, "Disconnected - press Connect to go online"
        if self._pause_until_change:
            return ERROR, self._state["detail"]
        return None

    def _supervise(self) -> None:
        backoff = 1.0
        while self._running:
            blocked = self._blocked()
            if blocked:
                self._withdraw()
                self._set_state(*blocked)
                self._wake.wait(5)
                self._wake.clear()
                continue
            try:
                self._connect_once()
                backoff = 1.0
            except _AuthRefused as error:
                self._pause_until_change = True
                self._set_state(ERROR, str(error))
            except _Revoked as error:
                store.put("revoked", True)
                store.audit("device_revoked_by_peer", str(error))
                self._set_state(ERROR, str(error))
                self._emit({"type": "revoked", "message": str(error)})
            except Exception as error:
                if not self._running:
                    break
                reason, is_error = self._explain(error)
                self._set_state(ERROR if is_error or self._blocked() is not None else CONNECTING,
                                f"{reason} - retrying in {int(backoff)} s")
                self._wake.wait(backoff + random.uniform(0, backoff / 2))
                self._wake.clear()
                backoff = min(60.0, backoff * 2)
            finally:
                self._close_socket(None)
        self._withdraw()

    def _explain(self, error: Exception):
        """(reason for the user, is it a configuration ERROR rather than a transient drop)."""
        import socket
        import ssl
        text = str(error)
        if isinstance(error, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
            return (f"The server's TLS certificate is not trusted ({getattr(error, 'verify_message', '') or 'verify failed'})"
                    " - check the address, or set the CA file for a self-hosted server"), True
        if getattr(error, "winerror", None) == 10013 or "10013" in text:
            return ("Windows Firewall blocks this program from the internet (ZEUS Sovereign Block rules) - an "
                    "administrator can allow the server: FIREWALL_CRUDEX.bat apply after setting its address"), True
        if type(error).__name__ == "EgressBlocked":
            return f"Blocked by ZEUS's network policy: {text}", True
        if isinstance(error, socket.gaierror):
            return "The server name could not be resolved - no internet, or the address is wrong", True
        if isinstance(error, NetError) and error.code == "protocol":
            return text, True
        if isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in text.lower():
            return "The server did not answer in time", False
        if isinstance(error, ConnectionRefusedError):
            return "The server refused the connection - it may not be running", False
        if isinstance(error, NetError):
            return f"Connection lost: {text}", False
        return f"Connection failed: {type(error).__name__}: {text[:160]}", False

    def _withdraw(self) -> None:
        if self._approved_host:
            try:
                from security.egress import egress_guard
                egress_guard.withdraw_destination(self._approved_host, "ZEUS NETWORK offline")
            except Exception:
                pass
            self._approved_host = None

    def _close_socket(self, reason: Optional[str]) -> None:
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                # shutdown, not close(): close() would WRITE a close frame from this
                # thread while the I/O thread may be reading - one TLS connection
                # must only ever be used by the thread that owns it.
                ws.shutdown()
            except Exception:
                pass
            for future in list(self._replies.values()):
                if not future.done():
                    future.set_exception(NetError("the connection closed", "closed"))
            self._replies.clear()
            with self._up_cond:
                self._up_cond.notify_all()
            if self._download:
                try:
                    self._download["fh"].close()
                except Exception:
                    pass
                self._download = None
            self._connected_at = None
            if reason:
                logger.info(f"ZEUS NETWORK connection closed: {reason}")

    # ------------------------------------------------------------ one session --
    def _connect_once(self) -> None:
        import websocket
        endpoint = settings.endpoint()
        user = self._identity_user()
        self._set_state(CONNECTING, f"Connecting to {endpoint['host']}")
        try:
            from security.egress import egress_guard
            egress_guard.approve_destination(endpoint["host"], int(endpoint["port"]),
                                             "ZEUS NETWORK server (Online mode)")
            self._approved_host = str(endpoint["host"]).lower()
        except Exception as error:
            logger.warning(f"egress approval failed: {error}")
        from core import branding
        ws = websocket.WebSocket(sslopt={"context": settings.ssl_context()}, enable_multithread=True)
        ws.connect(endpoint["url"], timeout=15, header=[f"User-Agent: ZEUS/{branding.APP_VERSION}"],
                   suppress_origin=True)
        ws.settimeout(20)
        hello = json.loads(ws.recv())
        if hello.get("t") != "hello" or hello.get("protocol") != P.PROTOCOL_VERSION:
            ws.close()
            raise NetError("the server does not speak the ZEUS protocol version this device uses", "protocol")
        import base64
        nonce = base64.b64decode(hello["nonce"])
        me = identity.describe()
        api_key = keystore.load_api_key(user)
        frame = json.dumps({"t": "auth", "api_key": api_key, "device": me,
                            "sig": identity.sign(P.AUTH_CONTEXT + nonce + me["device_id"].encode())})
        api_key = None
        ws.send(frame)
        frame = None
        answer = json.loads(ws.recv())
        if answer.get("t") == "error":
            ws.close()
            if answer.get("code") == "revoked":
                raise _Revoked(answer.get("message") or "This device was revoked")
            if answer.get("code") in ("auth_failed", "rate_limited"):
                store.audit("auth_refused", answer.get("code"))
                raise _AuthRefused(answer.get("message") or "The server refused the API key")
            raise NetError(answer.get("message") or "the server refused the connection", answer.get("code", "error"))
        if answer.get("t") != "welcome":
            ws.close()
            raise NetError("unexpected answer from the server", "protocol")
        with self._lock:
            self._ws = ws
            self._outq = queue.Queue(maxsize=64)
            self._io_thread = threading.current_thread()
            self._account = {"name": answer["account"]["name"], "id": answer["account"]["id"],
                             "server": endpoint["host"]}
        self._connected_at = _now()
        store.put("account_name", answer["account"]["name"])
        store.audit("connected", {"server": endpoint["host"], "registered": answer.get("registered")})
        self._set_state(ONLINE, f"Connected to {endpoint['host']} as {answer['account']['name']}")
        self._emit({"type": "connected", "registered": answer.get("registered")})
        pump = threading.Thread(target=self._pump, args=(ws,), name="crudex-net-pump", daemon=True)
        pump.start()
        self._io(ws)

    def _io(self, ws) -> None:
        """The ONLY thread that touches the socket. OpenSSL does not support one
        TLS connection being read and written by two threads at once (it breaks
        the stream under load), so every other thread queues its frames and
        this loop writes them between short reads."""
        import websocket
        silent_since = time.monotonic()
        outq = self._outq
        while self._running and self._ws is ws:
            try:
                while True:
                    try:
                        kind, payload = outq.get_nowait()
                    except queue.Empty:
                        break
                    ws.settimeout(60)                  # a write must never time out half-way
                    if kind == "text":
                        ws.send(payload)
                    else:
                        ws.send_binary(payload)
                ws.settimeout(0.02 if not outq.empty() else 0.15)
                opcode, data = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                if time.monotonic() - silent_since > 70:
                    raise NetError("the server stopped answering", "timeout")
                continue
            except (websocket.WebSocketConnectionClosedException, OSError) as error:
                if self._ws is not ws:
                    return
                raise NetError(f"the connection dropped ({type(error).__name__})", "closed")
            silent_since = time.monotonic()
            if opcode == websocket.ABNF.OPCODE_BINARY:
                self._on_chunk(ws, data)
            elif opcode == websocket.ABNF.OPCODE_TEXT:
                try:
                    message = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                self._dispatch(ws, message)
            elif opcode == websocket.ABNF.OPCODE_CLOSE:
                raise NetError("the server closed the connection", "closed")

    # -------------------------------------------------------------- sending --
    def _queue_frame(self, ws, kind: str, payload) -> None:
        if self._ws is not ws:
            raise NetError("the connection closed", "closed")
        if threading.current_thread() is self._io_thread:
            ws.settimeout(60)                          # already on the I/O thread: write directly
            (ws.send if kind == "text" else ws.send_binary)(payload)
            return
        while True:                                    # bounded queue = back-pressure for uploads
            try:
                self._outq.put((kind, payload), timeout=1)
                return
            except queue.Full:
                if self._ws is not ws:
                    raise NetError("the connection closed", "closed")

    def _send(self, ws, message: Dict[str, Any]) -> None:
        self._queue_frame(ws, "text", json.dumps(message, separators=(",", ":")))

    def _send_binary(self, ws, data: bytes) -> None:
        self._queue_frame(ws, "binary", data)

    def request(self, message: Dict[str, Any], timeout: float = 30) -> Dict[str, Any]:
        with self._lock:
            ws = self._ws
        if ws is None:
            raise NetError("This device is not connected to the ZEUS NETWORK", "offline")
        rid = uuid.uuid4().hex
        future: Future = Future()
        self._replies[rid] = future
        try:
            self._send(ws, {**message, "rid": rid})
            reply = future.result(timeout=timeout)
        except NetError:
            raise
        except Exception as error:
            raise NetError(f"no answer from the server ({type(error).__name__})", "timeout")
        finally:
            self._replies.pop(rid, None)
        if reply.get("t") == "error":
            raise NetError(reply.get("message") or "refused", reply.get("code", "error"))
        return reply

    # ---------------------------------------------------------- dispatching --
    def _dispatch(self, ws, message: Dict[str, Any]) -> None:
        kind = message.get("t")
        rid = message.get("rid")
        if rid and rid in self._replies:
            future = self._replies.get(rid)
            if future and not future.done():
                future.set_result(message)
            return
        if kind == "deliver":
            self._on_deliver(ws, message)
        elif kind == "up_ack":
            with self._up_cond:
                self._up_acked[message["transfer_id"]] = int(message["offset"])
                self._up_cond.notify_all()
        elif kind == "upload_resync":
            with self._up_cond:
                self._up_resync[message["transfer_id"]] = int(message["offset"])
                self._up_cond.notify_all()
        elif kind == "down_window":
            self._on_window(ws, message)
        elif kind == "presence":
            self._update_device(message["device_id"], {"online": message.get("online"),
                                                       "last_seen": message.get("last_seen")})
            self._emit({"type": "presence", "device_id": message["device_id"], "online": message.get("online")})
        elif kind in ("paired", "unpaired", "device_revoked", "grants", "device_changed"):
            store.audit(kind, {k: v for k, v in message.items() if k != "t"})
            self._emit({"type": "devices", "event": kind, **{k: v for k, v in message.items() if k != "t"}})
            threading.Thread(target=self._refresh_devices_quietly, daemon=True).start()
        elif kind == "revoked":
            store.put("revoked", True)
            store.audit("device_revoked_by_peer", message.get("by"))
            self._emit({"type": "revoked", "message": message.get("message")})
        elif kind == "transfer_cancelled":
            self._transfer_update(message["transfer_id"], state="cancelled", error="cancelled by the other device")
        elif kind == "error":
            if message.get("code") == "revoked":
                store.put("revoked", True)
            elif message.get("code") == "integrity" and self._download:
                logger.warning("download integrity error from server")
            logger.info(f"server error: {message.get('code')}: {message.get('message')}")

    def _on_deliver(self, ws, message: Dict[str, Any]) -> None:
        sender, msg_id, kind = message.get("from"), message.get("id"), message.get("kind")
        body = message.get("body") or {}
        origin = sender or "server"            # receipts are generated by the server itself
        seen = store.one("SELECT 1 FROM received WHERE from_device = ? AND msg_id = ?", (origin, msg_id))
        if not seen:
            try:
                self._apply(sender, message.get("from_name"), msg_id, kind, body, message)
            except Exception:
                logger.exception(f"could not apply a {kind} delivery - it will be delivered again")
                return                         # not acknowledged: the server re-delivers it
            store.x("INSERT OR IGNORE INTO received (from_device, msg_id, kind, seq, received_at) VALUES (?,?,?,?,?)",
                    (origin, msg_id, kind, message.get("seq"), _now()))
        try:
            self._send(ws, {"t": "ack", "seq": message["seq"]})
        except Exception:
            pass
        self._mark_synced()

    def _apply(self, sender, sender_name, msg_id, kind, body, message) -> None:
        if kind in ("message", "notification"):
            text = str(body.get("text", ""))[:20000]
            store.x("INSERT OR IGNORE INTO messages (msg_id, peer, direction, kind, text, created_at, state) "
                    "VALUES (?,?,?,?,?,?,?)", (msg_id, sender, "in", kind, text, message.get("created_at") or _now(),
                                               "received"))
            self._emit({"type": "message", "direction": "in", "kind": kind, "peer": sender, "peer_name": sender_name,
                        "text": text, "msg_id": msg_id})
        elif kind == "receipt":
            self._on_receipt(body)
        elif kind == "file":
            self._on_file_offer(sender, sender_name, body)
        elif kind == "task":
            from crudex_net import tasks
            tasks.on_task(self, sender, sender_name, body)
        elif kind in ("task_status", "task_cancel"):
            from crudex_net import tasks
            (tasks.on_status if kind == "task_status" else tasks.on_cancel)(self, sender, body)
        elif kind in ("share", "knowledge"):
            from crudex_net import items
            items.on_item(self, sender, sender_name, msg_id, kind, body)
        elif kind in ("sync_manifest", "sync_request"):
            from crudex_net import folders
            (folders.on_manifest if kind == "sync_manifest" else folders.on_request)(self, sender, body)
        else:
            logger.info(f"ignored a delivery of unknown kind {kind}")

    def _on_receipt(self, body: Dict[str, Any]) -> None:
        msg_id = str(body.get("id", ""))
        if body.get("transfer_id"):
            self._transfer_update(body["transfer_id"], state="delivered" if body.get("status") == "delivered"
                                  else body.get("status", "delivered"))
            return
        store.x("UPDATE outbox SET state = 'delivered', delivered_at = ? WHERE msg_id = ?", (_now(), msg_id))
        store.x("UPDATE messages SET state = 'delivered' WHERE msg_id = ?", (msg_id,))
        self._emit({"type": "receipt", "msg_id": msg_id, "status": body.get("status")})

    # ----------------------------------------------------------- devices ----
    def _update_device(self, device_id: str, changes: Dict[str, Any]) -> None:
        row = store.one("SELECT record FROM devices WHERE device_id = ?", (device_id,))
        record = store.loads(row["record"], {}) if row else {"device_id": device_id}
        record.update(changes)
        store.x("INSERT INTO devices (device_id, record, updated_at) VALUES (?,?,?) ON CONFLICT (device_id) DO UPDATE "
                "SET record = excluded.record, updated_at = excluded.updated_at",
                (device_id, json.dumps(record), _now()))

    def refresh_devices(self) -> List[Dict[str, Any]]:
        reply = self.request({"t": "devices"})
        with store.connect() as conn:
            conn.execute("DELETE FROM devices")
            for record in reply["devices"]:
                conn.execute("INSERT INTO devices (device_id, record, updated_at) VALUES (?,?,?)",
                             (record["device_id"], json.dumps(record), _now()))
        return reply["devices"]

    def _refresh_devices_quietly(self) -> None:
        try:
            self.refresh_devices()
            self._emit({"type": "devices", "event": "refreshed"})
        except Exception:
            pass

    def connected(self) -> bool:
        with self._lock:
            return self._ws is not None

    def cached_devices(self) -> List[Dict[str, Any]]:
        rows = [store.loads(r["record"], {}) for r in store.q("SELECT record FROM devices")]
        live = self.connected()
        for row in rows:
            if not live:
                row["online"] = None          # never show stale presence as live
        return rows

    def my_grants_to(self, device_id: str) -> List[str]:
        row = store.one("SELECT record FROM devices WHERE device_id = ?", (device_id,))
        return list(store.loads(row["record"], {}).get("grants_given", [])) if row else []

    def peer_name(self, device_id: str) -> str:
        row = store.one("SELECT record FROM devices WHERE device_id = ?", (device_id,))
        return (store.loads(row["record"], {}).get("name") if row else None) or device_id

    # ------------------------------------------------------------- outbox ----
    def queue_event(self, to_device: str, kind: str, body: Dict[str, Any], msg_id: Optional[str] = None) -> str:
        msg_id = msg_id or f"{kind[:4]}-{uuid.uuid4().hex}"
        encoded = json.dumps(body, default=str)
        if len(encoded) > P.MAX_BODY_BYTES:
            raise ValueError("too large to send as an event - send it as a file")
        store.x("INSERT OR IGNORE INTO outbox (msg_id, to_device, kind, body, created_at) VALUES (?,?,?,?,?)",
                (msg_id, to_device, kind, encoded, _now()))
        self._work.set()
        self._mark_synced()
        return msg_id

    def _flush_outbox(self, ws) -> None:
        for row in store.q("SELECT * FROM outbox WHERE state = 'pending' ORDER BY created_at"):
            if self._ws is not ws:
                return
            store.x("UPDATE outbox SET attempts = attempts + 1 WHERE msg_id = ?", (row["msg_id"],))
            try:
                reply = self.request({"t": "send", "id": row["msg_id"], "to": row["to_device"], "kind": row["kind"],
                                      "body": json.loads(row["body"])})
            except NetError as error:
                if error.code in _FATAL_SEND:
                    store.x("UPDATE outbox SET state = 'failed', error = ? WHERE msg_id = ?", (str(error), row["msg_id"]))
                    store.x("UPDATE messages SET state = 'failed' WHERE msg_id = ?", (row["msg_id"],))
                    if row["kind"] == "task":
                        from crudex_net import tasks
                        tasks.mark_local(json.loads(row["body"]).get("task_id"), "FAILED", error=f"not sent: {error}")
                    self._emit({"type": "send_failed", "msg_id": row["msg_id"], "error": str(error)})
                    continue
                raise
            store.x("UPDATE outbox SET state = 'sent', seq = ?, sent_at = ? WHERE msg_id = ? AND state = 'pending'",
                    (reply.get("seq"), _now(), row["msg_id"]))
            store.x("UPDATE messages SET state = 'sent' WHERE msg_id = ? AND state = 'pending'", (row["msg_id"],))
            self._emit({"type": "sent", "msg_id": row["msg_id"], "kind": row["kind"]})

    # --------------------------------------------------------------- pump ----
    def _pump(self, ws) -> None:
        last_ping = time.monotonic()
        try:
            self.refresh_devices()
            self._emit({"type": "devices", "event": "refreshed"})
        except Exception:
            pass
        from crudex_net import folders
        while self._running and self._ws is ws:
            try:
                self._flush_outbox(ws)
                self._resume_downloads(ws)
                upload = store.one("SELECT * FROM transfers WHERE direction = 'out' AND state IN ('queued', "
                                   "'uploading') ORDER BY created_at LIMIT 1")
                if upload:
                    self._upload(ws, upload)
                    continue
                folders.tick(self)
                self._mark_synced()
            except NetError as error:
                if self._ws is not ws:
                    return
                logger.info(f"pump: {error}")
            except Exception:
                if self._ws is not ws:
                    return
                logger.exception("pump failed")
            if time.monotonic() - last_ping > 20:
                try:
                    self._send(ws, {"t": "ping"})
                except Exception:
                    return
                last_ping = time.monotonic()
            self._work.wait(2)
            self._work.clear()

    # ------------------------------------------------------------ uploads ----
    def queue_file(self, to_device: str, path: Path, file_kind: str = "file", meta: Dict[str, Any] = None,
                   name: Optional[str] = None) -> Dict[str, Any]:
        size = path.stat().st_size
        sha = _sha256_file(path)
        transfer_id = str(uuid.uuid4())
        now = _now()
        store.x("INSERT INTO transfers (transfer_id, peer, direction, name, source_path, size, sha256, mime, "
                "file_kind, meta, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (transfer_id, to_device, "out", name or path.name, str(path), size, sha,
                 _guess_mime(path.name), file_kind, json.dumps(meta or {}), "queued", now, now))
        store.audit("file_queued", {"transfer_id": transfer_id, "to": to_device, "name": name or path.name,
                                    "size": size})
        self._work.set()
        self._mark_synced()
        return {"transfer_id": transfer_id, "size": size, "sha256": sha, "state": "queued"}

    def _transfer_update(self, transfer_id: str, **fields) -> None:
        fields["updated_at"] = _now()
        names = ", ".join(f"{k} = ?" for k in fields)
        store.x(f"UPDATE transfers SET {names} WHERE transfer_id = ?", (*fields.values(), transfer_id))
        row = store.one("SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,))
        if row:
            self._emit({"type": "transfer", **_transfer_view(row)})

    def _progress(self, transfer_id: str, done: int, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_progress > 0.5:
            self._last_progress = now
            self._transfer_update(transfer_id, bytes_done=done)

    def _upload(self, ws, row: Dict[str, Any]) -> None:
        transfer_id = row["transfer_id"]
        source = Path(row["source_path"] or "")
        if not source.is_file() or source.stat().st_size != row["size"]:
            self._transfer_update(transfer_id, state="failed", error="the file changed or was removed after it was queued")
            return
        self._transfer_update(transfer_id, state="uploading")
        try:
            reply = self.request({"t": "offer", "transfer_id": transfer_id, "to": row["peer"], "name": row["name"],
                                  "size": row["size"], "sha256": row["sha256"], "mime": row["mime"],
                                  "kind": row["file_kind"], "meta": store.loads(row["meta"], {})})
        except NetError as error:
            if error.code in _FATAL_SEND | {"conflict", "no_space"}:
                self._transfer_update(transfer_id, state="failed", error=str(error))
                return
            raise
        if reply.get("state") in ("stored", "delivered"):
            self._transfer_update(transfer_id, state="uploaded", bytes_done=row["size"])
            return
        if reply.get("state") in ("failed", "cancelled"):
            self._transfer_update(transfer_id, state=reply["state"], error="the server discarded this transfer")
            return
        offset = int(reply.get("offset", 0))
        with self._up_cond:
            self._up_acked[transfer_id] = offset
            self._up_resync.pop(transfer_id, None)
        self._transfer_update(transfer_id, bytes_done=offset)
        # A resumed upload re-checks the local file: the bytes already sent must be
        # the same bytes (the server re-hashes what it holds when it completes).
        with source.open("rb") as handle:
            handle.seek(offset)
            while offset < row["size"]:
                with self._up_cond:
                    while (self._ws is ws and transfer_id not in self._up_resync
                           and offset - self._up_acked.get(transfer_id, 0) >= P.UPLOAD_WINDOW):
                        if not self._up_cond.wait(60):
                            raise NetError("the server stopped acknowledging the upload", "timeout")
                    if self._ws is not ws:
                        raise NetError("the connection dropped during the upload", "closed")
                    if transfer_id in self._up_resync:
                        offset = self._up_resync.pop(transfer_id)
                        handle.seek(offset)
                data = handle.read(P.CHUNK_SIZE)
                if not data:
                    break
                self._send_binary(ws, P.pack_chunk(transfer_id, offset, data))
                offset += len(data)
                self._progress(transfer_id, offset)
        with self._up_cond:
            while self._ws is ws and self._up_acked.get(transfer_id, 0) < row["size"]:
                if not self._up_cond.wait(60):
                    raise NetError("the server did not confirm the last bytes", "timeout")
        try:
            reply = self.request({"t": "upload_done", "transfer_id": transfer_id}, timeout=120)
        except NetError as error:
            if error.code == "integrity":
                self._transfer_update(transfer_id, state="failed", error=str(error))
                return
            raise
        self._transfer_update(transfer_id, state="uploaded", bytes_done=row["size"])
        store.audit("file_uploaded", {"transfer_id": transfer_id, "state": reply.get("state")})

    def cancel_transfer(self, transfer_id: str) -> Dict[str, Any]:
        row = store.one("SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,))
        if not row:
            raise ValueError("no such transfer")
        if row["state"] in ("received", "delivered", "cancelled", "failed"):
            return {"ok": True, "state": row["state"]}
        if self.connected():
            try:
                self.request({"t": "cancel_transfer", "transfer_id": transfer_id})
            except NetError as error:
                if error.code != "unknown_transfer":
                    raise
        self._transfer_update(transfer_id, state="cancelled")
        return {"ok": True, "state": "cancelled"}

    # ---------------------------------------------------------- downloads ----
    def _on_file_offer(self, sender: str, sender_name: str, body: Dict[str, Any]) -> None:
        transfer_id = body["transfer_id"]
        if store.one("SELECT 1 FROM transfers WHERE transfer_id = ?", (transfer_id,)):
            return
        now = _now()
        part_dir = settings.received_folder() / ".incoming"
        part_dir.mkdir(parents=True, exist_ok=True)
        store.x("INSERT INTO transfers (transfer_id, peer, direction, name, part_path, size, sha256, mime, file_kind, "
                "meta, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (transfer_id, sender, "in", _safe_name(body.get("name")), str(part_dir / f"{transfer_id}.part"),
                 int(body["size"]), str(body["sha256"]), body.get("mime"), body.get("file_kind", "file"),
                 json.dumps(body.get("meta") or {}), "offered", now, now))
        store.audit("file_offered", {"transfer_id": transfer_id, "from": sender, "name": body.get("name"),
                                     "size": body.get("size")})
        self._emit({"type": "transfer", "transfer_id": transfer_id, "direction": "in", "state": "offered",
                    "name": body.get("name"), "peer": sender, "peer_name": sender_name})
        self._work.set()

    def _resume_downloads(self, ws) -> None:
        with self._lock:
            if self._download is not None:
                return
            self._start_download(ws)

    def _start_download(self, ws) -> None:
        row = store.one("SELECT * FROM transfers WHERE direction = 'in' AND state IN ('offered', 'downloading') "
                        "ORDER BY created_at LIMIT 1")
        if not row:
            return
        if P.RECEIVE_FILES not in self.my_grants_to(row["peer"]) and self.my_grants_to(row["peer"]):
            self._transfer_update(row["transfer_id"], state="failed", error="RECEIVE_FILES is not granted to the sender")
            return
        part = Path(row["part_path"])
        part.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        offset = 0
        if part.exists():
            with part.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                    offset += len(block)
        if offset > row["size"]:
            part.unlink()
            digest, offset = hashlib.sha256(), 0
        self._download = {"transfer_id": row["transfer_id"], "fh": part.open("ab"), "hash": digest, "offset": offset,
                          "size": row["size"], "sha256": row["sha256"], "part": part, "row": row}
        self._transfer_update(row["transfer_id"], state="downloading", bytes_done=offset)
        if offset >= row["size"]:
            self._finish_download(ws)
        else:
            self._send(ws, {"t": "download", "transfer_id": row["transfer_id"], "offset": offset})

    def _on_chunk(self, ws, frame: bytes) -> None:
        download = self._download
        try:
            transfer_id, offset, data = P.unpack_chunk(frame)
        except ValueError:
            return
        if download is None or transfer_id != download["transfer_id"] or offset != download["offset"]:
            return                            # stale window after a reconnect: ignored, re-requested below
        download["fh"].write(data)
        download["hash"].update(data)
        download["offset"] += len(data)
        self._progress(transfer_id, download["offset"])

    def _on_window(self, ws, message: Dict[str, Any]) -> None:
        download = self._download
        if download is None or message.get("transfer_id") != download["transfer_id"]:
            return
        download["fh"].flush()
        if download["offset"] >= download["size"]:
            self._finish_download(ws)
        else:
            self._send(ws, {"t": "download", "transfer_id": download["transfer_id"], "offset": download["offset"]})

    def _finish_download(self, ws) -> None:
        with self._lock:
            download, self._download = self._download, None
        if download is None:
            return
        download["fh"].close()
        row = download["row"]
        transfer_id = row["transfer_id"]
        if download["hash"].hexdigest() != row["sha256"]:
            download["part"].unlink(missing_ok=True)
            self._transfer_update(transfer_id, state="offered", bytes_done=0,
                                  error="SHA-256 mismatch - the file is downloaded again")
            self._work.set()
            return
        meta = store.loads(row["meta"], {})
        final = None
        conflict = 0
        if row["file_kind"] == "sync_file":
            from crudex_net import folders
            final, conflict = folders.place_file(self, row["peer"], meta, download["part"], row["sha256"])
        if final is None:
            folder = settings.received_folder() / _safe_name(self.peer_name(row["peer"]))
            folder.mkdir(parents=True, exist_ok=True)
            final = _unique(folder / row["name"])
            shutil.move(str(download["part"]), str(final))
        self._transfer_update(transfer_id, state="received", bytes_done=row["size"], final_path=str(final),
                              conflict=conflict)
        store.audit("file_received", {"transfer_id": transfer_id, "from": row["peer"], "sha256": row["sha256"],
                                      "path": str(final)})
        try:
            self._send(ws, {"t": "download_done", "transfer_id": transfer_id, "sha256": row["sha256"]})
        except Exception:
            pass
        if row["file_kind"] in ("project_bundle", "artifact", "document", "file"):
            from crudex_net import items
            items.on_file(self, row, final)
        self._work.set()


def _transfer_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"transfer_id": row["transfer_id"], "peer": row["peer"], "direction": row["direction"],
            "name": row["name"], "size": row["size"], "bytes_done": row["bytes_done"], "state": row["state"],
            "error": row.get("error"), "final_path": row.get("final_path"), "file_kind": row["file_kind"],
            "conflict": row.get("conflict")}


def _guess_mime(name: str) -> str:
    import mimetypes
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _safe_name(name: Any) -> str:
    import re
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", Path(str(name or "file")).name).strip(" .")
    return (text or "file")[:150]


def _unique(path: Path) -> Path:
    """Never overwrite a file that is already there - a second copy gets a number."""
    if not path.exists():
        return path
    for n in range(2, 10000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}")


client = NetworkClient()
