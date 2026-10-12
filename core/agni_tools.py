"""
ZEUS capabilities exposed as tools.

Registered into the SAME registry as every other tool, so each one passes the
policy engine (role, classification, vault gate, approvals), the audit log and
the timeout - whether it is reached from a typed command, a spoken one, a
button in the interface, or an agent.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from core.logger import get_logger
from core.security_manager import RiskLevel
from core.tool_registry import registry

logger = get_logger("agni.tools")


# ============================================================ engineering ==

@registry.register("engineering_calculate",
                   "Evaluate an engineering/maths expression exactly with SymPy and verify it "
                   "(e.g. 2*pi*sqrt(9.81*3), 12.5^2/4)", RiskLevel.SAFE,
                   params={"expression": "the expression to evaluate"}, timeout=30.0)
def engineering_calculate(expression: str):
    from industrial import engineering
    try:
        return engineering.evaluate(expression)
    except Exception as error:
        return {"error": f"could not evaluate: {error}"}


@registry.register("unit_convert", "Convert a value between units with a dimension check",
                   RiskLevel.SAFE, params={"value": "number", "from_unit": "e.g. bar",
                                           "to_unit": "e.g. psi"}, timeout=30.0)
def unit_convert(value, from_unit: str, to_unit: str):
    from industrial import engineering
    try:
        return engineering.convert(float(value), from_unit, to_unit)
    except Exception as error:
        return {"error": str(error)}


@registry.register("engineering_formula",
                   "Run a standard engineering calculation (pump_power, pressure_drop, corrosion, "
                   "wall_thickness, mawp, reynolds, heat_duty, lmtd, npsh_available, velocity, "
                   "head_from_pressure) with inputs like 'flow=50 m3/h, head=30 m'",
                   RiskLevel.SAFE, params={"formula": "formula name", "inputs": "name=value unit, ..."},
                   timeout=30.0)
def engineering_formula(formula: str, inputs: str = ""):
    from industrial import engineering
    try:
        return engineering.formula(formula, engineering.parse_inputs(inputs))
    except Exception as error:
        return {"error": str(error)}


@registry.register("solve_equation", "Solve an equation for one unknown and verify each root",
                   RiskLevel.SAFE, params={"equation": "left = right", "unknown": "variable"},
                   timeout=30.0)
def solve_equation(equation: str, unknown: str = "x"):
    from industrial import engineering
    try:
        return engineering.solve(equation, unknown)
    except Exception as error:
        return {"error": str(error)}


@registry.register("list_engineering_formulas", "List the deterministic engineering formulas",
                   RiskLevel.SAFE)
def list_engineering_formulas():
    from industrial import engineering
    return {"formulas": engineering.catalogue()}


# ================================================================ sandbox ==

@registry.register("run_python_sandboxed",
                   "Run a Python script in the ZEUS sandbox (separate process, no network, "
                   "memory and time limits) and return its output", RiskLevel.MEDIUM_RISK,
                   params={"code": "Python source"}, timeout=90.0)
def run_python_sandboxed(code: str, timeout: float = 30.0):
    from security.sandbox import run_python
    return run_python(code, timeout=min(float(timeout), 60.0))


# ================================================================= agents ==

_AGENT_ALIASES = {"inspection": "inspection", "document": "industrial_document",
                  "industrial_document": "industrial_document", "engineering": "engineering",
                  "coding": "industrial_coding", "code": "industrial_coding",
                  "industrial_coding": "industrial_coding", "calculation": "calculation",
                  "calculate": "calculation", "report": "report_approval",
                  "approval": "report_approval", "report_approval": "report_approval",
                  "research": "research", "planning": "planning", "plan": "planning",
                  "safety": "safety", "operations": "operations"}


@registry.register("run_industrial_agent",
                   "Run a specialist agent: inspection, document, engineering, coding, "
                   "calculation, report, research, planning, safety or operations",
                   RiskLevel.LOW_RISK,
                   params={"agent": "which agent", "task": "what to do"}, timeout=900.0)
def run_industrial_agent(agent: str, task: str = "", document_path: str = "",
                         equipment: str = "", output_format: str = "text",
                         create_tasks: bool = False):
    from industrial.agent_interfaces import AgentRequest, get_agent
    agent_id = _AGENT_ALIASES.get((agent or "").strip().lower().replace(" agent", ""))
    if not agent_id:
        return {"error": f"unknown agent '{agent}'", "agents": sorted(set(_AGENT_ALIASES.values()))}
    implementation = get_agent(agent_id)
    if implementation is None:
        return {"error": f"agent '{agent_id}' is not available"}
    if document_path:
        from security import pathguard
        allowed, why = pathguard.check(document_path)
        if not allowed:
            return {"error": why}
    request = AgentRequest(task=task, document_paths=[document_path] if document_path else [],
                           equipment=equipment or None, output_format=output_format or "text",
                           options={"create_tasks": bool(create_tasks)})
    from core.assistant_state import State, assistant_state
    with assistant_state.activity(State.EXECUTING, f"{implementation.name}"):
        result = implementation.run(request)
    data = result.to_dict()
    data["agent_name"] = implementation.name
    return data


# ============================================================== security ==

@registry.register("security_status", "Show the live security state: identity, role, network "
                   "policy, vault, lockdown, approvals, audit chain and recent events",
                   RiskLevel.SAFE)
def security_status():
    from security.approvals import approval_gateway
    from security.audit_chain import audit_chain
    from security.egress import egress_guard
    from security.lockdown import lockdown
    from security.policy import current_identity
    from security import events
    from core.vault import vault
    identity = current_identity()
    return {"user": identity.username, "role": identity.role,
            "network": egress_guard.status(), "lockdown": lockdown.status(),
            "vault": {"configured": vault.is_configured(), "unlocked": vault.is_unlocked},
            "approvals_pending": approval_gateway.pending(),
            "approval_history": approval_gateway.history(10),
            "audit_chain": audit_chain.verify(),
            "events_24h": events.totals_since(24 * 3600),
            "recent_events": events.recent(15)}


@registry.register("security_events", "List recent security events", RiskLevel.SAFE)
def security_events(limit: int = 50):
    from security import events
    return {"events": events.recent(int(limit))}


@registry.register("verify_audit_chain", "Verify the tamper-evident audit log end to end",
                   RiskLevel.SAFE)
def verify_audit_chain():
    from security.audit_chain import audit_chain
    return audit_chain.verify()


@registry.register("sovereignty_report", "The measured 'prove it' sovereignty report",
                   RiskLevel.SAFE, timeout=60.0)
def sovereignty_report():
    from security.sovereignty import report
    return report()


@registry.register("network_status", "Show the application network policy and egress counts",
                   RiskLevel.SAFE)
def network_status():
    from security.egress import egress_guard
    from security.firewall import status as firewall_status
    return {"application": egress_guard.status(), "os_firewall": firewall_status()}


@registry.register("set_network_mode", "Change the network mode: monitor, sovereign or lockdown",
                   RiskLevel.HIGH_RISK, params={"mode": "monitor | sovereign | lockdown"})
def set_network_mode(mode: str):
    from security.egress import egress_guard
    try:
        egress_guard.configure(mode=mode.strip().lower())
    except ValueError as error:
        return {"error": str(error)}
    return egress_guard.status()


@registry.register("engage_lockdown", "EMERGENCY LOCKDOWN: cut network to loopback, lock the "
                   "vault, refuse pending approvals, cancel tasks, release models",
                   RiskLevel.MEDIUM_RISK, params={"reason": "why"})
def engage_lockdown(reason: str = "operator request"):
    from security.lockdown import lockdown
    from auth.session_manager import session_manager
    return lockdown.engage(reason, by=session_manager.username or "")


@registry.register("disengage_lockdown", "Release the emergency lockdown (admin only)",
                   RiskLevel.HIGH_RISK)
def disengage_lockdown():
    from security.lockdown import lockdown
    from auth.session_manager import session_manager
    return lockdown.disengage(by=session_manager.username or "")


@registry.register("model_integrity_report", "Compare installed model digests with the trusted "
                   "registry", RiskLevel.SAFE, timeout=30.0)
def model_integrity_report():
    from security.integrity import model_integrity
    return model_integrity.report()


@registry.register("pin_trusted_models", "Pin the digests of the installed models as trusted "
                   "(admin only)", RiskLevel.HIGH_RISK, timeout=30.0)
def pin_trusted_models():
    from security.integrity import model_integrity
    from auth.session_manager import session_manager
    from core.model_router import model_router
    try:
        registry_data = model_integrity.pin(by=session_manager.username or "")
    except Exception as error:
        return {"error": str(error)}
    model_router.invalidate()
    return {"pinned": len(registry_data["models"]), "models": sorted(registry_data["models"])}


@registry.register("firewall_status", "Show whether the ZEUS Windows Firewall rules are applied",
                   RiskLevel.SAFE, timeout=30.0)
def firewall_status():
    from security.firewall import ALL_PREFIXES, status
    result = dict(status())
    try:
        from agni_ops.system import is_admin
        result["is_admin"] = bool(is_admin())
    except Exception:
        result["is_admin"] = None                 # not measured - said as such, never assumed
    result["rule_prefixes"] = list(ALL_PREFIXES)
    result["privileges"] = ("ZEUS is running with administrator rights" if result["is_admin"] else
                            "ADMIN PRIVILEGES REQUIRED to apply or remove rules - Windows asks for permission "
                            "(UAC) each time; reading the status needs none" if result["is_admin"] is False else
                            "administrator rights could not be determined")
    return result


@registry.register("list_users", "List local accounts and their roles (admin only)", RiskLevel.LOW_RISK)
def list_users():
    from memory import database
    return {"users": [{"username": u["username"], "display_name": u.get("display_name"),
                       "role": database.user_role(u), "active": bool(u.get("is_active")),
                       "last_login": u.get("last_login")} for u in database.list_users()]}


@registry.register("set_user_role", "Change an account's role (admin only)", RiskLevel.HIGH_RISK,
                   params={"username": "account", "role": "viewer | operator | engineer | admin"})
def set_user_role(username: str, role: str):
    from memory import database
    user = database.get_user(username)
    if not user:
        return {"error": f"no account '{username}'"}
    admins = [u for u in database.list_users() if database.user_role(u) == "admin" and u["is_active"]]
    if database.user_role(user) == "admin" and role != "admin" and len(admins) <= 1:
        return {"error": "that is the last administrator - add another admin first"}
    try:
        database.set_user_role(user["id"], role.strip().lower())
    except ValueError as error:
        return {"error": str(error)}
    database.log_auth_event("role_changed", username, f"-> {role}")
    return {"username": username, "role": role}


# ============================================================== hardware ==

@registry.register("hardware_profile", "Show this machine's CPU, RAM, GPU/VRAM and the model plan "
                   "chosen for it", RiskLevel.SAFE, timeout=30.0)
def hardware_profile():
    from core.hardware import model_plan, profile
    hw = profile(include_adapters=True)
    return {"hardware": hw.as_dict(), "plan": model_plan(hw)}


# ============================================================= artifacts ==

@registry.register("list_artifacts", "List generated artifacts with their provenance",
                   RiskLevel.SAFE)
def list_artifacts_tool(limit: int = 30):
    from core.artifacts import list_artifacts
    return {"artifacts": list_artifacts(int(limit))}


@registry.register("remove_artifact_record", "Remove a generated file from the artifact list (the file itself "
                   "is kept on disk)", RiskLevel.LOW_RISK, params={"artifact_id": "artifact id"})
def remove_artifact_record(artifact_id: str):
    from core.artifacts import forget_record
    try:
        removed = forget_record(int(artifact_id))
    except (TypeError, ValueError):
        return {"ok": False, "error": f"'{artifact_id}' is not an artifact id"}
    return {"ok": removed, "removed": removed, "file_kept": True} if removed else \
        {"ok": False, "error": f"no artifact {artifact_id} in your list"}


@registry.register("reveal_artifact", "Show a generated file in its folder (Windows Explorer)", RiskLevel.LOW_RISK,
                   params={"artifact_id": "artifact id"})
def reveal_artifact(artifact_id: str):
    import subprocess
    import sys as _sys
    from core.artifacts import get_artifact
    try:
        row = get_artifact(int(artifact_id))
    except (TypeError, ValueError):
        row = None
    if row is None:
        return {"ok": False, "error": f"no artifact {artifact_id} in your list"}
    if not row["exists"]:
        return {"ok": False, "error": f"the file no longer exists: {row['path']}"}
    from core.security_manager import security_manager
    if not security_manager.validate_path(row["path"]):
        return {"ok": False, "error": "that location is blocked by security policy"}
    if _sys.platform != "win32":
        return {"ok": False, "error": "revealing a file is only implemented for Windows Explorer"}
    subprocess.Popen(["explorer", "/select,", str(row["path"])])
    return {"ok": True, "revealed": row["path"]}


@registry.register("verify_artifact", "Re-verify a generated file: exists, opens, unchanged",
                   RiskLevel.SAFE, params={"path": "file path"})
def verify_artifact(path: str):
    from core.artifacts import verify
    return verify(path)


# ============================================================= telemetry ==

@registry.register("plant_telemetry", "Field telemetry: stored readings with their source label "
                   "(DEMO data is always labelled)", RiskLevel.SAFE)
def plant_telemetry():
    from industrial.telemetry import operations_overview, production_kpis, telemetry_snapshot
    return {"telemetry": telemetry_snapshot(), "production": production_kpis(),
            "operations": operations_overview()}


@registry.register("import_readings_csv", "Import real readings from a CSV export "
                   "(equipment_tag, parameter, value, unit, measured_at)", RiskLevel.MEDIUM_RISK,
                   params={"path": "CSV file", "source": "system the readings came from"},
                   timeout=120.0)
def import_readings_csv_tool(path: str, source: str):
    from industrial.telemetry import import_readings_csv
    return import_readings_csv(path, source)


# =============================================================== meetings ==

@registry.register("process_meeting_audio",
                   "Meeting intelligence: transcribe a meeting recording locally, then extract "
                   "summary, decisions and action items, create tasks, and write minutes",
                   RiskLevel.MEDIUM_RISK,
                   params={"audio_path": "WAV/MP3/M4A file", "title": "meeting title"},
                   timeout=1800.0)
def process_meeting_audio(audio_path: str, title: str = "", create_tasks: bool = True):
    from industrial.meetings import process_meeting
    return process_meeting(audio_path, title=title, create_tasks=bool(create_tasks))


@registry.register("process_meeting_transcript",
                   "Meeting intelligence from an existing transcript text file",
                   RiskLevel.MEDIUM_RISK, params={"transcript_path": "text file"}, timeout=900.0)
def process_meeting_transcript(transcript_path: str, title: str = "", create_tasks: bool = True):
    from industrial.meetings import process_transcript_file
    return process_transcript_file(transcript_path, title=title, create_tasks=bool(create_tasks))


# ====================================================== interface snapshots ==
# Structured rows for the interface panels. The older tools return sentences
# for the chat; the panels need the records themselves. Same policy, same audit.

@registry.register("productivity_snapshot", "Tasks, calendar entries and notes as records",
                   RiskLevel.SAFE)
def productivity_snapshot():
    from memory import database
    database.init_db()
    return {"todos": database.list_todos(include_completed=True),
            "events": database.list_events(),
            "notes": database.list_notes()}


@registry.register("vault_snapshot", "Vault files, the employee register and the vault audit "
                   "trail as records (vault must be unlocked)", RiskLevel.LOW_RISK)
def vault_snapshot():
    from memory import database
    from core.vault import vault
    database.init_db()
    return {"unlocked": vault.is_unlocked, "files": database.list_vault_files(),
            "employees": database.list_employees(include_inactive=True),
            "audit": database.recent_vault_events(limit=25)}


@registry.register("vault_overview", "Vault state and its audit trail (no file names while locked)",
                   RiskLevel.SAFE)
def vault_overview():
    from memory import database
    from core.vault import vault, backend_name
    database.init_db()
    return {"configured": vault.is_configured(), "unlocked": vault.is_unlocked,
            "cipher": backend_name(), "audit": database.recent_vault_events(limit=15)}


@registry.register("plant_snapshot", "Equipment, open alerts, permits, isolations, maintenance "
                   "due and recent inspections as records (DEMO rows labelled)", RiskLevel.SAFE,
                   timeout=30.0)
def plant_snapshot():
    from industrial import plant_data
    plant_data.init_plant_schema()
    return {"equipment": plant_data.list_equipment(limit=500),
            "alerts": plant_data.list_alerts(status="open", limit=100),
            "permits": plant_data.list_permits(limit=50),
            "isolations": plant_data.list_loto(active_only=True),
            "maintenance_due": plant_data.maintenance_due(within_days=30),
            "inspections": plant_data.inspection_history(limit=25),
            "sources": plant_data.data_sources()}


@registry.register("agent_roster", "The specialist agents with their workflows - fast, no probes",
                   RiskLevel.SAFE)
def agent_roster():
    from core.orchestration import agents as specialists
    from industrial.agent_interfaces import INDUSTRIAL_AGENTS
    return {"industrial": [agent.describe() for agent in INDUSTRIAL_AGENTS.values()],
            "specialists": specialists.describe_agents()}


# ================================================================== face ==
# InsightFace (third_party/insightface). The face is only ever a SECOND
# factor after the password; these tools act on the signed-in account only
# (an administrator may remove someone else's template).

def _signed_in():
    from auth.session_manager import session_manager
    session = session_manager.session
    if session is None or not session_manager.is_authenticated:
        raise PermissionError("No one is signed in")
    return session


def _face_frames(image_paths: str, count: int = 6) -> list:
    from security import face_auth, pathguard
    paths = [p.strip().strip("\"'") for p in (image_paths or "").split(",") if p.strip()]
    if not paths:
        return face_auth.capture_frames(count=count)
    frames = []
    for path in paths:
        allowed, why = pathguard.check(path)
        if not allowed:
            raise face_auth.FaceError(why)
        frames.append(face_auth.load_image(path))
    return frames


@registry.register("face_status", "Face login (InsightFace) status: model pack, liveness model, "
                   "enrolled accounts, login mode", RiskLevel.SAFE)
def face_status():
    from security import face_auth
    data = face_auth.status()
    try:
        data["your_face_enrolled"] = face_auth.has_template(_signed_in().user_id)
    except PermissionError:
        pass
    return data


@registry.register("register_face", "Enrol YOUR face for face login (the camera, or photos "
                   "given as comma-separated paths). Stores an encrypted template, never an image",
                   RiskLevel.MEDIUM_RISK,
                   params={"image_paths": "optional: photos instead of the camera"}, timeout=180.0)
def register_face(image_paths: str = ""):
    from security import face_auth
    session = _signed_in()
    try:
        frames = _face_frames(image_paths)
        return face_auth.enroll(session.user_id, session.username, frames, by=session.username)
    except face_auth.FaceError as error:
        return {"error": str(error)}


@registry.register("verify_my_face", "Test face verification for YOUR account (camera or photos)",
                   RiskLevel.SAFE, params={"image_paths": "optional: photos instead of the camera"},
                   timeout=180.0)
def verify_my_face(image_paths: str = ""):
    from security import face_auth
    session = _signed_in()
    try:
        frames = _face_frames(image_paths, count=4)
        return face_auth.verify(session.user_id, session.username, frames, purpose="test")
    except face_auth.FaceError as error:
        return {"error": str(error)}


@registry.register("remove_face", "Remove a face template (yours; an administrator may remove "
                   "another account's)", RiskLevel.HIGH_RISK,
                   params={"username": "optional: another account (admin only)"})
def remove_face(username: str = ""):
    from memory import database
    from security import face_auth
    from security.policy import current_identity
    session = _signed_in()
    target = (username or "").strip() or session.username
    if target != session.username and current_identity().role != "admin":
        return {"error": "Only an administrator can remove another account's face template."}
    user = database.get_user(target)
    if not user:
        return {"error": f"No account named '{target}'."}
    return face_auth.remove(user["id"], target, by=session.username)


# ============================================================ WFP firewall ==
# bin/agni-wfp.exe, built from the operator-supplied tailscale/wf library.

@registry.register("wfp_firewall_status", "WFP firewall (tailscale/wf) status - read only, "
                   "never asks for administrator rights", RiskLevel.SAFE)
def wfp_firewall_status():
    from security import wfp
    return wfp.status()


def _flag(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@registry.register("apply_wfp_firewall", "Install ZEUS's WFP filters: ZEUS's programs may reach "
                   "only this PC (and the LAN). Windows asks for administrator permission",
                   RiskLevel.HIGH_RISK,
                   params={"allow_lan": "true (default) | false",
                           "include_ollama": "false (default) | true - Ollama then cannot pull models"},
                   timeout=300.0)
def apply_wfp_firewall(allow_lan: str = "true", include_ollama: str = "false"):
    from security import events, wfp
    result = wfp.apply(allow_lan=_flag(allow_lan), include_ollama=_flag(include_ollama))
    if result.get("ok"):
        events.record("wfp_firewall_applied", f"{result.get('filters_added')} filters", severity="info")
    return result


@registry.register("remove_wfp_firewall", "Remove ZEUS's WFP filters (administrator permission "
                   "needed)", RiskLevel.HIGH_RISK, timeout=300.0)
def remove_wfp_firewall():
    from security import events, wfp
    result = wfp.remove()
    if result.get("ok"):
        events.record("wfp_firewall_removed", f"{result.get('removed_filters')} filters",
                      severity="warning")
    return result


@registry.register("wfp_blocked_connections", "Connections ZEUS's WFP filters blocked recently "
                   "(administrator permission needed to read them)", RiskLevel.MEDIUM_RISK,
                   params={"minutes": "look back this many minutes (default 60)"}, timeout=300.0)
def wfp_blocked_connections(minutes: str = "60"):
    from security import wfp
    try:
        span = max(1, min(int(float(minutes)), 7 * 24 * 60))
    except ValueError:
        span = 60
    return wfp.drops(span)


@registry.register("model_roles", "Which installed, trusted model serves each role (reasoning, fast, coding, "
                   "vision, extraction, document, small, embedding, reranker) on this hardware - nothing is "
                   "downloaded", RiskLevel.SAFE, params={"probe": "true = make a real call to each chat model"},
                   timeout=600.0)
def model_roles(probe: str = ""):
    from core import model_roles as roles
    return {"roles": roles.roster(probe=str(probe).lower() in ("1", "true", "yes"))}


def _verification_rows(records):
    from agni_ops import models
    rows = []
    for record in records:
        rows.append({"model": record.get("model"), "slot": record.get("slot"), "type": record.get("type"),
                     "status": record.get("status"), "tested_at": record.get("tested_at"),
                     "cause": record.get("cause") or "", "fallback": record.get("fallback") or "",
                     "points": models.verification_points(record)})
    return sorted(rows, key=lambda r: r["model"] or "")


@registry.register("whisper_status", "WHISPER STATUS: runtime, model files, model load, microphone and a real "
                   "transcription - each measured, with the actual reason when one fails", RiskLevel.SAFE,
                   params={"load": "true (default) loads the model and transcribes known speech; false = files only"},
                   timeout=600.0)
def whisper_status_tool(load: str = "true"):
    from agni_ops import voice
    lines = voice.whisper_status(load=str(load).lower() not in ("0", "false", "no"))
    return {"ok": not any(line["ok"] is False for line in lines), "checks": lines, "text": voice.render_status(lines)}


@registry.register("model_verification", "The eight recorded checks of every local model (installed, digest trusted, "
                   "memory room, loaded, answered, output validated, behaviour calibrated, unloaded + metrics) "
                   "from its last probe - nothing is loaded", RiskLevel.SAFE, timeout=30.0)
def model_verification():
    from agni_ops import models
    return {"ok": True, "models": _verification_rows(models.load_health().values()),
            "points": list(models.POINTS), "note": "run MODEL_PROBE.bat or 'Probe every chat model' to refresh"}


@registry.register("verify_models", "Probe every chat (and vision) model ONE AT A TIME: load, real call, output "
                   "validated by type, latency and memory measured, unloaded before the next; results saved",
                   RiskLevel.SAFE, params={"kind": "chat (default) or all"}, timeout=1800.0)
def verify_models(kind: str = "chat"):
    from agni_ops import models
    wanted = models.configured_models() if str(kind).lower() == "all" else models.chat_models()
    reports = models.run_matrix(wanted)
    return {"ok": True, "models": _verification_rows(r.to_dict() for r in reports),
            "points": list(models.POINTS), "tested": len(reports)}
