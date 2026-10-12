"""
Shared items: the generic ZEUS data layer for everything that is not a plain
file, a message or a task.

    memory         one remembered fact                      category memory
    knowledge      a knowledge entry (title + text)          category knowledge   (kind "knowledge")
    conversation   a conversation (turns)                    category conversation
    preferences    selected, non-secret preferences          category preferences
    workspace      the workspace state (screen, project)     category preferences
    project        a ZEUS project update (sync bundle)     category document    (sent as a file)
    artifact       a generated file from the artifact list   category artifact    (sent as a file)

Sending: the sharing category is checked (LOCAL_ONLY / SHARED / ASK), then the
content is refused if it holds a secret (crudex_net/sharing.py).

Receiving: the item is stored and listed. Nothing is written into this device's
memory, knowledge base or projects until it is IMPORTED - by the user pressing
Import, or automatically only where the user granted the sender
WRITE_SHARED_KNOWLEDGE (knowledge) / WRITE_SHARED_DATA (project updates). Every
import runs through the policy engine (origin "lan" when automatic), so the
signed-in user's role still decides - an operator cannot add to the company
knowledge base, whoever sent the entry.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from core.logger import get_logger
from crudex_net import settings, sharing, store
from crudex_server import protocol as P

logger = get_logger("crudex.items")
ITEM_TYPES = ("memory", "knowledge", "conversation", "preferences", "workspace")


def share(client, to_device: str, item_type: str, title: str, content: Any, confirmed: bool = False) -> Dict[str, Any]:
    if item_type not in ITEM_TYPES:
        raise ValueError(f"'{item_type}' is not a shareable item type ({', '.join(ITEM_TYPES)}; files, artifacts and "
                         f"projects are sent as files)")
    sharing.check_category(sharing.category_for_item(item_type), confirmed)
    if item_type in ("memory", "knowledge"):
        text = str(content.get("text") if isinstance(content, dict) else content or "").strip()
        if not text:
            raise ValueError("nothing to share")
        sharing.check_text(text, item_type)
        content = {"text": text[:100_000]}
    elif item_type == "conversation":
        turns = content if isinstance(content, list) else (content or {}).get("turns", [])
        turns = [{"role": str(t.get("role", "")), "text": str(t.get("text", ""))[:20000]} for t in turns if isinstance(t, dict)]
        if not turns:
            raise ValueError("the conversation is empty")
        sharing.check_text("\n".join(t["text"] for t in turns), "conversation")
        content = {"turns": turns[-200:]}
    else:
        if not isinstance(content, dict):
            raise ValueError("preferences / workspace state is an object of simple values")
        content = {str(k)[:60]: v for k, v in content.items() if isinstance(v, (str, int, float, bool)) or v is None}
        sharing.check_text(json.dumps(content), item_type)
    body = {"item_type": item_type, "title": (title or item_type).strip()[:200], "content": content,
            "shared_at": time.time()}
    kind = "knowledge" if item_type == "knowledge" else "share"
    msg_id = client.queue_event(to_device, kind, body, msg_id=f"item-{uuid.uuid4().hex}")
    store.audit("item_shared", {"to": to_device, "type": item_type, "msg_id": msg_id})
    return {"ok": True, "msg_id": msg_id, "state": "pending"}


# ------------------------------------------------------------ receiving ----
def on_item(client, sender: str, sender_name: Optional[str], msg_id: str, kind: str, body: Dict[str, Any]) -> None:
    item_type = str(body.get("item_type", "item"))[:40]
    store.x("INSERT OR IGNORE INTO items (item_id, peer, kind, item_type, title, body, received_at) "
            "VALUES (?,?,?,?,?,?,?)", (msg_id, sender, kind, item_type, str(body.get("title", ""))[:200],
                                       json.dumps(body.get("content"), default=str), time.time()))
    store.audit("item_received", {"from": sender, "type": item_type, "msg_id": msg_id})
    client._emit({"type": "item", "item_id": msg_id, "item_type": item_type, "peer": sender, "peer_name": sender_name})
    if item_type == "knowledge" and P.WRITE_SHARED_KNOWLEDGE in client.my_grants_to(sender):
        threading.Thread(target=_auto_import, args=(client, msg_id), daemon=True).start()


def on_file(client, transfer: Dict[str, Any], final: Path) -> None:
    if transfer["file_kind"] != "project_bundle":
        return
    item_id = f"file-{transfer['transfer_id']}"
    store.x("INSERT OR IGNORE INTO items (item_id, peer, kind, item_type, title, body, received_at) "
            "VALUES (?,?,?,?,?,?,?)", (item_id, transfer["peer"], "file", "project", transfer["name"],
                                       json.dumps({"path": str(final), "transfer_id": transfer["transfer_id"]}),
                                       time.time()))
    client._emit({"type": "item", "item_id": item_id, "item_type": "project", "peer": transfer["peer"]})
    if P.WRITE_SHARED_DATA in client.my_grants_to(transfer["peer"]):
        threading.Thread(target=_auto_import, args=(client, item_id), daemon=True).start()


def _auto_import(client, item_id: str) -> None:
    try:
        result = import_item(item_id, automatic=True)
        client._emit({"type": "item", "item_id": item_id, "imported": result.get("ok"), "detail": result.get("detail")})
    except Exception as error:
        logger.info(f"automatic import of {item_id} did not run: {error}")


def import_item(item_id: str, automatic: bool = False) -> Dict[str, Any]:
    from core.tool_registry import registry
    from security.policy import origin_scope
    row = store.one("SELECT * FROM items WHERE item_id = ?", (item_id,))
    if not row:
        raise ValueError("no such received item")
    if row["imported_at"]:
        return {"ok": True, "detail": row["import_result"], "already": True}
    content = store.loads(row["body"], {})
    origin = "lan" if automatic else "ui"
    if row["item_type"] == "memory":
        call = ("remember_fact", {"fact": content.get("text", "")})
    elif row["item_type"] == "knowledge":
        folder = settings.received_folder() / "knowledge"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_stem(row['title'] or 'knowledge')}-{item_id[-8:]}.md"
        path.write_text(f"# {row['title'] or 'Shared knowledge'}\n\n{content.get('text', '')}\n", encoding="utf-8")
        call = ("ingest_company_document", {"path": str(path), "origin": "company"})
    elif row["item_type"] == "project":
        call = ("ingest_sync_bundle", {"path": content.get("path", "")})
    else:
        return {"ok": False, "detail": f"a {row['item_type']} item is kept for reading; there is nothing to import"}
    with origin_scope(origin):
        outcome = registry.execute(call[0], **call[1])
    output = outcome.output if outcome.success else None
    ok = outcome.success and not (isinstance(output, dict) and (output.get("ok") is False or output.get("error")))
    detail = (outcome.error if not outcome.success else
              (output.get("error") if isinstance(output, dict) and output.get("error") else f"imported with {call[0]}"))
    if ok:
        store.x("UPDATE items SET imported_at = ?, import_result = ? WHERE item_id = ?", (time.time(), detail, item_id))
        store.audit("item_imported", {"item_id": item_id, "tool": call[0], "automatic": automatic})
    else:
        store.x("UPDATE items SET import_result = ? WHERE item_id = ?", (f"not imported: {detail}", item_id))
    return {"ok": ok, "detail": detail, "result": output}


def _stem(text: str) -> str:
    import re
    return re.sub(r"[^\w\-]+", "_", text).strip("_")[:60] or "item"


def view(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["content"] = store.loads(row["body"], {})
    out.pop("body", None)
    return out
