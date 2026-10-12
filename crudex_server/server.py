"""
The ZEUS NETWORK coordination server.

One Qt WebSocket server (QtWebSockets, part of PySide6 - RFC 6455, TLS through
Qt's OpenSSL backend). Every device keeps ONE outbound wss:// connection to it;
the server authenticates the device, enforces pairings and permissions, relays
messages, tasks and file chunks, and stores-and-forwards for devices that are
offline. It never executes anything it relays.

Run it with TLS (a certificate for its public name), or on loopback only behind
a TLS-terminating reverse proxy on the same host (Caddy / nginx) - never as
plain ws:// on a network address: API keys would cross the network in clear.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import itertools
import json
import logging
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from crudex_server import protocol as P
from crudex_server.store import ServerStore

logger = logging.getLogger("crudex.server")

_DEVICE_ID = re.compile(r"^dev_[a-z0-9]{16,40}$")
_SAFE_NAME = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")
_ids = itertools.count(1)


def _clean_name(name: str, limit: int = 200) -> str:
    name = _SAFE_NAME.sub("_", Path(str(name or "")).name).strip(" .")
    return (name or "file")[:limit]


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Connection:
    def __init__(self, server: "CrudexServer", sock):
        self.server, self.sock = server, sock
        self.id = next(_ids)
        self.nonce = secrets.token_bytes(32)
        self.account_id: Optional[str] = None
        self.device_id: Optional[str] = None
        self.device_name = ""
        self.address = server.address_of(sock)
        self.opened = time.monotonic()
        self.last_activity = time.monotonic()
        self.uploads: Dict[str, Dict[str, Any]] = {}
        self.inflight: set = set()
        self.closed = False

    def send(self, message: Dict[str, Any]) -> None:
        if not self.closed:
            self.sock.sendTextMessage(json.dumps(message, separators=(",", ":"), default=str))

    def send_binary(self, data: bytes) -> None:
        if not self.closed:
            from PySide6.QtCore import QByteArray
            self.sock.sendBinaryMessage(QByteArray(data))

    def close(self, reason: str = "", policy: bool = False) -> None:
        if self.closed:
            return
        from PySide6.QtWebSockets import QWebSocketProtocol
        code = (QWebSocketProtocol.CloseCode.CloseCodePolicyViolated if policy
                else QWebSocketProtocol.CloseCode.CloseCodeNormal)
        self.sock.close(code, reason[:120])

    def release_uploads(self) -> None:
        for state in self.uploads.values():
            try:
                state["fh"].close()
            except Exception:
                pass
        self.uploads.clear()


class CrudexServer:
    def __init__(self, data_dir: Path, host: str = "127.0.0.1", port: int = 8765, cert: str = None,
                 key: str = None, behind_proxy: bool = False, max_file_bytes: int = 20 * 1024 ** 3):
        self.store = ServerStore(Path(data_dir))
        self.host, self.port, self.cert, self.key = host, port, cert, key
        self.behind_proxy = behind_proxy
        self.max_file_bytes = max_file_bytes
        self.connections: Dict[int, Connection] = {}
        self.online: Dict[str, Connection] = {}
        self._auth_failures: Dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._pair_failures: Dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.ws = None
        self._timers = []

    # --------------------------------------------------------------- start ----
    def start(self) -> int:
        from PySide6.QtCore import QByteArray, QTimer
        from PySide6.QtNetwork import QHostAddress, QSsl, QSslCertificate, QSslConfiguration, QSslKey, QSslSocket
        from PySide6.QtWebSockets import QWebSocketServer
        loopback = self.host in ("127.0.0.1", "::1", "localhost")
        if not self.cert:
            if not (self.behind_proxy and loopback):
                raise SystemExit("Refusing to serve without TLS: give --cert/--key, or --behind-proxy on 127.0.0.1 "
                                 "with a TLS-terminating reverse proxy on the same host.")
            mode = QWebSocketServer.SslMode.NonSecureMode
        else:
            mode = QWebSocketServer.SslMode.SecureMode
        self.ws = QWebSocketServer("ZEUS", mode)
        if self.cert:
            conf = QSslConfiguration.defaultConfiguration()
            chain = QSslCertificate.fromPath(self.cert)
            if not chain:
                raise SystemExit(f"no certificate in {self.cert}")
            key_bytes = QByteArray(Path(self.key).read_bytes())
            private = QSslKey(key_bytes, QSsl.KeyAlgorithm.Ec, QSsl.EncodingFormat.Pem)
            if private.isNull():
                private = QSslKey(key_bytes, QSsl.KeyAlgorithm.Rsa, QSsl.EncodingFormat.Pem)
            if private.isNull():
                raise SystemExit(f"could not read the private key {self.key}")
            conf.setLocalCertificateChain(chain)
            conf.setPrivateKey(private)
            conf.setPeerVerifyMode(QSslSocket.PeerVerifyMode.VerifyNone)
            conf.setProtocol(QSsl.SslProtocol.TlsV1_2OrLater)
            self.ws.setSslConfiguration(conf)
        address = QHostAddress(self.host if self.host != "localhost" else "127.0.0.1")
        if not self.ws.listen(address, self.port):
            raise SystemExit(f"could not listen on {self.host}:{self.port}: {self.ws.errorString()}")
        self.port = self.ws.serverPort()
        self.ws.newConnection.connect(self._on_new)
        self.ws.sslErrors.connect(lambda errors: logger.info(f"TLS handshake problem: {errors}"))
        for interval, fn in ((15_000, self._sweep), (3_600_000, self._housekeeping)):
            timer = QTimer()
            timer.timeout.connect(fn)
            timer.start(interval)
            self._timers.append(timer)
        logger.info(f"ZEUS server listening on {self.host}:{self.port} ({'TLS' if self.cert else 'behind proxy'})")
        return self.port

    def stop(self) -> None:
        for conn in list(self.connections.values()):
            conn.close("server stopping")
        if self.ws:
            self.ws.close()

    def address_of(self, sock) -> str:
        if self.behind_proxy:
            try:
                forwarded = bytes(sock.request().rawHeader(b"X-Forwarded-For")).decode("ascii", "ignore")
                if forwarded:
                    return forwarded.split(",")[0].strip()[:64]
            except Exception:
                pass
        return sock.peerAddress().toString()[:64]

    # --------------------------------------------------------- connections ----
    def _on_new(self):
        while self.ws.hasPendingConnections():
            sock = self.ws.nextPendingConnection()
            sock.setMaxAllowedIncomingMessageSize(P.MAX_TEXT_FRAME * 2)
            sock.setMaxAllowedIncomingFrameSize(P.MAX_TEXT_FRAME * 2)
            conn = Connection(self, sock)
            self.connections[conn.id] = conn
            sock.textMessageReceived.connect(lambda text, c=conn: self._guard(c, self._on_text, text))
            sock.binaryMessageReceived.connect(lambda data, c=conn: self._guard(c, self._on_binary, bytes(data)))
            sock.disconnected.connect(lambda c=conn: self._on_close(c))
            conn.send({"t": "hello", "nonce": base64.b64encode(conn.nonce).decode(), "protocol": P.PROTOCOL_VERSION,
                       "server": "crudex-server/1"})

    def _guard(self, conn: Connection, fn, payload):
        conn.last_activity = time.monotonic()
        try:
            fn(conn, payload)
        except ProtocolError as error:
            conn.send({"t": "error", "code": error.code, "message": str(error)})
        except Exception as error:                       # never let one bad frame stop the server
            logger.exception("frame handling failed")
            conn.send({"t": "error", "code": "server_error", "message": type(error).__name__})

    def _on_close(self, conn: Connection):
        conn.closed = True
        conn.release_uploads()
        self.connections.pop(conn.id, None)
        if conn.device_id and self.online.get(conn.device_id) is conn:
            del self.online[conn.device_id]
            self.store.touch_device(conn.device_id)
            self._presence(conn.device_id, False)
        try:
            conn.sock.deleteLater()
        except Exception:
            pass

    def _sweep(self):
        now = time.monotonic()
        for conn in list(self.connections.values()):
            if conn.device_id is None and now - conn.opened > 20:
                conn.close("authentication timeout", policy=True)
            elif now - conn.last_activity > 90:
                conn.close("idle")

    def _housekeeping(self):
        try:
            result = self.store.housekeeping()
            if any(result.values()):
                logger.info(f"housekeeping: {result}")
        except Exception:
            logger.exception("housekeeping failed")

    # -------------------------------------------------------------- frames ----
    def _on_text(self, conn: Connection, text: str):
        if len(text) > P.MAX_TEXT_FRAME:
            raise ProtocolError("too_large", "frame too large")
        try:
            message = json.loads(text)
        except ValueError:
            raise ProtocolError("bad_json", "not JSON")
        if not isinstance(message, dict) or not isinstance(message.get("t"), str):
            raise ProtocolError("bad_frame", "a frame needs a type")
        kind = message["t"]
        if kind == "auth":
            return self._auth(conn, message)
        if conn.device_id is None:
            conn.close("authenticate first", policy=True)
            return
        handler = getattr(self, f"_h_{kind}", None)
        if handler is None:
            raise ProtocolError("unknown_type", f"unknown frame type '{kind[:40]}'")
        try:
            reply = handler(conn, message)
        except ProtocolError as error:
            conn.send({"t": "error", "rid": message.get("rid"), "code": error.code, "message": str(error)})
            return
        if reply is not None:
            reply.setdefault("rid", message.get("rid"))
            conn.send(reply)

    # ----------------------------------------------------------------- auth ----
    def _limited(self, bucket: Dict[str, collections.deque], key: str, limit: int, window: float = 600) -> bool:
        entries = bucket[key]
        now = time.time()
        while entries and now - entries[0] > window:
            entries.popleft()
        return len(entries) >= limit

    def _auth(self, conn: Connection, message: Dict[str, Any]):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        if conn.device_id is not None:
            raise ProtocolError("already_authenticated", "already authenticated")
        if self._limited(self._auth_failures, conn.address, P.AUTH_MAX_FAILURES):
            conn.send({"t": "error", "code": "rate_limited", "message": "too many failed sign-ins - wait 10 minutes"})
            conn.close("rate limited", policy=True)
            return

        def fail(code: str, text: str, audit_detail: str = ""):
            self._auth_failures[conn.address].append(time.time())
            self.store.audit("auth_failed", None, None, audit_detail or code, conn.address)
            conn.send({"t": "error", "code": code, "message": text})
            conn.close(code, policy=True)

        identity = message.get("device") if isinstance(message.get("device"), dict) else {}
        device_id = str(identity.get("device_id", ""))
        name = str(identity.get("name", "")).strip()
        try:
            public = base64.b64decode(str(identity.get("public_key", "")), validate=True)
            signature = base64.b64decode(str(message.get("sig", "")), validate=True)
        except (ValueError, TypeError):
            return fail("auth_failed", "authentication failed", "undecodable identity")
        if not _DEVICE_ID.match(device_id) or not name or len(public) != 32:
            return fail("auth_failed", "authentication failed", "malformed identity")
        account = self.store.account_for_key(message.get("api_key"))
        if account is None:
            return fail("auth_failed", "authentication failed - the API key is not valid", "unknown or revoked key")
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(signature, P.AUTH_CONTEXT + conn.nonce + device_id.encode())
        except (InvalidSignature, ValueError):
            return fail("auth_failed", "authentication failed - device signature invalid", f"bad signature {device_id}")
        public_text = base64.b64encode(public).decode()
        identity = {"device_id": device_id, "name": name[:80], "platform": str(identity.get("platform", ""))[:80],
                    "version": str(identity.get("version", ""))[:40], "public_key": public_text}
        existing = self.store.device(device_id)
        if existing:
            if existing["account_id"] != account["account_id"]:
                return fail("auth_failed", "authentication failed", f"{device_id} belongs to another account")
            if existing["public_key"] != public_text:
                return fail("auth_failed", "authentication failed - this device id is registered with another key",
                            f"key mismatch {device_id}")
            if existing["status"] == "revoked":
                self.store.audit("revoked_device_refused", account["account_id"], device_id, "", conn.address)
                conn.send({"t": "error", "code": "revoked",
                           "message": "This device was revoked and can no longer connect to the ZEUS NETWORK."})
                conn.close("revoked", policy=True)
                return
            self.store.touch_device(device_id, identity)
            record = self.store.device(device_id)
            registered = False
        else:
            record = self.store.register_device(account["account_id"], identity)
            registered = True
        conn.account_id, conn.device_id, conn.device_name = account["account_id"], device_id, record["name"]
        previous = self.online.get(device_id)
        if previous is not None and previous is not conn:
            previous.send({"t": "error", "code": "replaced", "message": "this device connected again elsewhere"})
            previous.close("replaced")
        self.online[device_id] = conn
        self.store.audit("device_connected", account["account_id"], device_id, {"registered": registered}, conn.address)
        conn.send({"t": "welcome", "account": {"id": account["account_id"], "name": account["name"]},
                   "device": self._device_view(conn.device_id, record), "registered": registered,
                   "server_time": time.time(), "protocol": P.PROTOCOL_VERSION})
        self._presence(device_id, True)
        self._flush(conn)

    # ------------------------------------------------------------- devices ----
    def _device_view(self, viewer: str, row: Dict[str, Any]) -> Dict[str, Any]:
        device_id = row["device_id"]
        paired = device_id == viewer or bool(self.store.pairing(viewer, device_id))
        visible = device_id == viewer or (paired and self.store.has(device_id, viewer, P.VIEW_STATUS))
        return {"device_id": device_id, "name": row["name"], "platform": row.get("platform"),
                "version": row.get("version"), "status": row["status"], "registered_at": row.get("registered_at"),
                "this_device": device_id == viewer, "paired": paired and device_id != viewer,
                "online": (device_id in self.online) if visible else None,
                "last_seen": row.get("last_seen") if visible else None,
                "grants_given": list(self.store.grants(viewer, device_id)) if device_id != viewer else [],
                "grants_received": list(self.store.grants(device_id, viewer)) if device_id != viewer else []}

    def _h_devices(self, conn, message):
        return {"t": "devices", "devices": [self._device_view(conn.device_id, row)
                                            for row in self.store.account_devices(conn.account_id)]}

    def _h_rename(self, conn, message):
        name = str(message.get("name", "")).strip()[:80]
        if not name:
            raise ProtocolError("bad_request", "a device name is needed")
        self.store.x("UPDATE devices SET name = ? WHERE device_id = ?", (name, conn.device_id))
        conn.device_name = name
        self.store.audit("device_renamed", conn.account_id, conn.device_id, name)
        for peer in self.store.peers(conn.device_id):
            self._push(peer, {"t": "device_changed", "device_id": conn.device_id})
        return {"t": "renamed", "name": name}

    def _presence(self, device_id: str, online: bool):
        row = self.store.device(device_id)
        for peer in self.store.peers(device_id):
            if self.store.has(device_id, peer, P.VIEW_STATUS):
                self._push(peer, {"t": "presence", "device_id": device_id, "online": online,
                                  "last_seen": row.get("last_seen") if row else None})

    def _push(self, device_id: str, message: Dict[str, Any]) -> bool:
        conn = self.online.get(device_id)
        if conn is None:
            return False
        conn.send(message)
        return True

    # ------------------------------------------------------------ pairing ----
    def _h_pair_create(self, conn, message):
        code = self.store.create_code(conn.account_id, conn.device_id)
        return {"t": "pair_code", **code}

    def _h_pair_join(self, conn, message):
        if self._limited(self._pair_failures, conn.device_id, P.PAIRING_MAX_FAILURES):
            raise ProtocolError("rate_limited", "too many wrong pairing codes - wait 10 minutes")
        code = str(message.get("code", "")).strip().replace(" ", "")
        if not re.fullmatch(r"\d{6}", code):
            raise ProtocolError("bad_request", "a pairing code is 6 digits")
        peer = self.store.redeem_code(conn.account_id, conn.device_id, code)
        if peer is None:
            self._pair_failures[conn.device_id].append(time.time())
            self.store.audit("pairing_code_rejected", conn.account_id, conn.device_id, "", conn.address)
            raise ProtocolError("bad_code", "that code is not valid (wrong, expired, already used, or from another "
                                            "account)")
        peer_row = self.store.device(peer)
        if not peer_row or peer_row["status"] != "active":
            raise ProtocolError("bad_code", "the device that made this code is not active")
        self.store.create_pairing(conn.account_id, conn.device_id, peer, by=conn.device_id)
        for me, other in ((conn.device_id, peer), (peer, conn.device_id)):
            self._push(me, {"t": "paired", "device": self._device_view(me, self.store.device(other))})
        self._presence(conn.device_id, True)
        if peer in self.online:
            self._presence(peer, True)
        return {"t": "pair_ok", "device": self._device_view(conn.device_id, peer_row)}

    def _require_peer(self, conn, device_id: str) -> Dict[str, Any]:
        row = self.store.device(str(device_id or ""))
        if not row or row["account_id"] != conn.account_id or row["device_id"] == conn.device_id:
            raise ProtocolError("unknown_device", "no such device in this account")
        return row

    def _h_unpair(self, conn, message):
        row = self._require_peer(conn, message.get("device_id"))
        if not self.store.end_pairing(conn.device_id, row["device_id"], by=conn.device_id):
            raise ProtocolError("not_paired", "these devices are not paired")
        for me, other in ((conn.device_id, row["device_id"]), (row["device_id"], conn.device_id)):
            self._push(me, {"t": "unpaired", "device_id": other})
        return {"t": "unpair_ok", "device_id": row["device_id"]}

    def _h_revoke(self, conn, message):
        row = self._require_peer(conn, message.get("device_id"))
        # Only a TRUSTED device may revoke: one that is paired, or the account's
        # first device. A newly registered, unpaired device holding a leaked key
        # cannot revoke the owner's devices.
        devices = [d for d in self.store.account_devices(conn.account_id) if d["status"] == "active"]
        first = devices[0]["device_id"] if devices else None
        if not self.store.peers(conn.device_id) and conn.device_id != first:
            raise ProtocolError("not_trusted", "only a paired device (or the account's first device) can revoke")
        peers = self.store.revoke_device(row["device_id"], by=conn.device_id)
        for peer in set(peers) | {conn.device_id}:
            self._push(peer, {"t": "device_revoked", "device_id": row["device_id"], "by": conn.device_name})
        target = self.online.get(row["device_id"])
        if target is not None:
            target.send({"t": "revoked", "by": conn.device_name,
                         "message": f"This device was revoked by {conn.device_name}."})
            target.close("revoked", policy=True)
        return {"t": "revoke_ok", "device_id": row["device_id"]}

    def _h_grants(self, conn, message):
        row = self._require_peer(conn, message.get("device_id"))
        if not self.store.pairing(conn.device_id, row["device_id"]):
            raise ProtocolError("not_paired", "grants exist only between paired devices")
        grants = self.store.set_grants(conn.device_id, row["device_id"], message.get("grants") or [])
        self._push(row["device_id"], {"t": "grants", "from": conn.device_id, "grants": list(grants)})
        self._presence(conn.device_id, True)
        return {"t": "grants_ok", "device_id": row["device_id"], "grants": list(grants)}

    # ------------------------------------------------------------- relay ----
    def _allowed(self, sender: str, recipient: str, kind: str, body: Dict[str, Any]) -> None:
        if not self.store.pairing(sender, recipient):
            raise ProtocolError("not_paired", "the devices are not paired")
        rule = P.KIND_RULES.get(kind)
        if rule is None or kind in ("receipt", "file"):
            raise ProtocolError("bad_kind", f"'{kind}' cannot be sent")
        if rule == ("reply", "reply"):
            task = self.store.task(str(body.get("task_id", "")))
            if kind == "task_status" and not (task and task["from_device"] == recipient and task["to_device"] == sender):
                raise ProtocolError("forbidden", "a task status answers a task the recipient sent")
            if kind == "task_cancel" and not (task and task["from_device"] == sender and task["to_device"] == recipient):
                raise ProtocolError("forbidden", "only the device that sent a task can cancel it")
            return
        outbound, inbound = rule
        if outbound and not self.store.has(sender, recipient, outbound):
            raise ProtocolError("forbidden", f"this device has not granted {outbound} to the recipient")
        if inbound and not self.store.has(recipient, sender, inbound):
            raise ProtocolError("forbidden", f"the recipient has not granted {inbound} to this device")

    def _h_send(self, conn, message):
        msg_id = str(message.get("id", ""))
        kind = str(message.get("kind", ""))
        body = message.get("body")
        if not re.fullmatch(r"[\w\-.:]{8,100}", msg_id) or not isinstance(body, dict):
            raise ProtocolError("bad_request", "a message needs an id and an object body")
        if len(json.dumps(body, default=str)) > P.MAX_BODY_BYTES:
            raise ProtocolError("too_large", "the body is too large - send it as a file")
        recipient = self._require_peer(conn, message.get("to"))
        if recipient["status"] != "active":
            raise ProtocolError("revoked", "the recipient device was revoked")
        self._allowed(conn.device_id, recipient["device_id"], kind, body)
        if kind == "task":
            self.store.record_task(str(body.get("task_id", "")), conn.device_id, recipient["device_id"],
                                   str(body.get("task_type", ""))[:40])
        elif kind == "task_status" and body.get("state") in P.TASK_STATES:
            self.store.set_task_state(str(body.get("task_id")), body["state"])
        seq, created = self.store.enqueue(conn.account_id, conn.device_id, recipient["device_id"], msg_id, kind, body)
        if created:
            self._deliver_to(recipient["device_id"])
        return {"t": "sent", "id": msg_id, "seq": seq, "duplicate": not created}

    def _deliver_to(self, device_id: str):
        conn = self.online.get(device_id)
        if conn is not None:
            self._flush(conn)

    def _flush(self, conn: Connection):
        if len(conn.inflight) >= 100:
            return
        for row in self.store.pending(conn.device_id, 0, 200):
            if row["seq"] in conn.inflight:
                continue
            conn.inflight.add(row["seq"])
            conn.send({"t": "deliver", "seq": row["seq"], "id": row["msg_id"], "from": row["from_device"],
                       "from_name": row.get("from_name"), "kind": row["kind"], "body": json.loads(row["body"]),
                       "created_at": row["created_at"]})
            if len(conn.inflight) >= 100:
                break

    def _receipt(self, row: Dict[str, Any], status: str, extra: Dict[str, Any] = None):
        if not row.get("from_device"):
            return
        body = {"id": row["msg_id"], "kind": row["kind"], "to": row["to_device"], "status": status, "at": time.time()}
        body.update(extra or {})
        self.store.enqueue(row["account_id"], None, row["from_device"], f"rcpt-{status}-{row['msg_id']}", "receipt", body)
        self._deliver_to(row["from_device"])

    def _h_ack(self, conn, message):
        try:
            seq = int(message.get("seq"))
        except (TypeError, ValueError):
            raise ProtocolError("bad_request", "ack needs a seq")
        row = self.store.mark_delivered(conn.device_id, seq)
        conn.inflight.discard(seq)
        if row and row["kind"] not in ("receipt", "file"):
            self._receipt(row, "delivered")
        self._flush(conn)
        return None

    def _h_ping(self, conn, message):
        return {"t": "pong", "server_time": time.time()}

    # ----------------------------------------------------------- transfers ----
    def _h_offer(self, conn, message):
        transfer_id = str(message.get("transfer_id", ""))
        try:
            transfer_id = str(uuid.UUID(transfer_id))
        except ValueError:
            raise ProtocolError("bad_request", "transfer_id must be a UUID")
        kind = str(message.get("kind", "file"))
        if kind not in P.FILE_KINDS:
            raise ProtocolError("bad_kind", f"'{kind}' is not a file kind")
        try:
            size = int(message.get("size"))
        except (TypeError, ValueError):
            raise ProtocolError("bad_request", "size must be a number")
        sha = str(message.get("sha256", "")).lower()
        if size < 0 or size > self.max_file_bytes or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ProtocolError("bad_request", f"size must be 0..{self.max_file_bytes} and sha256 64 hex characters")
        recipient = self._require_peer(conn, message.get("to"))
        if not self.store.pairing(conn.device_id, recipient["device_id"]):
            raise ProtocolError("not_paired", "the devices are not paired")
        if not self.store.has(conn.device_id, recipient["device_id"], P.SEND_FILES):
            raise ProtocolError("forbidden", "this device has not granted SEND_FILES to the recipient")
        if not self.store.has(recipient["device_id"], conn.device_id, P.RECEIVE_FILES):
            raise ProtocolError("forbidden", "the recipient has not granted RECEIVE_FILES to this device")
        if kind == "sync_file" and not self.store.has(recipient["device_id"], conn.device_id, P.WRITE_SHARED_DATA):
            raise ProtocolError("forbidden", "the recipient has not granted WRITE_SHARED_DATA to this device")
        row = self.store.transfer(transfer_id)
        if row:
            if row["from_device"] != conn.device_id or row["to_device"] != recipient["device_id"] \
                    or row["size"] != size or row["sha256"] != sha:
                raise ProtocolError("conflict", "a different transfer already uses this id")
            if row["state"] != "uploading":
                return {"t": "offer_ok", "transfer_id": transfer_id, "offset": row["received"], "state": row["state"]}
        else:
            free = shutil.disk_usage(self.store.blob_dir).free
            if free < size + 512 * 1024 * 1024:
                raise ProtocolError("no_space", "the server does not have room for this file")
            meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
            if len(json.dumps(meta)) > 16 * 1024:
                raise ProtocolError("too_large", "metadata too large")
            self.store.create_transfer({"transfer_id": transfer_id, "account_id": conn.account_id,
                                        "from_device": conn.device_id, "to_device": recipient["device_id"],
                                        "name": _clean_name(message.get("name")), "size": size, "sha256": sha,
                                        "mime": str(message.get("mime", ""))[:100], "kind": kind, "meta": meta})
            row = self.store.transfer(transfer_id)
        state = self._open_upload(conn, row)
        return {"t": "offer_ok", "transfer_id": transfer_id, "offset": state["received"], "state": "uploading"}

    def _open_upload(self, conn: Connection, row: Dict[str, Any]) -> Dict[str, Any]:
        """The upload's file and running hash. A resumed upload re-reads what is
        already on disk once, so the hash covers every byte actually received."""
        transfer_id = row["transfer_id"]
        if transfer_id in conn.uploads:
            return conn.uploads[transfer_id]
        path = self.store.blob_path(transfer_id)
        digest = hashlib.sha256()
        received = 0
        if path.exists():
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    received += len(block)
        if received != row["received"]:
            self.store.set_transfer(transfer_id, received=received)
        state = {"fh": path.open("ab"), "hash": digest, "received": received, "size": row["size"],
                 "sha256": row["sha256"], "since_ack": 0}
        conn.uploads[transfer_id] = state
        return state

    def _on_binary(self, conn: Connection, frame: bytes):
        if conn.device_id is None:
            conn.close("authenticate first", policy=True)
            return
        try:
            transfer_id, offset, data = P.unpack_chunk(frame)
        except ValueError as error:
            raise ProtocolError("bad_chunk", str(error))
        state = conn.uploads.get(transfer_id)
        if state is None:
            raise ProtocolError("no_upload", "offer the transfer before sending chunks")
        if offset != state["received"]:
            conn.send({"t": "upload_resync", "transfer_id": transfer_id, "offset": state["received"]})
            return
        if state["received"] + len(data) > state["size"]:
            raise ProtocolError("too_large", "more bytes than offered")
        state["fh"].write(data)
        state["hash"].update(data)
        state["received"] += len(data)
        state["since_ack"] += len(data)
        if state["since_ack"] >= 1024 * 1024 or state["received"] == state["size"]:
            state["fh"].flush()
            state["since_ack"] = 0
            self.store.set_transfer(transfer_id, received=state["received"])
            conn.send({"t": "up_ack", "transfer_id": transfer_id, "offset": state["received"]})

    def _h_upload_done(self, conn, message):
        transfer_id = str(message.get("transfer_id", ""))
        state = conn.uploads.get(transfer_id)
        row = self.store.transfer(transfer_id)
        if row is None or row["from_device"] != conn.device_id:
            raise ProtocolError("unknown_transfer", "no such transfer")
        if row["state"] != "uploading":
            return {"t": "upload_ok", "transfer_id": transfer_id, "state": row["state"]}
        if state is None:
            state = self._open_upload(conn, row)
        state["fh"].flush()
        state["fh"].close()
        conn.uploads.pop(transfer_id, None)
        self.store.set_transfer(transfer_id, received=state["received"])
        if state["received"] != row["size"] or state["hash"].hexdigest() != row["sha256"]:
            self.store.set_transfer(transfer_id, state="failed")
            self.store.blob_path(transfer_id).unlink(missing_ok=True)
            self.store.audit("transfer_integrity_failed", conn.account_id, conn.device_id, transfer_id)
            raise ProtocolError("integrity", "the received bytes do not match the offered size and SHA-256 - "
                                             "the upload was discarded; send it again")
        self.store.set_transfer(transfer_id, state="stored", completed_at=time.time())
        body = {"transfer_id": transfer_id, "name": row["name"], "size": row["size"], "sha256": row["sha256"],
                "mime": row["mime"], "file_kind": row["kind"], "meta": json.loads(row["meta"] or "{}")}
        self.store.enqueue(conn.account_id, conn.device_id, row["to_device"], f"file-{transfer_id}", "file", body)
        self._deliver_to(row["to_device"])
        self.store.audit("transfer_stored", conn.account_id, conn.device_id, {"transfer_id": transfer_id})
        return {"t": "upload_ok", "transfer_id": transfer_id, "state": "stored"}

    def _h_download(self, conn, message):
        row = self.store.transfer(str(message.get("transfer_id", "")))
        if row is None or row["to_device"] != conn.device_id:
            raise ProtocolError("unknown_transfer", "no such transfer for this device")
        if row["state"] != "stored":
            raise ProtocolError("unavailable", f"the file is {row['state']}")
        try:
            offset = max(0, int(message.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        end = min(row["size"], offset + P.DOWNLOAD_WINDOW)
        with self.store.blob_path(row["transfer_id"]).open("rb") as handle:
            handle.seek(offset)
            position = offset
            while position < end:
                data = handle.read(min(P.CHUNK_SIZE, end - position))
                if not data:
                    break
                conn.send_binary(P.pack_chunk(row["transfer_id"], position, data))
                position += len(data)
        return {"t": "down_window", "transfer_id": row["transfer_id"], "offset": position,
                "eof": position >= row["size"], "size": row["size"]}

    def _h_download_done(self, conn, message):
        row = self.store.transfer(str(message.get("transfer_id", "")))
        if row is None or row["to_device"] != conn.device_id:
            raise ProtocolError("unknown_transfer", "no such transfer for this device")
        if str(message.get("sha256", "")).lower() != row["sha256"]:
            raise ProtocolError("integrity", "the downloaded file does not match - download it again")
        if row["state"] == "stored":
            self.store.set_transfer(row["transfer_id"], state="delivered", delivered_at=time.time())
            self.store.blob_path(row["transfer_id"]).unlink(missing_ok=True)
            queued = self.store.one("SELECT * FROM queue WHERE to_device = ? AND msg_id = ?",
                                    (conn.device_id, f"file-{row['transfer_id']}"))
            if queued:
                self._receipt(queued, "delivered", {"transfer_id": row["transfer_id"], "name": row["name"]})
            self.store.audit("transfer_delivered", conn.account_id, conn.device_id, {"transfer_id": row["transfer_id"]})
        return {"t": "download_ok", "transfer_id": row["transfer_id"]}

    def _h_transfer_status(self, conn, message):
        row = self.store.transfer(str(message.get("transfer_id", "")))
        if row is None or conn.device_id not in (row["from_device"], row["to_device"]):
            raise ProtocolError("unknown_transfer", "no such transfer")
        return {"t": "transfer_status", "transfer_id": row["transfer_id"], "state": row["state"],
                "received": row["received"], "size": row["size"]}

    def _h_cancel_transfer(self, conn, message):
        row = self.store.transfer(str(message.get("transfer_id", "")))
        if row is None or conn.device_id not in (row["from_device"], row["to_device"]):
            raise ProtocolError("unknown_transfer", "no such transfer")
        if row["state"] in ("uploading", "stored"):
            state = conn.uploads.pop(row["transfer_id"], None)
            if state:
                state["fh"].close()
            self.store.cancel_transfer(row["transfer_id"], f"cancelled by {conn.device_name}")
            other = row["to_device"] if conn.device_id == row["from_device"] else row["from_device"]
            self._push(other, {"t": "transfer_cancelled", "transfer_id": row["transfer_id"]})
        return {"t": "cancel_ok", "transfer_id": row["transfer_id"]}
