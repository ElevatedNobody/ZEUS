"""
Bidirectional folder sync between two paired devices, over the same transfers
as any file (resumable, SHA-256 verified).

    A shares a folder with B          -> B sees "folder offered" and picks where it lives
    each side sends a MANIFEST        -> (relative path, size, SHA-256) of every file
    each side requests what it needs  -> files it lacks, or that changed on the other side only
    files arrive as sync_file         -> written into the synced folder

Rules
    * a file changed on BOTH sides since the last sync is a CONFLICT: the local
      file is kept and the incoming one is saved beside it as
      "name.sync-conflict-<device>-<time>.ext" - nothing is overwritten silently
    * deletions are NOT propagated (a deleted file comes back from the other
      side); removing a file everywhere is a person's decision
    * writing into a folder needs WRITE_SHARED_DATA granted to the sender;
      reading its files needs READ_SHARED_DATA - checked here and by the server
    * key / credential files are never sent (crudex_net/sharing.py)
    * a synced folder may not be inside ZEUS's protected folders
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger
from crudex_net import settings, sharing, store
from crudex_server import protocol as P

logger = get_logger("crudex.folders")
_SCAN_EVERY = 300
_last_scan: Dict[str, float] = {}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_folder(local_path: str) -> Path:
    from security import pathguard
    ok, why = pathguard.check(local_path)
    if not ok:
        raise PermissionError(why)
    folder = Path(local_path).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} is not a folder")
    return folder


def _relative(folder: Path, rel_path: str) -> Path:
    rel = Path(str(rel_path).replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"not a relative path inside the folder: {rel_path}")
    target = (folder / rel).resolve()
    target.relative_to(folder)                 # raises if it escapes the folder
    return target


def scan(folder_key: str, folder: Path) -> List[Dict[str, Any]]:
    """The manifest of a folder. Hashes are reused while size and mtime are unchanged."""
    known = {r["rel_path"]: r for r in store.q("SELECT * FROM folder_files WHERE folder_key = ?", (folder_key,))}
    files = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.startswith(".") or name.endswith((".part", ".crxpart", ".tmp")):
                continue
            path = Path(root) / name
            if path.is_symlink():
                continue
            try:
                sharing.check_file(str(path))
            except (PermissionError, FileNotFoundError):
                continue
            stat = path.stat()
            rel = path.relative_to(folder).as_posix()
            row = known.get(rel)
            if row and row["size"] == stat.st_size and abs((row["mtime"] or 0) - stat.st_mtime) < 0.001 and row["sha256"]:
                sha = row["sha256"]
            else:
                sha = _sha(path)
                store.x("INSERT INTO folder_files (folder_key, rel_path, size, mtime, sha256) VALUES (?,?,?,?,?) "
                        "ON CONFLICT (folder_key, rel_path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, "
                        "sha256 = excluded.sha256", (folder_key, rel, stat.st_size, stat.st_mtime, sha))
            files.append({"path": rel, "size": stat.st_size, "sha256": sha})
    return files


# ---------------------------------------------------------------- A side --
def share_folder(client, to_device: str, local_path: str, name: str = "", confirmed: bool = False) -> Dict[str, Any]:
    sharing.check_category("document", confirmed)
    folder = _check_folder(local_path)
    folder_key = f"fld-{uuid.uuid4().hex[:16]}"
    store.x("INSERT INTO folders (folder_key, peer, local_path, name, state, created_at) VALUES (?,?,?,?,?,?)",
            (folder_key, to_device, str(folder), (name or folder.name)[:80], "active", time.time()))
    store.audit("folder_shared", {"folder_key": folder_key, "to": to_device, "path": str(folder)})
    send_manifest(client, folder_key, to_device, reply=False)
    return {"ok": True, "folder_key": folder_key, "name": name or folder.name}


def send_manifest(client, folder_key: str, peer: str, reply: bool) -> Optional[str]:
    row = store.one("SELECT * FROM folders WHERE folder_key = ? AND peer = ? AND state = 'active' AND enabled = 1",
                    (folder_key, peer))
    if not row or not row["local_path"]:
        return None
    files = scan(folder_key, Path(row["local_path"]))
    _last_scan[f"{folder_key}:{peer}"] = time.time()
    body = {"folder_key": folder_key, "name": row["name"], "files": files[:5000], "reply": reply}
    return client.queue_event(peer, "sync_manifest", body, msg_id=f"mft-{uuid.uuid4().hex}")


def accept(client, folder_key: str, peer: str, local_path: str) -> Dict[str, Any]:
    folder = _check_folder(local_path)
    row = store.one("SELECT * FROM folders WHERE folder_key = ? AND peer = ?", (folder_key, peer))
    if not row or row["state"] != "offered":
        raise ValueError("there is no folder offer with that key from that device")
    store.x("UPDATE folders SET local_path = ?, state = 'active' WHERE folder_key = ? AND peer = ?",
            (str(folder), folder_key, peer))
    store.audit("folder_accepted", {"folder_key": folder_key, "from": peer, "path": str(folder)})
    manifest = store.get(f"manifest:{folder_key}:{peer}") or {}
    _answer(client, peer, {"folder_key": folder_key, "files": manifest.get("files", []), "reply": False})
    return {"ok": True, "folder_key": folder_key, "path": str(folder)}


def remove(folder_key: str, peer: str) -> bool:
    return bool(store.x("UPDATE folders SET enabled = 0, state = 'removed' WHERE folder_key = ? AND peer = ?",
                        (folder_key, peer)))


def sync_now(client, folder_key: str, peer: str) -> Dict[str, Any]:
    msg_id = send_manifest(client, folder_key, peer, reply=False)
    if not msg_id:
        raise ValueError("that synced folder is not active")
    return {"ok": True, "manifest": msg_id}


def tick(client) -> None:
    now = time.time()
    for row in store.q("SELECT * FROM folders WHERE state = 'active' AND enabled = 1"):
        key = f"{row['folder_key']}:{row['peer']}"
        if now - _last_scan.get(key, 0) >= _SCAN_EVERY:
            try:
                send_manifest(client, row["folder_key"], row["peer"], reply=False)
            except Exception as error:
                store.x("UPDATE folders SET last_error = ? WHERE folder_key = ? AND peer = ?",
                        (str(error)[:300], row["folder_key"], row["peer"]))


# ---------------------------------------------------------------- B side --
def on_manifest(client, sender: str, body: Dict[str, Any]) -> None:
    folder_key = str(body.get("folder_key", ""))[:40]
    if not folder_key:
        return
    store.put(f"manifest:{folder_key}:{sender}", {"files": body.get("files", []), "at": time.time()})
    row = store.one("SELECT * FROM folders WHERE folder_key = ? AND peer = ?", (folder_key, sender))
    if row is None:
        store.x("INSERT INTO folders (folder_key, peer, name, state, created_at) VALUES (?,?,?,?,?)",
                (folder_key, sender, str(body.get("name", "shared folder"))[:80], "offered", time.time()))
        client._emit({"type": "folder_offered", "folder_key": folder_key, "peer": sender, "name": body.get("name")})
        return
    if row["state"] != "active" or not row["enabled"]:
        return
    _answer(client, sender, body)


def _answer(client, sender: str, body: Dict[str, Any]) -> None:
    folder_key = body["folder_key"]
    row = store.one("SELECT * FROM folders WHERE folder_key = ? AND peer = ?", (folder_key, sender))
    folder = Path(row["local_path"])
    local = {f["path"]: f for f in scan(folder_key, folder)}
    records = {r["rel_path"]: r for r in store.q("SELECT * FROM folder_files WHERE folder_key = ?", (folder_key,))}
    wanted = []
    for remote in body.get("files", []):
        path, sha = str(remote.get("path", "")), str(remote.get("sha256", ""))
        try:
            _relative(folder, path)
        except ValueError:
            continue
        mine = local.get(path)
        synced = (records.get(path) or {}).get("synced_sha256")
        if mine and mine["sha256"] == sha:
            store.x("UPDATE folder_files SET synced_sha256 = ?, synced_at = ? WHERE folder_key = ? AND rel_path = ?",
                    (sha, time.time(), folder_key, path))
        elif mine is None or mine["sha256"] == synced or sha != synced:
            wanted.append(path)            # missing here, changed only there, or changed on both (conflict copy)
    wanted = [p for p in wanted if not _in_flight(folder_key, p)]
    if wanted:
        client.queue_event(sender, "sync_request", {"folder_key": folder_key, "paths": wanted[:1000]},
                           msg_id=f"sreq-{uuid.uuid4().hex}")
    store.x("UPDATE folders SET last_sync_at = ?, last_error = NULL WHERE folder_key = ? AND peer = ?",
            (time.time(), folder_key, sender))
    if not body.get("reply"):
        send_manifest(client, folder_key, sender, reply=True)
    client._emit({"type": "folder", "folder_key": folder_key, "peer": sender, "requested": len(wanted)})


def _in_flight(folder_key: str, rel_path: str) -> bool:
    return any(store.loads(r["meta"], {}).get("rel_path") == rel_path and
               store.loads(r["meta"], {}).get("folder_key") == folder_key
               for r in store.q("SELECT meta FROM transfers WHERE file_kind = 'sync_file' AND direction = 'in' "
                                "AND state IN ('offered', 'downloading')"))


def on_request(client, sender: str, body: Dict[str, Any]) -> None:
    folder_key = str(body.get("folder_key", ""))
    row = store.one("SELECT * FROM folders WHERE folder_key = ? AND peer = ? AND state = 'active' AND enabled = 1",
                    (folder_key, sender))
    if not row or P.READ_SHARED_DATA not in (client.my_grants_to(sender) or [P.READ_SHARED_DATA]):
        return
    folder = Path(row["local_path"])
    for rel in body.get("paths", [])[:1000]:
        try:
            target = _relative(folder, rel)
            sharing.check_file(str(target))
        except (ValueError, PermissionError, FileNotFoundError):
            continue
        sha = _sha(target)
        duplicate = any(store.loads(r["meta"], {}).get("rel_path") == rel and r["sha256"] == sha
                        for r in store.q("SELECT meta, sha256 FROM transfers WHERE direction = 'out' AND peer = ? AND "
                                         "file_kind = 'sync_file' AND state IN ('queued', 'uploading')", (sender,)))
        if not duplicate:
            client.queue_file(sender, target, "sync_file", meta={"folder_key": folder_key, "rel_path": rel,
                                                                 "sha256": sha}, name=target.name)


def place_file(client, peer: str, meta: Dict[str, Any], part: Path, sha: str) -> Tuple[Optional[Path], int]:
    """Where an incoming sync_file goes. (None, 0) = not into a synced folder (it
    lands in the received-files folder instead)."""
    row = store.one("SELECT * FROM folders WHERE folder_key = ? AND peer = ? AND state = 'active' AND enabled = 1",
                    (str(meta.get("folder_key", "")), peer))
    if not row or P.WRITE_SHARED_DATA not in client.my_grants_to(peer):
        return None, 0
    folder = Path(row["local_path"])
    rel = str(meta.get("rel_path", ""))
    try:
        target = _relative(folder, rel)
    except ValueError:
        return None, 0
    record = store.one("SELECT * FROM folder_files WHERE folder_key = ? AND rel_path = ?", (row["folder_key"], rel))
    conflict = 0
    if target.exists():
        current = _sha(target)
        if current == sha:
            part.unlink(missing_ok=True)
            return target, 0
        if record is None or current != record.get("synced_sha256"):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            device = "".join(c for c in client.peer_name(peer) if c.isalnum() or c in "-_")[:30] or "device"
            target = target.with_name(f"{target.stem}.sync-conflict-{device}-{stamp}{target.suffix}")
            conflict = 1
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(part), str(target))
    stat = target.stat()
    stored_rel = target.relative_to(folder).as_posix()
    store.x("INSERT INTO folder_files (folder_key, rel_path, size, mtime, sha256, synced_sha256, synced_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT (folder_key, rel_path) DO UPDATE SET size = excluded.size, "
            "mtime = excluded.mtime, sha256 = excluded.sha256, synced_sha256 = excluded.synced_sha256, "
            "synced_at = excluded.synced_at", (row["folder_key"], stored_rel, stat.st_size, stat.st_mtime, sha, sha,
                                               time.time()))
    if conflict:
        # The other side's version of `rel` has now been seen and kept (as the
        # copy); recording it stops the next manifest asking for it again.
        store.x("UPDATE folder_files SET synced_sha256 = ?, synced_at = ? WHERE folder_key = ? AND rel_path = ?",
                (sha, time.time(), row["folder_key"], rel))
        store.audit("sync_conflict", {"folder_key": row["folder_key"], "path": rel, "kept_both": stored_rel})
        client._emit({"type": "conflict", "folder_key": row["folder_key"], "path": rel, "copy": stored_rel})
    return target, conflict
