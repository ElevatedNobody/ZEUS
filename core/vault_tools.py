"""
Tools for the secure file locker and the employee register.

These are ordinary registered tools, so they run through the same
classify → plan → route → execute → validate → auto-fix pipeline as everything
else. A UI-only vault would be a feature the assistant could not actually use.

Three rules run through all of them:

1. **Nothing claims to have worked when it did not.** A locked vault refuses
   with the reason and the fix. A missing encryption backend refuses rather
   than storing anything in the clear.

2. **Every action is audited, successful or not.** A refused unlock is exactly
   as worth recording as an accepted one - more so.

3. **The passphrase is never a tool argument.** Tool arguments are stringified
   into the security audit log by `registry.execute`, so a passphrase passed
   as one would be written to disk in plaintext. Unlocking is therefore done
   through `unlock_vault_interactive`, which prompts, and the model-callable
   tool only ever reports state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.logger import get_logger
from core.security_manager import RiskLevel, security_manager
from core.tool_registry import registry

logger = get_logger("vault.tools")


def _username() -> str:
    try:
        from auth.session_manager import session_manager
        return session_manager.username or ""
    except Exception:
        return ""


def _audit(action: str, subject: str = "", detail: str = "",
           succeeded: bool = True):
    try:
        from memory.database import init_db, log_vault_event
        init_db()
        log_vault_event(action, subject=subject, detail=detail,
                        succeeded=succeeded, username=_username())
    except Exception as error:                 # auditing must never break a call
        logger.warning(f"could not write the vault audit record: {error}")


# ================================================================ vault ====

@registry.register(
    "vault_status",
    "Report whether the secure file locker is set up, unlocked, and how many "
    "files it holds",
    RiskLevel.SAFE)
def vault_status_tool():
    from core.vault import vault
    report = vault.status().as_dict()
    if not report["configured"]:
        report["next_step"] = ("Say 'set up the vault' to choose a passphrase.")
    elif not report["unlocked"]:
        report["next_step"] = "Say 'unlock the vault' to open it."
    return report


@registry.register(
    "lock_vault",
    "Lock the secure file locker. Its contents become unreadable until the "
    "passphrase is entered again",
    RiskLevel.SAFE)
def lock_vault_tool():
    from core.vault import vault
    was_unlocked = vault.is_unlocked
    vault.lock()
    _audit("lock", detail="locked by request" if was_unlocked else "already locked")
    return {"locked": True,
            "detail": "The vault is locked." if was_unlocked
                      else "The vault was already locked."}


@registry.register(
    "list_vault_files",
    "List the files in the secure locker. Names only - opening a file needs "
    "the vault to be unlocked",
    RiskLevel.SAFE)
def list_vault_files_tool(category: str = ""):
    from core.vault import vault
    from memory.database import init_db, list_vault_files
    init_db()

    status = vault.status()
    if not status.configured:
        return {"error": "The vault has not been set up yet. Say 'set up the vault'."}

    records = list_vault_files(category=category.strip())
    if not records:
        return {"files": "The vault is empty." if not category
                         else f"Nothing in the vault under '{category}'.",
                "unlocked": status.unlocked}
    lines = []
    for record in records:
        size = record["size_bytes"]
        readable = (f"{size / (1024 ** 2):.1f} MB" if size >= 1024 ** 2
                    else f"{max(1, size // 1024)} KB")
        line = f"{record['display_name']}  ({readable})"
        if record.get("category"):
            line += f"  [{record['category']}]"
        lines.append(line)
    # The listing is metadata, so it works while locked. Say so, rather than
    # letting the operator think the vault is open.
    return {"files": lines, "count": len(records), "unlocked": status.unlocked,
            "note": ("" if status.unlocked else
                     "The vault is locked - these are names only. Unlock it to "
                     "open or export anything.")}


@registry.register(
    "add_file_to_vault",
    "Encrypt a file into the secure locker. The original file is left where "
    "it is",
    RiskLevel.MEDIUM_RISK,
    params={"path": "the file to store"},
    timeout=120.0)
def add_file_to_vault_tool(path: str, name: str = "", category: str = "",
                           note: str = ""):
    from core.vault import VaultError, VaultLocked, vault

    source = Path(path).expanduser()
    if not security_manager.validate_path(str(source)):
        _audit("add", subject=str(source), detail="blocked by security policy",
               succeeded=False)
        return {"error": f"Access to '{path}' is blocked by security policy."}
    try:
        record = vault.store(source, display_name=name.strip(),
                             category=category.strip(), note=note.strip())
    except VaultLocked as error:
        _audit("add", subject=str(source), detail="vault locked", succeeded=False)
        return {"error": str(error)}
    except VaultError as error:
        _audit("add", subject=str(source), detail=str(error), succeeded=False)
        return {"error": str(error)}

    _audit("add", subject=record["name"],
           detail=f"{record['size_bytes']} bytes from {source}")
    return {"stored": record["name"], "size_bytes": record["size_bytes"],
            "category": category.strip() or "uncategorised",
            "encrypted": True,
            "detail": (f"'{record['name']}' is encrypted in the vault. The "
                       f"original is still at {source}.")}


@registry.register(
    "open_vault_file",
    "Read a file out of the secure locker into a temporary copy and open it",
    RiskLevel.HIGH_RISK,
    params={"name": "the name of the file in the vault"},
    timeout=60.0)
def open_vault_file_tool(name: str):
    """HIGH_RISK on purpose: this puts decrypted content back onto the
    ordinary filesystem, where anything running as this user can read it.
    That deserves a confirmation prompt."""
    import os
    import tempfile

    from core.vault import VaultError, VaultLocked, vault
    from memory.database import find_vault_file, init_db
    init_db()

    record = find_vault_file(name.strip())
    if not record:
        _audit("open", subject=name, detail="no such file", succeeded=False)
        return {"error": f"There is nothing called '{name}' in the vault."}
    try:
        data = vault.read(record["file_id"])
    except VaultLocked as error:
        _audit("open", subject=record["display_name"], detail="vault locked",
               succeeded=False)
        return {"error": str(error)}
    except VaultError as error:
        _audit("open", subject=record["display_name"], detail=str(error),
               succeeded=False)
        return {"error": str(error)}

    folder = Path(tempfile.mkdtemp(prefix="medha_vault_"))
    target = folder / record["display_name"]
    target.write_bytes(data)
    _audit("open", subject=record["display_name"],
           detail=f"decrypted to a temporary copy at {target}")
    try:
        if os.name == "nt":
            os.startfile(str(target))       # noqa: S606 - the user asked for it
        opened = True
    except Exception as error:
        logger.debug(f"could not launch a viewer: {error}")
        opened = False
    return {"opened": record["display_name"], "temporary_copy": str(target),
            "launched": opened,
            "warning": ("This is a DECRYPTED copy outside the vault. Delete it "
                        "when you are finished with it.")}


@registry.register(
    "export_vault_file",
    "Write a decrypted copy of a vault file to a folder you choose",
    RiskLevel.HIGH_RISK,
    params={"name": "the name of the file in the vault",
            "destination": "where to write the decrypted copy"},
    timeout=120.0)
def export_vault_file_tool(name: str, destination: str):
    from core.vault import VaultError, VaultLocked, vault
    from memory.database import find_vault_file, init_db
    init_db()

    if not security_manager.validate_path(destination):
        _audit("export", subject=name, detail="blocked by security policy",
               succeeded=False)
        return {"error": f"Writing to '{destination}' is blocked by security policy."}
    record = find_vault_file(name.strip())
    if not record:
        _audit("export", subject=name, detail="no such file", succeeded=False)
        return {"error": f"There is nothing called '{name}' in the vault."}
    try:
        written = vault.export(record["file_id"], Path(destination))
    except VaultLocked as error:
        _audit("export", subject=record["display_name"], detail="vault locked",
               succeeded=False)
        return {"error": str(error)}
    except VaultError as error:
        _audit("export", subject=record["display_name"], detail=str(error),
               succeeded=False)
        return {"error": str(error)}

    _audit("export", subject=record["display_name"], detail=f"decrypted to {written}")
    return {"exported": record["display_name"], "saved": str(written),
            "warning": "That copy is not encrypted."}


@registry.register(
    "remove_vault_file",
    "Permanently delete a file from the secure locker",
    RiskLevel.HIGH_RISK,
    params={"name": "the name of the file in the vault"})
def remove_vault_file_tool(name: str):
    from core.vault import vault
    from memory.database import find_vault_file, init_db
    init_db()

    record = find_vault_file(name.strip())
    if not record:
        _audit("delete", subject=name, detail="no such file", succeeded=False)
        return {"error": f"There is nothing called '{name}' in the vault."}
    removed = vault.remove(record["file_id"])
    _audit("delete", subject=record["display_name"],
           detail="removed and overwritten" if removed else "delete failed",
           succeeded=removed)
    return ({"deleted": record["display_name"],
             "detail": "Gone. The encrypted data was overwritten before deletion."}
            if removed else {"error": "The file could not be removed."})


@registry.register(
    "vault_audit_log",
    "Show what has happened in the secure locker - what was added, opened, "
    "exported, and every refused unlock",
    RiskLevel.SAFE)
def vault_audit_log_tool(limit: int = 25):
    import time

    from memory.database import init_db, recent_vault_events
    init_db()
    events = recent_vault_events(limit=max(1, min(int(limit), 200)))
    if not events:
        return {"audit": "Nothing has happened in the vault yet."}
    lines = []
    for event in events:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(event["created_at"]))
        mark = "" if event["succeeded"] else "REFUSED "
        who = f" ({event['username']})" if event.get("username") else ""
        subject = f" {event['subject']}" if event.get("subject") else ""
        lines.append(f"{when}  {mark}{event['action']}{subject}{who}")
    return {"audit": lines, "count": len(lines)}


# ============================================================ employees ====

@registry.register(
    "add_employee",
    "Add someone to the employee register",
    RiskLevel.MEDIUM_RISK,
    params={"employee_id": "their works number or staff ID", "name": "full name"})
def add_employee_tool(employee_id: str, name: str, role: str = "",
                      department: str = "", email: str = "", phone: str = "",
                      location: str = "", started_on: str = "", notes: str = ""):
    from memory.database import add_employee, find_employee, init_db
    init_db()

    employee_id = employee_id.strip()
    name = name.strip()
    if not employee_id or not name:
        return {"error": "An employee record needs both a works number and a name."}
    if find_employee(employee_id):
        return {"error": f"There is already an employee with ID '{employee_id}'. "
                         f"Use 'update employee {employee_id}' to change them."}

    add_employee(employee_id=employee_id, name=name, role=role.strip(),
                 department=department.strip(), email=email.strip(),
                 phone=phone.strip(), location=location.strip(),
                 started_on=started_on.strip(), notes=notes.strip())
    _audit("employee_add", subject=f"{employee_id} {name}",
           detail=department.strip() or "no department")
    return {"added": name, "employee_id": employee_id,
            "department": department.strip() or "not set",
            "role": role.strip() or "not set"}


def _format_employee(person: dict, full: bool = False) -> str:
    line = f"{person['employee_id']}  {person['name']}"
    if person.get("role"):
        line += f"  - {person['role']}"
    if person.get("department"):
        line += f"  ({person['department']})"
    if full:
        for label, key in (("email", "email"), ("phone", "phone"),
                           ("location", "location"), ("started", "started_on")):
            if person.get(key):
                line += f"\n    {label}: {person[key]}"
        if person.get("notes"):
            line += f"\n    notes: {person['notes']}"
    return line


@registry.register(
    "list_employees",
    "List people in the employee register, optionally for one department",
    RiskLevel.LOW_RISK)
def list_employees_tool(department: str = "", include_inactive: bool = False):
    from memory.database import init_db, list_employees
    init_db()
    people = list_employees(department=department.strip(),
                            include_inactive=bool(include_inactive))
    if not people:
        return {"employees": (f"Nobody on record in '{department}'." if department
                              else "The employee register is empty.")}
    _audit("employee_list", subject=department.strip() or "all",
           detail=f"{len(people)} record(s)")
    return {"employees": [_format_employee(p) for p in people],
            "count": len(people)}


@registry.register(
    "find_employee",
    "Look up one person by name or works number",
    RiskLevel.LOW_RISK,
    params={"query": "a name or works number"})
def find_employee_tool(query: str):
    from memory.database import find_employee, init_db
    init_db()
    person = find_employee(query.strip())
    if not person:
        return {"error": f"Nobody on record matching '{query}'."}
    _audit("employee_lookup", subject=person["employee_id"])
    return {"employee": _format_employee(person, full=True),
            "employee_id": person["employee_id"], "name": person["name"],
            "department": person.get("department") or "not set",
            "role": person.get("role") or "not set"}


@registry.register(
    "update_employee",
    "Change an employee's details",
    RiskLevel.MEDIUM_RISK,
    params={"employee_id": "their works number or staff ID"})
def update_employee_tool(employee_id: str, name: str = "", role: str = "",
                         department: str = "", email: str = "", phone: str = "",
                         location: str = "", notes: str = ""):
    from memory.database import find_employee, init_db, update_employee
    init_db()

    person = find_employee(employee_id.strip())
    if not person:
        return {"error": f"Nobody on record with ID '{employee_id}'."}
    fields = {key: value.strip() for key, value in
              (("name", name), ("role", role), ("department", department),
               ("email", email), ("phone", phone), ("location", location),
               ("notes", notes)) if value.strip()}
    if not fields:
        return {"error": "Nothing to change - say what should be different."}
    update_employee(person["employee_id"], **fields)
    _audit("employee_update", subject=person["employee_id"],
           detail=", ".join(sorted(fields)))
    return {"updated": person["name"], "changed": sorted(fields)}


@registry.register(
    "remove_employee",
    "Remove someone from the employee register",
    RiskLevel.HIGH_RISK,
    params={"employee_id": "their works number or staff ID"})
def remove_employee_tool(employee_id: str):
    from memory.database import delete_employee, find_employee, init_db
    init_db()
    person = find_employee(employee_id.strip())
    if not person:
        return {"error": f"Nobody on record with ID '{employee_id}'."}
    removed = delete_employee(person["employee_id"])
    _audit("employee_delete", subject=person["employee_id"],
           detail=person["name"], succeeded=removed)
    return ({"removed": person["name"], "employee_id": person["employee_id"]}
            if removed else {"error": "The record could not be removed."})


@registry.register(
    "department_roster",
    "Show how many people are in each department",
    RiskLevel.LOW_RISK)
def department_roster_tool():
    from memory.database import init_db, list_employees
    init_db()
    people = list_employees()
    if not people:
        return {"roster": "The employee register is empty."}
    counts: dict = {}
    for person in people:
        counts[person.get("department") or "unassigned"] = \
            counts.get(person.get("department") or "unassigned", 0) + 1
    return {"roster": [f"{name}: {n}" for name, n in sorted(counts.items())],
            "total": len(people), "departments": len(counts)}
