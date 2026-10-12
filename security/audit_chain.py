"""
Tamper-evident audit log.

Every record carries the hash of the record before it, and the hash itself is
an HMAC keyed with a secret held outside the log. So:

  * editing any record changes its hash and breaks every link after it;
  * deleting a record breaks the link across the gap;
  * truncating the tail is caught by the ANCHOR - the last sequence number and
    hash, kept in the database, a separate file the log does not live in;
  * forging a whole new chain needs the key, which is protected with Windows
    DPAPI (bound to this Windows account) where available.

What this is NOT: prevention. Anyone who controls this Windows account can
delete the file. What they cannot do is change it without `verify()` saying
exactly where. That is the honest claim, and it is the one that matters for an
investigation.

The plain `logs/audit.log` the project always wrote is kept unchanged; this
chain sits beside it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

from core.logger import get_logger

logger = get_logger("security.audit")

GENESIS = "0" * 64


# ----------------------------------------------------------- key storage --

def _dpapi(data: bytes, protect: bool) -> Optional[bytes]:
    """CryptProtectData / CryptUnprotectData through ctypes. None if unavailable."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_string_buffer(data, len(data))
        blob_in = BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = BLOB()
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = function(ctypes.byref(blob_in), None, None, None, None, 0x01,  # UI_FORBIDDEN
                      ctypes.byref(blob_out))
        if not ok:
            return None
        result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        kernel32.LocalFree(blob_out.pbData)
        return result
    except Exception as error:
        logger.debug(f"DPAPI unavailable: {error}")
        return None


def load_or_create_key(path: Path) -> bytes:
    """A 32-byte secret, DPAPI-wrapped on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_bytes()
        if raw.startswith(b"DPAPI:"):
            unwrapped = _dpapi(base64.b64decode(raw[6:]), protect=False)
            if unwrapped:
                return unwrapped
            raise RuntimeError("the audit key could not be unwrapped by this Windows account")
        if raw.startswith(b"RAW:"):
            return base64.b64decode(raw[4:])
    key = os.urandom(32)
    wrapped = _dpapi(key, protect=True)
    if wrapped:
        path.write_bytes(b"DPAPI:" + base64.b64encode(wrapped))
    else:
        path.write_bytes(b"RAW:" + base64.b64encode(key))
    return key


# ------------------------------------------------------- process lock --

@contextmanager
def _process_lock(lock_path: Path, timeout: float = 15.0):
    """Serialise appends across PROCESSES (the app, the ops CLI, the firewall
    BAT running elevated) - a thread lock only covers one process."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    deadline = time.time() + timeout
    locked = False
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError(f"audit chain lock busy for {timeout:.0f}s ({lock_path})")
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ----------------------------------------------------------------- chain --

class AuditChain:
    def __init__(self):
        self._lock = threading.Lock()
        self._key: Optional[bytes] = None
        self._last: Optional[Dict] = None
        self._owner_cache: Dict[str, bool] = {}
        self._warned_foreign = False

    @staticmethod
    def path() -> Path:
        from core.paths import log_dir
        return log_dir() / "audit_chain.jsonl"

    @staticmethod
    def key_path() -> Path:
        from core.paths import data_dir
        return data_dir() / ".audit_key"

    def _get_key(self) -> bytes:
        if self._key is None:
            self._key = load_or_create_key(self.key_path())
        return self._key

    @staticmethod
    def _canonical(record: Dict) -> bytes:
        body = {k: v for k, v in record.items() if k != "hash"}
        return json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    def _sign(self, record: Dict) -> str:
        return hmac.new(self._get_key(), self._canonical(record), hashlib.sha256).hexdigest()

    def _tail(self, path: Optional[Path] = None) -> Dict:
        """The last record ON DISK. Never a cached copy: another process (the
        firewall BAT running as administrator, the ops CLI) may have appended
        since - a cached tail is how two records once got the same number."""
        path = path or self.path()
        last = {"seq": 0, "hash": GENESIS}
        if path.exists():
            try:
                with path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(max(0, size - 65536))
                    lines = handle.read().decode("utf-8", "replace").splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if line:
                        record = json.loads(line)
                        last = {"seq": int(record["seq"]), "hash": record["hash"]}
                        break
            except Exception as error:
                logger.warning(f"audit chain tail unreadable: {error}")
        self._last = last
        return last

    def _first_record(self, path: Path) -> Optional[Dict]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        return json.loads(line)
        except Exception:
            return None
        return None

    def owns(self, path: Optional[Path] = None) -> bool:
        """Was this chain started with THIS process's key? A process whose data
        directory (and so its key) differs from the one that started the log -
        a test run with a temporary data folder - must not append to it: its
        records could never verify and would break the chain for everyone."""
        path = path or self.path()
        cache_key = str(path)
        if self._owner_cache.get(cache_key) is not None:
            return self._owner_cache[cache_key]
        first = self._first_record(path) if path.exists() else None
        owned = True if first is None else hmac.compare_digest(str(first.get("hash", "")), self._sign(first))
        self._owner_cache[cache_key] = owned
        return owned

    def _target(self) -> Path:
        path = self.path()
        if self.owns(path):
            return path
        fingerprint = hmac.new(self._get_key(), b"agni-audit-key", hashlib.sha256).hexdigest()[:12]
        side = path.with_name(f"audit_chain.foreign-{fingerprint}.jsonl")
        if not self._warned_foreign:
            logger.error(f"AUDIT: this process's key did not start {path.name} (different data folder?) - "
                         f"writing to {side.name} instead of breaking the main chain")
            self._warned_foreign = True
        return side

    def append(self, event: str, detail: str = "", actor: str = "", **extra) -> Optional[Dict]:
        """Append one record. Returns it, or None if the log could not be written
        (which is itself logged - an audit failure is never silent)."""
        try:
            target = self._target()
            with self._lock, _process_lock(target.with_suffix(".lock")):
                tail = self._tail(target)
                record = {"seq": tail["seq"] + 1, "ts": round(time.time(), 3),
                          "event": event, "actor": actor or "", "detail": (detail or "")[:2000],
                          "prev": tail["hash"]}
                for key, value in extra.items():
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        record[key] = value
                record["hash"] = self._sign(record)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._last = {"seq": record["seq"], "hash": record["hash"]}
                if target == self.path():
                    self._write_anchor(record["seq"], record["hash"])
                return record
        except Exception as error:
            logger.error(f"AUDIT CHAIN WRITE FAILED: {error}")
            return None

    # -- anchor (outside the log file) -------------------------------------

    @staticmethod
    def _anchor_conn():
        from memory import database
        conn = database.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS audit_anchor (id INTEGER PRIMARY KEY CHECK (id = 1), "
                     "seq INTEGER NOT NULL, hash TEXT NOT NULL, ts REAL NOT NULL)")
        return conn

    def _write_anchor(self, seq: int, digest: str) -> None:
        try:
            conn = self._anchor_conn()
            try:
                conn.execute("INSERT INTO audit_anchor (id, seq, hash, ts) VALUES (1, ?, ?, ?) "
                             "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq, hash=excluded.hash, "
                             "ts=excluded.ts", (seq, digest, time.time()))
                conn.commit()
            finally:
                conn.close()
        except Exception as error:
            logger.debug(f"audit anchor not written: {error}")

    def _read_anchor(self) -> Optional[Dict]:
        try:
            conn = self._anchor_conn()
            try:
                row = conn.execute("SELECT seq, hash FROM audit_anchor WHERE id = 1").fetchone()
                return {"seq": row["seq"], "hash": row["hash"]} if row else None
            finally:
                conn.close()
        except Exception:
            return None

    # -- verification ------------------------------------------------------

    def verify(self) -> Dict:
        """Walk the whole chain. Every record must carry a valid signature and
        link to an authentic record one position before it.

        Reported as BROKEN (ok False): a modified record (signature), a
        removed record (a link that leads nowhere), a malformed line, a
        truncated tail (the anchor).

        Reported as a FORK (ok True, with `forks`): two records at the SAME
        position, both correctly signed and both linking to the same authentic
        predecessor - two processes appended at once. Nothing was altered or
        removed; the records are kept exactly as written and the fork's
        location is reported. Appends are serialised across processes since,
        so a new fork means that lock was bypassed."""
        path = self.path()
        if not path.exists():
            return {"ok": True, "records": 0, "detail": "no audit records yet", "forks": []}
        accepted: Dict[int, Dict[str, int]] = {0: {GENESIS: 0}}   # seq -> {hash: line}
        forks = []
        count = 0
        top = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        seq = int(record.get("seq"))
                    except (ValueError, TypeError):
                        return {"ok": False, "records": count, "broken_at_line": number, "forks": forks,
                                "detail": f"line {number} is not a valid record"}
                    if not hmac.compare_digest(str(record.get("hash", "")), self._sign(record)):
                        return {"ok": False, "records": count, "broken_at_line": number, "forks": forks,
                                "detail": f"record {seq} (line {number}) was modified after it was written"}
                    parents = accepted.get(seq - 1, {})
                    if record.get("prev") not in parents:
                        expected = top + 1
                        what = (f"sequence gap at line {number}: expected {expected}, found {seq}"
                                if seq != expected else f"record {seq} does not link to the one before it")
                        return {"ok": False, "records": count, "broken_at_line": number, "forks": forks,
                                "detail": what}
                    siblings = accepted.setdefault(seq, {})
                    if record["hash"] in siblings:
                        return {"ok": False, "records": count, "broken_at_line": number, "forks": forks,
                                "detail": f"record {seq} appears twice (lines {siblings[record['hash']]} and "
                                          f"{number}) - a copied line was inserted"}
                    if siblings:
                        first_line = next(iter(siblings.values()))
                        forks.append({"seq": seq, "lines": [first_line, number],
                                      "detail": f"two authentic records numbered {seq} (lines {first_line} and "
                                                f"{number}) - written concurrently by two processes"})
                    siblings[record["hash"]] = number
                    top = max(top, seq)
                    count += 1
                    for old in [s for s in accepted if s < top - 2]:
                        del accepted[old]
        except Exception as error:
            return {"ok": False, "records": count, "forks": forks, "detail": f"audit chain unreadable: {error}"}

        anchor = self._read_anchor()
        if anchor and (anchor["seq"] != top or anchor["hash"] not in accepted.get(top, {})):
            return {"ok": False, "records": count, "forks": forks,
                    "detail": (f"the log ends at record {top} but the anchor says "
                               f"{anchor['seq']} - records were removed from the end")}
        detail = f"{count} records, every link and signature verified"
        if forks:
            detail += (f"; {len(forks)} concurrent-write fork(s) at record(s) "
                       f"{', '.join(str(f['seq']) for f in forks)} - authentic, nothing altered or removed")
        return {"ok": True, "records": count, "detail": detail, "anchored": bool(anchor), "forks": forks}

    def tail(self, limit: int = 50) -> list:
        path = self.path()
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
            return [json.loads(line) for line in reversed(lines) if line.strip()]
        except Exception:
            return []

    def reset_cache(self) -> None:
        """For tests and after the data directory changes."""
        with self._lock:
            self._key = None
            self._last = None
            self._owner_cache = {}
            self._warned_foreign = False


audit_chain = AuditChain()
