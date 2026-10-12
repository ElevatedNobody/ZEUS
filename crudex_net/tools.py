"""
ZEUS NETWORK registered tools.

Every network action the interface or the command bar can take is a registered
tool, so it passes `core.tool_registry.ToolRegistry.execute` - the one policy
choke point (session, lockdown, role, classification, protected paths, approval,
audit chain). Names follow the existing classes, so security/policy.py needed no
change:

    crudex_*                                          READ   viewer and above
    create_ / register_ / write_ / set_ / remove_ /
    cancel_ / approve_ / request_                     WRITE  operator and above

A file path argument is called `path` / `folder`, so the protected-path rule
(no ZEUS database, vault, log, configuration or code may be sent) is applied
by the policy engine before the tool runs - and again inside crudex_net/sharing.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from core.security_manager import RiskLevel
from core.tool_registry import registry

logger = get_logger("crudex.tools")
SAFE, LOW = RiskLevel.SAFE, RiskLevel.LOW_RISK


def _guard(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from crudex_net.client import NetError
        from crudex_net.service import Refused
        from crudex_net.sharing import NeedsConfirmation
        try:
            return func(*args, **kwargs)
        except NeedsConfirmation as error:
            return {"ok": False, "needs_confirmation": True, "error": str(error)}
        except (Refused, PermissionError, NetError, ValueError, FileNotFoundError, KeyError, RuntimeError) as error:
            return {"ok": False, "error": str(error)}
    return wrapper


def tool(name: str, description: str, risk=SAFE, params: Optional[Dict[str, str]] = None, timeout: float = 60.0):
    def decorator(func):
        registry.register(name, description, risk, params=params or {}, timeout=timeout)(_guard(func))
        return func
    return decorator


def _flag(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _list(value) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


# ================================================================== READ =====

@tool("crudex_network_status", "ZEUS NETWORK: mode, connection state, server, pending events, last sync, "
      "and what works locally")
def crudex_network_status():
    from crudex_net import service
    return {"ok": True, **service.state()}


@tool("crudex_devices", "ZEUS NETWORK: my devices - paired or not, online state (from the server), last seen, "
      "platform, version and permissions both ways", timeout=45.0)
def crudex_devices():
    from crudex_net import service
    return service.devices()


@tool("crudex_messages", "ZEUS NETWORK: messages with one device (or all)", params={"device": "device id or name"})
def crudex_messages(device: str = ""):
    from crudex_net import service
    return service.messages(device)


@tool("crudex_transfers", "ZEUS NETWORK: file transfers - direction, bytes done, state, where a file was saved")
def crudex_transfers():
    from crudex_net import service
    return service.transfers()


@tool("crudex_remote_tasks", "ZEUS NETWORK: remote tasks sent and received, with their states and results")
def crudex_remote_tasks():
    from crudex_net import store, tasks
    rows = store.q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200")
    from crudex_net.client import client
    return {"ok": True, "tasks": [dict(tasks.view(r), peer_name=client.peer_name(r["peer"])) for r in rows],
            "types": tasks.catalogue()}


@tool("crudex_received_items", "ZEUS NETWORK: shared items received from other devices and whether they were imported")
def crudex_received_items():
    from crudex_net import items, store
    from crudex_net.client import client
    rows = store.q("SELECT * FROM items ORDER BY received_at DESC LIMIT 200")
    return {"ok": True, "items": [dict(items.view(r), peer_name=client.peer_name(r["peer"])) for r in rows]}


@tool("crudex_sharing_policy", "ZEUS NETWORK: what may leave this device (LOCAL ONLY / SHARED / ASK) per kind of data")
def crudex_sharing_policy():
    from crudex_net import settings
    return {"ok": True, "policy": settings.sharing_policy(), "categories": list(settings.SHARING_CATEGORIES),
            "choices": list(settings.SHARING_CHOICES)}


@tool("crudex_sync_folders", "ZEUS NETWORK: folders synchronised with other devices, and folder offers waiting")
def crudex_sync_folders():
    from crudex_net import store
    from crudex_net.client import client
    rows = store.q("SELECT * FROM folders WHERE state != 'removed' ORDER BY created_at DESC")
    return {"ok": True, "folders": [dict(r, peer_name=client.peer_name(r["peer"])) for r in rows]}


# ================================================================= WRITE =====

@tool("create_crudex_pairing_code", "ZEUS NETWORK: a one-time 6-digit code (10 minutes) to pair another device "
      "of this account", LOW)
def create_crudex_pairing_code():
    from crudex_net import service
    return service.pairing_code()


@tool("register_crudex_pairing", "ZEUS NETWORK: pair this device using a code shown on the other device", LOW,
      params={"code": "the 6-digit code"})
def register_crudex_pairing(code: str):
    from crudex_net import service
    return service.join(code)


@tool("remove_crudex_pairing", "ZEUS NETWORK: unpair a device (it stays registered but can no longer exchange "
      "anything with this one)", LOW, params={"device": "device id or name"})
def remove_crudex_pairing(device: str):
    from crudex_net import service
    return service.unpair(device)


@tool("remove_crudex_device", "ZEUS NETWORK: REVOKE a device - it can no longer connect, every pairing ends and "
      "nothing more is delivered to it", LOW, params={"device": "device id or name"})
def remove_crudex_device(device: str):
    from crudex_net import service
    return service.revoke(device)


@tool("set_crudex_permissions", "ZEUS NETWORK: what a paired device may do with this one (SEND_FILES, "
      "RECEIVE_FILES, READ_SHARED_DATA, WRITE_SHARED_DATA, SEND_TASKS, RECEIVE_TASKS, READ_SHARED_KNOWLEDGE, "
      "WRITE_SHARED_KNOWLEDGE, VIEW_STATUS)", LOW,
      params={"device": "device id or name", "grants": "the complete list of permissions to grant"})
def set_crudex_permissions(device: str, grants: Any = ""):
    from crudex_net import service
    return service.set_grants(device, _list(grants))


@tool("set_crudex_device_name", "ZEUS NETWORK: rename this device", LOW, params={"name": "the new name"})
def set_crudex_device_name(name: str):
    from crudex_net import service
    return service.rename(name)


@tool("write_crudex_message", "ZEUS NETWORK: send a message (or a notification) to a paired device - queued "
      "while offline", LOW, params={"device": "device id or name", "text": "the message",
                                    "notification": "true to show it as a notification"})
def write_crudex_message(device: str, text: str, notification: Any = False):
    from crudex_net import service
    return service.send_message(device, text, _flag(notification))


@tool("create_crudex_file_transfer", "ZEUS NETWORK: send a file of any type to a paired device (resumable, "
      "SHA-256 verified) - queued while offline", LOW,
      params={"device": "device id or name", "path": "the file", "confirmed": "true once the user confirmed (ASK)"},
      timeout=600.0)
def create_crudex_file_transfer(device: str, path: str, confirmed: Any = False):
    from crudex_net import service
    return service.send_file(device, path, _flag(confirmed))


@tool("create_crudex_artifact_transfer", "ZEUS NETWORK: send a generated artifact to a paired device", LOW,
      params={"device": "device id or name", "artifact_id": "artifact id", "confirmed": "true once confirmed"},
      timeout=600.0)
def create_crudex_artifact_transfer(device: str, artifact_id: Any, confirmed: Any = False):
    from crudex_net import service
    return service.send_artifact(device, int(artifact_id), _flag(confirmed))


@tool("create_crudex_project_transfer", "ZEUS NETWORK: send this device's project updates (a conflict-safe sync "
      "bundle) to a paired device", LOW, params={"device": "device id or name", "confirmed": "true once confirmed"},
      timeout=600.0)
def create_crudex_project_transfer(device: str, confirmed: Any = False):
    from crudex_net import service
    return service.send_project(device, _flag(confirmed))


@tool("cancel_crudex_transfer", "ZEUS NETWORK: cancel a file transfer", LOW, params={"transfer_id": "transfer id"})
def cancel_crudex_transfer(transfer_id: str):
    from crudex_net.client import client
    return client.cancel_transfer(transfer_id)


@tool("create_crudex_share", "ZEUS NETWORK: share an item (memory, knowledge, conversation, preferences, "
      "workspace) with a paired device", LOW,
      params={"device": "device id or name", "item_type": "memory|knowledge|conversation|preferences|workspace",
              "title": "a title", "content": "the text, or a JSON object/list", "confirmed": "true once confirmed"})
def create_crudex_share(device: str, item_type: str, content: Any, title: str = "", confirmed: Any = False):
    from crudex_net import items, service
    from crudex_net.client import client
    if isinstance(content, str) and content.strip()[:1] in ("{", "["):
        try:
            content = json.loads(content)
        except ValueError:
            pass
    if item_type in ("memory", "knowledge") and isinstance(content, str):
        content = {"text": content}
    return items.share(client, service.paired_device(device), item_type, title, content, _flag(confirmed))


@tool("approve_crudex_received_item", "ZEUS NETWORK: import a received item (memory, knowledge entry, project "
      "update) into this device - your role decides whether it is allowed", LOW, params={"item_id": "item id"},
      timeout=600.0)
def approve_crudex_received_item(item_id: str):
    from crudex_net import items
    return items.import_item(item_id, automatic=False)


@tool("create_crudex_remote_task", "ZEUS NETWORK: ask a paired device to do a task locally and send back the "
      "result (status, ask, summarize_file, search_knowledge, project_status, list_shared)", LOW,
      params={"device": "device id or name", "task_type": "the task type", "question": "for ask",
              "query": "for search_knowledge", "project": "for project_status", "transfer_id": "for summarize_file",
              "confirmed": "true once confirmed"})
def create_crudex_remote_task(device: str, task_type: str, question: str = "", query: str = "", project: str = "",
                              transfer_id: str = "", confirmed: Any = False):
    from crudex_net import service, tasks
    from crudex_net.client import client
    params = {"question": question, "query": query, "project": project, "transfer_id": transfer_id}
    return {"ok": True, "task": tasks.create(client, service.paired_device(device), task_type, params,
                                             _flag(confirmed))}


@tool("cancel_crudex_remote_task", "ZEUS NETWORK: cancel a remote task that has not started", LOW,
      params={"task_id": "task id"})
def cancel_crudex_remote_task(task_id: str):
    from crudex_net import tasks
    from crudex_net.client import client
    return {"ok": True, "task": tasks.cancel(client, task_id)}


@tool("set_crudex_sharing_policy", "ZEUS NETWORK: set LOCAL_ONLY, SHARED or ASK for one kind of data", LOW,
      params={"category": "conversation|task|artifact|document|knowledge|memory|preferences",
              "choice": "LOCAL_ONLY|SHARED|ASK"})
def set_crudex_sharing_policy(category: str, choice: str):
    from crudex_net import settings
    return {"ok": True, "policy": settings.set_sharing(category, choice.upper())}


@tool("create_crudex_folder_share", "ZEUS NETWORK: keep a folder synchronised with a paired device (both "
      "ways; conflicts keep both copies; deletions are not propagated)", LOW,
      params={"device": "device id or name", "folder": "the local folder", "name": "a name (optional)",
              "confirmed": "true once confirmed"}, timeout=600.0)
def create_crudex_folder_share(device: str, folder: str, name: str = "", confirmed: Any = False):
    from crudex_net import folders, service
    from crudex_net.client import client
    return folders.share_folder(client, service.paired_device(device), folder, name, _flag(confirmed))


@tool("approve_crudex_folder_share", "ZEUS NETWORK: accept a folder another device offered, into a local folder",
      LOW, params={"device": "device id or name", "folder_key": "the offered folder key", "folder": "local folder"},
      timeout=600.0)
def approve_crudex_folder_share(device: str, folder_key: str, folder: str):
    from crudex_net import folders, service
    from crudex_net.client import client
    return folders.accept(client, folder_key, service.resolve_device(device), folder)


@tool("request_crudex_folder_sync", "ZEUS NETWORK: synchronise a shared folder now", LOW,
      params={"device": "device id or name", "folder_key": "folder key"}, timeout=600.0)
def request_crudex_folder_sync(device: str, folder_key: str):
    from crudex_net import folders, service
    from crudex_net.client import client
    return folders.sync_now(client, folder_key, service.resolve_device(device))


@tool("remove_crudex_folder_share", "ZEUS NETWORK: stop synchronising a folder (no file is deleted)", LOW,
      params={"device": "device id or name", "folder_key": "folder key"})
def remove_crudex_folder_share(device: str, folder_key: str):
    from crudex_net import folders, service
    return {"ok": folders.remove(folder_key, service.resolve_device(device))}
