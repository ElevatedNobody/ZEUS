"""
The authorisation decision for every tool call.

    identity -> session state -> lockdown -> role -> data classification
    -> read / write / execute separation -> vault gate -> protected paths
    -> network policy for URLs -> approval requirement

The model never decides whether something is allowed. It can only ask for a
tool; this module answers, and `core.tool_registry.ToolRegistry.execute` will not
run anything this module refuses.

Roles (least to most privileged)
    viewer     reads public and internal information
    operator   + reads confidential information, writes work records, runs
                 harmless desktop actions
    engineer   + restricted data (employee register, vault) once the vault is
                 unlocked, document generation, code in the sandbox
    admin      + shell commands, user management, security configuration,
                 releasing a lockdown

Origins
    ui           the operator clicked a control that names this exact action
    command      the operator typed or spoke a request that routed to it
    planner      a MODEL proposed this step as part of a plan
    agent        an agent's own workflow step
    lan          a request from a paired LAN node
    system       internal housekeeping / tests

A model-proposed side effect (planner origin, write or execute) always needs a
human approval, whatever its risk level.
"""

from __future__ import annotations

import contextvars
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from core.logger import get_logger

logger = get_logger("security.policy")

ROLES = ("viewer", "operator", "engineer", "admin")
RANK = {name: index for index, name in enumerate(ROLES)}

READ, WRITE, EXECUTE = "read", "write", "execute"
PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED = "public", "internal", "confidential", "restricted"
CLASS_RANK = {PUBLIC: 0, INTERNAL: 1, CONFIDENTIAL: 2, RESTRICTED: 3}

ORIGINS = ("ui", "command", "planner", "agent", "lan", "system")

_origin: contextvars.ContextVar = contextvars.ContextVar("agni_origin", default="command")


def set_origin(origin: str):
    """Returns a token for `reset_origin`."""
    return _origin.set(origin if origin in ORIGINS else "command")


def reset_origin(token) -> None:
    try:
        _origin.reset(token)
    except Exception:
        pass


def current_origin() -> str:
    return _origin.get()


class origin_scope:
    """`with origin_scope("ui"): registry.execute(...)`"""

    def __init__(self, origin: str):
        self.origin = origin
        self._token = None

    def __enter__(self):
        self._token = set_origin(self.origin)
        return self

    def __exit__(self, *exc):
        reset_origin(self._token)
        return False


# ------------------------------------------------------ tool profiles ----

# Tools that act on the machine or run code.
_EXECUTE = {
    "run_command", "run_python_sandboxed", "open_application", "open_and_type",
    "type_text", "press_key", "hotkey", "mouse_move", "mouse_click",
    "close_application", "close_window", "focus_window", "minimize_window",
    "maximize_window", "media_control", "open_browser", "open_settings",
    "open_file_explorer", "lock_screen", "shutdown_pc", "restart_pc",
    "apply_wfp_firewall", "remove_wfp_firewall", "wfp_blocked_connections",
    "cancel_shutdown", "set_clipboard", "open_document", "open_vault_file",
    "take_screenshot", "read_screen", "coding_agent",
}
# Desktop actions harmless enough for an operator (still audited).
_OPERATOR_EXECUTE = {"open_application", "media_control", "focus_window",
                     "minimize_window", "maximize_window", "open_file_explorer",
                     "open_settings", "take_screenshot", "read_screen",
                     "open_document", "lock_screen", "cancel_shutdown"}

_WRITE_PREFIXES = ("add_", "update_", "delete_", "remove_", "write_", "create_",
                   "log_", "record_", "set_", "move_", "copy_", "convert_",
                   "ingest_", "index_", "forget_", "clear_", "approve_", "request_",
                   "schedule_", "raise_", "acknowledge_", "resolve_", "register_",
                   "seed_", "export_", "executive_", "complete_", "uncomplete_",
                   "remember_", "watch_", "remind_", "lock_", "cancel_", "stop_",
                   "rag_write", "update_stock", "spreadsheet_to_", "release_",
                   "process_meeting", "engage_", "disengage_", "pin_", "change_")
_WRITE_SUFFIXES = ("_report", "_presentation", "_workbook", "_pack", "_note",
                   "_docx", "_xlsx", "_pptx", "_pdf")

_RESTRICTED = {
    "add_file_to_vault", "open_vault_file", "export_vault_file", "remove_vault_file",
    "list_vault_files", "vault_audit_log", "add_employee", "list_employees",
    "find_employee", "update_employee", "remove_employee", "department_roster",
    "export_employees", "list_users", "set_user_role", "create_user",
    "disable_user", "disengage_lockdown", "set_network_mode", "pin_trusted_models",
    "vault_snapshot", "register_face", "verify_my_face", "remove_face",
}
_CONFIDENTIAL = {
    "process_meeting_audio", "process_meeting_transcript", "list_artifacts",
    "read_file", "search_files", "list_memories", "show_audit_log", "view_logs",
    "get_clipboard", "run_command", "run_python_sandboxed", "security_status",
    "security_events", "verify_audit_chain", "list_windows", "list_running_apps",
    "take_screenshot", "read_screen", "write_file", "copy_file", "move_file",
    "delete_file",
}
_PUBLIC = {"get_time", "get_cpu_usage", "get_memory_usage", "get_disk_usage",
           "get_battery", "get_system_info", "calculate", "engineering_calculate",
           "unit_convert", "list_agents", "list_specialists", "describe_agent",
           "validation_report", "pipeline_status", "explain_last_run",
           "capability_report", "model_routing", "hardware_profile",
           "industrial_term", "sovereignty_report", "agent_roster"}

# Needs the vault to be UNLOCKED, not merely configured.
_VAULT_GATED = {"add_file_to_vault", "open_vault_file", "export_vault_file",
                "remove_vault_file", "list_vault_files", "add_employee",
                "list_employees", "find_employee", "update_employee",
                "remove_employee", "department_roster", "export_employees",
                "vault_snapshot"}

_ADMIN_ONLY = {"apply_wfp_firewall", "remove_wfp_firewall", "wfp_blocked_connections",
               "run_command", "shutdown_pc", "restart_pc", "list_users",
               "set_user_role", "create_user", "disable_user",
               "disengage_lockdown", "set_network_mode", "pin_trusted_models",
               "clear_knowledge_base", "clear_industrial_knowledge_base",
               "seed_industrial_demo_data"}

# ZEUS company knowledge (knowledge_fabric/tools.py): names alone would not
# say who may change the corpus, so it is stated here - (access, minimum role).
_ROLE_OVERRIDES = {
    "ingest_company_document": (WRITE, "engineer"),
    "ingest_company_directory": (WRITE, "engineer"),
    "update_entity": (WRITE, "engineer"),
    "evaluate_company_rag": (READ, "engineer"),
    "remove_company_document": (WRITE, "admin"),
    "reindex_company_knowledge": (WRITE, "admin"),
    "reembed_company_knowledge": (WRITE, "admin"),
    "rebuild_knowledge_graph": (WRITE, "admin"),
    "set_user_departments": (WRITE, "admin"),
}

# Always allowed during a lockdown (read-only situational awareness + the
# controls needed to recover).
_LOCKDOWN_ALLOWED = {"security_status", "security_events", "verify_audit_chain",
                     "sovereignty_report", "get_time", "vault_status", "engage_lockdown",
                     "disengage_lockdown", "network_status", "lock_vault", "firewall_status",
                     "model_integrity_report", "pipeline_status",
                     "wfp_firewall_status", "apply_wfp_firewall", "face_status"}


@dataclass
class ToolProfile:
    access: str
    classification: str
    min_role: str
    vault_gated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"access": self.access, "classification": self.classification,
                "min_role": self.min_role, "vault_gated": self.vault_gated}


def profile_for(name: str, risk_level: int = 0) -> ToolProfile:
    """Classify a tool by what it does. `risk_level` is the RiskLevel int."""
    if name in _EXECUTE:
        access = EXECUTE
    elif name.startswith(_WRITE_PREFIXES) or name.endswith(_WRITE_SUFFIXES):
        access = WRITE
    else:
        access = READ

    if name in _RESTRICTED:
        classification = RESTRICTED
    elif name in _CONFIDENTIAL:
        classification = CONFIDENTIAL
    elif name in _PUBLIC:
        classification = PUBLIC
    else:
        classification = INTERNAL

    if name in _ADMIN_ONLY or risk_level >= 4:
        min_role = "admin"
    elif classification == RESTRICTED:
        min_role = "engineer"
    elif access == EXECUTE:
        min_role = "operator" if name in _OPERATOR_EXECUTE else "engineer"
    elif access == WRITE:
        min_role = "operator"
    elif classification == CONFIDENTIAL:
        min_role = "operator"
    else:
        min_role = "viewer"
    if name in _ROLE_OVERRIDES:
        access, stated = _ROLE_OVERRIDES[name]
        min_role = max(min_role, stated, key=ROLES.index)      # an override only ever raises the bar
    return ToolProfile(access=access, classification=classification,
                       min_role=min_role, vault_gated=name in _VAULT_GATED)


# ----------------------------------------------------------- identity ----

@dataclass
class Identity:
    username: str
    role: str
    authenticated: bool
    locked: bool = False
    system: bool = False


def test_mode() -> bool:
    """True only inside the pytest suite.

    It used to be the environment variable alone - so anyone who set
    AGNI_TEST_MODE=1 before starting ZEUS got an authenticated ADMIN identity
    with nobody signed in and no approvals: a developer bypass of login and
    policy. The variable now counts only when pytest is actually running, and
    main.py removes it (and records a security event) at startup."""
    import sys
    return os.environ.get("AGNI_TEST_MODE") == "1" and "pytest" in sys.modules


# The offline self-test (scripts/offline_test.py) runs from a terminal with no
# one signed in. Rather than switch authentication off for it, it runs as a
# fixed, least-privilege identity: role "viewer", so every write, execute and
# admin tool is still refused by the role check below, and every call it makes
# is audited under the name "diagnostics".
_diagnostic: contextvars.ContextVar = contextvars.ContextVar("agni_diagnostic", default=False)


class diagnostic_scope:
    """``with diagnostic_scope(): ...`` - read-only self-test identity."""

    def __enter__(self):
        self._token = _diagnostic.set(True)
        logger.warning("diagnostic identity in use (viewer role, read-only tools only)")
        return self

    def __exit__(self, *exc):
        _diagnostic.reset(self._token)
        return False


def current_identity() -> Identity:
    if _diagnostic.get():
        return Identity("diagnostics", "viewer", True, system=True)
    try:
        from auth.auth_manager import auth_manager
        from auth.session_manager import session_manager
    except Exception as error:                  # pragma: no cover - broken install
        logger.error(f"auth unavailable: {error}")
        return Identity("", "viewer", False)
    session = session_manager.session
    if session is not None:
        return Identity(session.username, getattr(session, "role", "") or
                        ("admin" if session.is_admin else "operator"),
                        authenticated=session_manager.is_authenticated,
                        locked=bool(session.locked))
    if test_mode() or not auth_manager.require_login:
        # No login configured (explicitly switched off) or the test suite:
        # the local system identity. Logged loudly at startup elsewhere.
        return Identity("system", "admin", True, system=True)
    return Identity("", "viewer", False)


# ----------------------------------------------------------- decision ----

@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    needs_approval: bool = False
    profile: Optional[ToolProfile] = None
    identity: Optional[Identity] = None
    origin: str = "command"
    notes: list = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "needs_approval": self.needs_approval, "origin": self.origin,
                "profile": self.profile.as_dict() if self.profile else None,
                "user": self.identity.username if self.identity else "",
                "role": self.identity.role if self.identity else ""}


_URL = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


def _deny(reason: str, profile, identity, origin) -> Decision:
    return Decision(False, reason, False, profile, identity, origin)


def authorize(tool_name: str, risk_level: int, arguments: Dict[str, Any],
              origin: Optional[str] = None) -> Decision:
    origin = origin or current_origin()
    profile = profile_for(tool_name, risk_level)
    identity = current_identity()

    # 1. an authenticated, unlocked session
    if not identity.authenticated:
        reason = ("ZEUS is locked - unlock it to continue" if identity.locked
                  else "No one is signed in - authentication is required")
        return _deny(reason, profile, identity, origin)

    # 2. emergency lockdown
    try:
        from security.lockdown import lockdown
        if lockdown.engaged and tool_name not in _LOCKDOWN_ALLOWED:
            return _deny("Emergency lockdown is engaged - only status and recovery "
                         "actions are available", profile, identity, origin)
    except Exception:
        pass

    # 3. role
    role = identity.role if identity.role in RANK else "viewer"
    if RANK[role] < RANK[profile.min_role]:
        return _deny(f"Your role ({role}) cannot run '{tool_name}' - it needs "
                     f"{profile.min_role} or above", profile, identity, origin)

    # 4. LAN peers never touch restricted data or execute anything
    if origin == "lan" and (profile.classification == RESTRICTED or profile.access == EXECUTE):
        return _deny("Requests from LAN nodes cannot use restricted data or run "
                     "actions on this machine", profile, identity, origin)

    # 5. the vault gate
    if profile.vault_gated:
        try:
            from core.vault import vault
            if not vault.is_unlocked:
                return _deny("The vault is locked - unlock it with your vault "
                             "passphrase to use restricted data", profile, identity, origin)
        except Exception as error:
            return _deny(f"The vault is unavailable: {error}", profile, identity, origin)

    # 6. protected paths
    from security import pathguard
    for key, value in pathguard.paths_in(arguments):
        # A destination for a decrypted vault export may be anywhere the user
        # chose EXCEPT inside ZEUS's own protected folders - same rule.
        ok, why = pathguard.check(value)
        if not ok:
            return _deny(f"Blocked '{key}': {why}", profile, identity, origin)

    # 7. URLs handed to tools obey the network policy
    for value in (arguments or {}).values():
        if isinstance(value, str) and _URL.match(value.strip()):
            try:
                from security.egress import egress_guard
                allowed, why = egress_guard.url_allowed(value.strip())
            except Exception:
                allowed, why = False, "network policy unavailable"
            if not allowed:
                return _deny(f"Network policy: {why}", profile, identity, origin)

    # 8. approval
    needs_approval = risk_level >= 3
    notes = []
    if origin == "planner" and profile.access in (WRITE, EXECUTE):
        needs_approval = True
        notes.append("a model proposed this step")
    if profile.access == EXECUTE and origin in ("command", "planner", "agent") and risk_level >= 2:
        needs_approval = True
    if profile.classification == RESTRICTED and profile.access == WRITE and tool_name.startswith(("export_", "remove_")):
        needs_approval = True
    if origin == "system" and identity.system:
        needs_approval = needs_approval and not test_mode()
    return Decision(True, "", needs_approval, profile, identity, origin, notes)
