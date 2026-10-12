"""
Remote tasks: device A asks device B to do something, B does it LOCALLY and
answers.   A -> server -> B -> local processing -> result -> server -> A

States (A only ever shows what B reported):
    PENDING    created on A, not yet received by B
    RECEIVED   B has it (B said so)
    RUNNING    B started it
    COMPLETED  B finished it - the result came back
    FAILED     B could not do it (the reason came back), or the server refused it
    CANCELLED  A cancelled it before B started

What B will do is a FIXED list - there is no "run this command" task, no shell,
no code, no desktop action. Each type needs RECEIVE_TASKS (B accepts tasks from
A at all) plus the grant its data needs, both checked on B. Everything runs
through ZEUS's policy engine with origin "lan" under the user signed in on B:
restricted data and machine actions are refused there by security/policy.py,
exactly as for any LAN request.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from core.logger import get_logger
from crudex_net import settings, sharing, store
from crudex_server import protocol as P

logger = get_logger("crudex.tasks")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crudex-task")
_MAX_RESULT = 150_000


def _ask(params: Dict[str, Any], sender: str) -> Dict[str, Any]:
    from core.model_router import model_router
    from core.ollama_client import is_failure_text, ollama_client
    question = str(params.get("question", "")).strip()
    if not question:
        raise ValueError("no question was sent")
    if not ollama_client.is_available(force=True):
        raise RuntimeError("the local model runtime (Ollama) is not running on this device")
    model = model_router.route("default")
    answer = ollama_client.chat(model, [
        {"role": "system", "content": "You are ZEUS, answering a question sent from the user's other device over "
                                      "the ZEUS NETWORK. Answer plainly and concisely. You cannot take actions."},
        {"role": "user", "content": question}], task_type="default", purpose="ZEUS NETWORK remote task")
    if not answer or is_failure_text(answer):
        raise RuntimeError(answer or "the local model returned nothing")
    return {"answer": answer, "model": model}


def _status(params: Dict[str, Any], sender: str) -> Dict[str, Any]:
    from core import branding
    from core.ollama_client import ollama_client
    from crudex_net import identity
    running = ollama_client.is_available(force=True)
    return {"crudex": branding.APP_VERSION, "device": identity.device_name(), "platform": identity._platform(),
            "local_model_runtime": "running" if running else "not running",
            "local_models": ollama_client.list_models() if running else []}


def _summarize_file(params: Dict[str, Any], sender: str) -> Dict[str, Any]:
    from pathlib import Path
    from industrial.doc_pipeline import ExtractionOptions, extract_document
    row = store.one("SELECT * FROM transfers WHERE transfer_id = ? AND direction = 'in' AND peer = ? AND "
                    "state IN ('received', 'delivered')", (str(params.get("transfer_id", "")), sender))
    if not row or not row.get("final_path") or not Path(row["final_path"]).is_file():
        raise ValueError("that file was not received from your device (or it was removed here)")
    document = extract_document(row["final_path"], ExtractionOptions(ocr=True, vision=False, save_assets=False))
    text = (document.text or "").strip()
    if not text:
        raise RuntimeError(document.error or "no readable text in the file")
    answer = _ask({"question": "Summarise this document in at most 10 bullet points, keeping every figure exact:\n\n"
                               + text[:12000]}, sender)
    return {"file": row["name"], "summary": answer["answer"], "model": answer["model"],
            "characters_read": min(len(text), 12000), "characters_total": len(text)}


def _registry(tool: str, **kwargs) -> Any:
    from core.tool_registry import registry
    from security.policy import origin_scope
    with origin_scope("lan"):
        outcome = registry.execute(tool, **kwargs)
    if not outcome.success:
        raise PermissionError(outcome.error or f"{tool} was refused on this device")
    output = outcome.output
    if isinstance(output, dict) and output.get("ok") is False:
        raise RuntimeError(output.get("error") or f"{tool} failed")
    return output


def _search_knowledge(params: Dict[str, Any], sender: str) -> Any:
    return _registry("search_company_documents", query=str(params.get("query", ""))[:500])


def _project_status(params: Dict[str, Any], sender: str) -> Any:
    return _registry("jarvis_overview", project=str(params.get("project", ""))[:120])


def _list_shared(params: Dict[str, Any], sender: str) -> Dict[str, Any]:
    policy = settings.sharing_policy()
    folders = store.q("SELECT name, folder_key FROM folders WHERE peer = ? AND enabled = 1", (sender,))
    shared = {"sharing_policy": policy, "synced_folders": folders}
    if policy.get("artifact") == settings.SHARED:
        from core import artifacts
        shared["artifacts"] = [{"id": a["id"], "name": a["name"], "format": a["format"]}
                               for a in artifacts.list_artifacts(30) if a.get("exists")]
    return shared


TASK_TYPES: Dict[str, Dict[str, Any]] = {
    "status": {"label": "Device status (version, local model runtime, models)", "grant": P.VIEW_STATUS,
               "params": [], "run": _status},
    "ask": {"label": "Ask this device's local model", "grant": P.RECEIVE_TASKS, "params": ["question"], "run": _ask},
    "summarize_file": {"label": "Summarise a file you sent this device", "grant": P.RECEIVE_TASKS,
                       "params": ["transfer_id"], "run": _summarize_file},
    "search_knowledge": {"label": "Search this device's company knowledge", "grant": P.READ_SHARED_KNOWLEDGE,
                         "params": ["query"], "run": _search_knowledge},
    "project_status": {"label": "Project health on this device", "grant": P.READ_SHARED_DATA,
                       "params": ["project"], "run": _project_status},
    "list_shared": {"label": "What this device shares with you", "grant": P.READ_SHARED_DATA, "params": [],
                    "run": _list_shared},
}


def catalogue() -> Dict[str, Dict[str, Any]]:
    return {name: {"label": t["label"], "grant": t["grant"], "params": t["params"]} for name, t in TASK_TYPES.items()}


# ----------------------------------------------------------------- A side --
def _record(task_id: str, **fields) -> None:
    row = store.one("SELECT history FROM tasks WHERE task_id = ?", (task_id,))
    if row is None:
        return
    history = store.loads(row["history"], [])
    if "state" in fields:
        history.append({"state": fields["state"], "at": time.time()})
    fields["history"] = json.dumps(history[-50:])
    fields["updated_at"] = time.time()
    names = ", ".join(f"{k} = ?" for k in fields)
    store.x(f"UPDATE tasks SET {names} WHERE task_id = ?", (*fields.values(), task_id))


def create(client, to_device: str, task_type: str, params: Dict[str, Any], confirmed: bool = False) -> Dict[str, Any]:
    if task_type not in TASK_TYPES:
        raise ValueError(f"'{task_type}' is not a task type (allowed: {', '.join(TASK_TYPES)})")
    sharing.check_category("task", confirmed)
    clean = {}
    for name in TASK_TYPES[task_type]["params"]:
        value = str((params or {}).get(name, "")).strip()
        if not value and name not in ("project",):
            raise ValueError(f"'{name}' is needed for a {task_type} task")
        clean[name] = value[:4000]
    if task_type == "ask":
        sharing.check_text(clean["question"], "question")
    task_id = str(uuid.uuid4())
    now = time.time()
    store.x("INSERT INTO tasks (task_id, peer, direction, task_type, params, state, created_at, updated_at, history) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (task_id, to_device, "out", task_type, json.dumps(clean), "PENDING", now, now,
                                          json.dumps([{"state": "PENDING", "at": now}])))
    client.queue_event(to_device, "task", {"task_id": task_id, "task_type": task_type, "params": clean,
                                           "sent_at": now}, msg_id=f"task-{task_id}")
    store.audit("task_sent", {"task_id": task_id, "to": to_device, "type": task_type})
    client._emit({"type": "task", "task_id": task_id, "state": "PENDING", "direction": "out"})
    return view(store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,)))


def mark_local(task_id: Optional[str], state: str, error: str = "") -> None:
    if task_id:
        _record(task_id, state=state, error=error or None)


def cancel(client, task_id: str) -> Dict[str, Any]:
    row = store.one("SELECT * FROM tasks WHERE task_id = ? AND direction = 'out'", (task_id,))
    if not row:
        raise ValueError("no such task sent from this device")
    if row["state"] in P.TASK_FINAL:
        return view(row)
    client.queue_event(row["peer"], "task_cancel", {"task_id": task_id}, msg_id=f"cancel-{task_id}")
    _record(task_id, cancel_requested=1)
    return view(store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,)))


def on_status(client, sender: str, body: Dict[str, Any]) -> None:
    task_id = str(body.get("task_id", ""))
    row = store.one("SELECT * FROM tasks WHERE task_id = ? AND direction = 'out' AND peer = ?", (task_id, sender))
    state = body.get("state")
    if not row or state not in P.TASK_STATES or row["state"] in P.TASK_FINAL:
        return
    fields = {"state": state}
    if "result" in body:
        fields["result"] = json.dumps(body["result"], default=str)[:_MAX_RESULT]
    if body.get("error"):
        fields["error"] = str(body["error"])[:2000]
    _record(task_id, **fields)
    client._emit({"type": "task", "task_id": task_id, "state": state, "direction": "out"})


# ----------------------------------------------------------------- B side --
def _report(client, row: Dict[str, Any], state: str, **extra) -> None:
    if "result" in extra:
        encoded = json.dumps(extra["result"], default=str)
        if len(encoded) > 200_000:
            extra["result"] = {"truncated": True, "note": "the result was longer than one event carries - the "
                                                          "first 200,000 characters follow", "text": encoded[:200_000]}
    _record(row["task_id"], state=state, **{k: (json.dumps(v, default=str)[:_MAX_RESULT] if k == "result" else v)
                                            for k, v in extra.items()})
    body = {"task_id": row["task_id"], "state": state, "at": time.time(), **extra}
    client.queue_event(row["peer"], "task_status", body, msg_id=f"status-{state.lower()}-{row['task_id']}")
    client._emit({"type": "task", "task_id": row["task_id"], "state": state, "direction": "in"})


def on_task(client, sender: str, sender_name: Optional[str], body: Dict[str, Any]) -> None:
    task_id = str(body.get("task_id", ""))
    if not task_id or store.one("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)):
        return
    task_type = str(body.get("task_type", ""))
    now = time.time()
    store.x("INSERT INTO tasks (task_id, peer, direction, task_type, params, state, created_at, updated_at, history) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (task_id, sender, "in", task_type, json.dumps(body.get("params") or {}),
                                          "RECEIVED", now, now, json.dumps([{"state": "RECEIVED", "at": now}])))
    store.audit("task_received", {"task_id": task_id, "from": sender, "type": task_type})
    row = store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    _report(client, row, "RECEIVED")
    _executor.submit(_execute, client, task_id)


def on_cancel(client, sender: str, body: Dict[str, Any]) -> None:
    row = store.one("SELECT * FROM tasks WHERE task_id = ? AND direction = 'in' AND peer = ?",
                    (str(body.get("task_id", "")), sender))
    if not row or row["state"] in P.TASK_FINAL:
        return
    if row["state"] == "RECEIVED":
        _report(client, row, "CANCELLED", error="cancelled by the device that sent it")
    else:
        _record(row["task_id"], cancel_requested=1)


def _execute(client, task_id: str) -> None:
    row = store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    if not row or row["state"] != "RECEIVED":
        return
    spec = TASK_TYPES.get(row["task_type"])
    if spec is None:
        return _report(client, row, "FAILED", error=f"'{row['task_type']}' is not a task this device runs")
    grants = client.my_grants_to(row["peer"])
    if not grants:
        try:
            client.refresh_devices()
            grants = client.my_grants_to(row["peer"])
        except Exception:
            pass
    missing = [g for g in {P.RECEIVE_TASKS, spec["grant"]} if g not in grants]
    if missing:
        return _report(client, row, "FAILED", error=f"not permitted on this device: {', '.join(sorted(missing))} "
                                                    f"is not granted to the sender")
    _report(client, row, "RUNNING")
    started = time.time()
    try:
        result = spec["run"](store.loads(row["params"], {}), row["peer"])
    except Exception as error:
        logger.info(f"remote task {task_id} failed: {error}")
        store.audit("task_failed", {"task_id": task_id, "error": str(error)[:300]})
        return _report(client, row, "FAILED", error=str(error)[:2000])
    store.audit("task_completed", {"task_id": task_id, "seconds": round(time.time() - started, 1)})
    _report(client, row, "COMPLETED", result=result)


def view(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    out = dict(row)
    out["params"] = store.loads(row["params"], {})
    out["result"] = store.loads(row["result"], row["result"]) if row.get("result") else None
    out["history"] = store.loads(row["history"], [])
    out["label"] = TASK_TYPES.get(row["task_type"], {}).get("label", row["task_type"])
    return out
