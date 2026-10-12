"""
The typed contract between the ZEUS interface (ui/web) and the backend.

The interface is DISPLAY, INTERACTION, INPUT and STATE. Everything else -
authentication, orchestration, models, agents, tools, the database, security,
RAG, artifacts, audit - stays in Python and is reached only through the methods
declared here. The web page cannot call anything that is not in this table,
and every argument is type-checked before a handler sees it.

Each method:
    args       name -> python type (str, int, float, bool, dict, list)
    auth       requires an authenticated, unlocked session
    thread     "worker" (long-running, answered asynchronously) or "main"
               (touches Qt - native dialogs - so runs on the interface thread)

Events pushed from Python to the page (bridge `event(kind, json)`):
    state          assistant state snapshot (IDLE ... LOCKED)
    level          live microphone loudness while LISTENING (0..1)
    approval       an approval request that needs the operator
    approval_done  an approval was decided (or timed out)
    security       a security event
    notification   a background notification (reminder, folder watcher)
    session        login / lock / unlock / logout
    lockdown       emergency lockdown engaged / released
    speech         speech output state (preparing / speaking / idle)
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

CONTRACT_VERSION = 1

# name: (args, auth, thread)
METHODS: Dict[str, Tuple[Dict[str, type], bool, str]] = {
    "hello":            ({}, False, "main"),
    "state":            ({}, False, "main"),
    "status":           ({}, True, "worker"),
    "submit":           ({"text": str}, True, "worker"),
    "listen":           ({}, True, "worker"),
    "stop_speaking":    ({}, True, "main"),
    "set_speak":        ({"enabled": bool}, True, "main"),
    "tool":             ({"name": str, "args": dict}, True, "worker"),
    "resolve_approval": ({"id": int, "approved": bool}, True, "main"),
    "lock":             ({}, True, "main"),
    "logout":           ({}, True, "main"),
    "unlock_vault":     ({}, True, "main"),
    "pick_file":        ({"purpose": str}, True, "main"),
    "pick_folder":      ({"purpose": str}, True, "main"),
    "open_path":        ({"path": str}, True, "worker"),
    "probe_network":    ({}, True, "worker"),
    "create_user":      ({}, True, "main"),
    "change_password":  ({}, True, "main"),
    "telemetry":        ({}, True, "worker"),
    "history":          ({}, True, "main"),

    # ---- ZEUS sign-in (auth/web_login.py). The ONLY methods, besides hello
    # and state, that work without a session. The page renders; Python decides.
    "auth_state":         ({}, False, "main"),
    "auth_setup":         ({"username": str, "password": str, "confirm": str}, False, "worker"),
    "auth_login":         ({"username": str, "password": str}, False, "worker"),
    "auth_unlock":        ({"password": str}, False, "worker"),
    "auth_face":          ({"purpose": str}, False, "main"),
    "auth_face_cancel":   ({}, False, "worker"),
    "auth_cancel_factor": ({}, False, "worker"),
    "auth_switch_user":   ({}, False, "worker"),
    "quit":               ({}, False, "main"),

    # ---- ZEUS workspace (signed in)
    "workspace":          ({}, True, "main"),
    "ask_company":        ({"question": str}, True, "worker"),
    "vault_open":         ({"passphrase": str, "confirm": str}, True, "worker"),
    "account_password":   ({"current": str, "new": str, "confirm": str}, True, "worker"),
    "account_create":     ({"username": str, "password": str, "confirm": str, "role": str}, True, "worker"),

    # ---- ZEUS NETWORK - Connection Mode in the user menu (crudex_net/service.py).
    # Each one asks security/policy.authorize() first. crudex_save_key is the ONLY
    # way an API key reaches Python: it goes straight to Windows Credential
    # Manager and is never returned, logged or audited (a masked form is).
    "crudex_state":       ({}, True, "worker"),
    "crudex_set_mode":    ({"mode": str}, True, "worker"),
    "crudex_save_key":    ({"api_key": str}, True, "worker"),
    "crudex_remove_key":  ({}, True, "worker"),
    "crudex_connect":     ({}, True, "worker"),
    "crudex_disconnect":  ({}, True, "worker"),
    "crudex_set_server":  ({"url": str, "ca_file": str}, True, "worker"),
}

# Events added with the ZEUS NETWORK:
#   crudex   connection state, deliveries, receipts, transfer progress, task
#            states, presence, pairing - never a secret

# Events added with the ZEUS interface:
#   auth_face    face sign-in progress (state, preview, result) - see auth/web_login.py
#   workspace    the workspace being built after sign-in: {step, state, detail}
#   analysis     company-intelligence stage events (QUERY ... READY), real, with agents


class ContractError(ValueError):
    pass


def validate(method: str, payload: Any) -> Dict[str, Any]:
    """Return the validated argument dict, or raise ContractError."""
    if method not in METHODS:
        raise ContractError(f"unknown method '{method}'")
    spec, _auth, _thread = METHODS[method]
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ContractError(f"{method}: arguments must be an object")
    unknown = set(payload) - set(spec)
    if unknown:
        raise ContractError(f"{method}: unexpected argument(s) {sorted(unknown)}")
    clean: Dict[str, Any] = {}
    for name, kind in spec.items():
        if name not in payload:
            raise ContractError(f"{method}: missing argument '{name}'")
        value = payload[name]
        if kind is int and isinstance(value, float) and value.is_integer():
            value = int(value)
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ContractError(f"{method}: '{name}' must be {kind.__name__}")
        if kind is str and len(value) > 20000:
            raise ContractError(f"{method}: '{name}' is too long")
        clean[name] = value
    return clean


def describe() -> Dict[str, Any]:
    return {"version": CONTRACT_VERSION,
            "methods": {name: {"args": {k: v.__name__ for k, v in spec.items()},
                               "auth": auth, "thread": thread}
                        for name, (spec, auth, thread) in METHODS.items()}}
