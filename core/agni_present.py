"""
Readable replies for ZEUS's structured tools.

A tool returns data; the operator reads text. This turns the dictionaries the
security, engineering, agent and telemetry tools return into short, honest
text - it only ever re-states what the tool returned, it never adds a claim.
Returns None for tools it does not know, so the caller's generic formatter
still applies.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

_MAX_ITEMS = 12


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _generic(output: Dict[str, Any], depth: int = 0) -> List[str]:
    """Nested data as indented lines, bounded so a big result stays readable."""
    lines: List[str] = []
    pad = "  " * depth
    for key, value in output.items():
        label = str(key).replace("_", " ")
        if isinstance(value, dict):
            if depth >= 2 or not value:
                lines.append(f"{pad}{label}: {len(value)} field(s)")
                continue
            lines.append(f"{pad}{label}:")
            lines.extend(_generic(value, depth + 1))
        elif isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{pad}{label}: none")
                continue
            lines.append(f"{pad}{label} ({len(value)}):")
            for item in list(value)[:_MAX_ITEMS]:
                if isinstance(item, dict):
                    compact = ", ".join(f"{k}={_scalar(v)}" for k, v in item.items()
                                        if not isinstance(v, (dict, list)))
                    lines.append(f"{pad}  - {compact[:220]}")
                else:
                    lines.append(f"{pad}  - {_scalar(item)[:220]}")
            if len(value) > _MAX_ITEMS:
                lines.append(f"{pad}  ... and {len(value) - _MAX_ITEMS} more")
        else:
            lines.append(f"{pad}{label}: {_scalar(value)}")
    return lines


def _sovereignty(out: Dict[str, Any]) -> str:
    lines = [f"Sovereign mode: {out.get('sovereign_mode', '?')} "
             f"(checked just now in {out.get('duration_ms', '?')} ms)"]
    for line in out.get("lines", []):
        mark = "PASS" if line.get("ok") else ("WARN" if line.get("ok") is None else "FAIL")
        lines.append(f"[{mark}] {line.get('label')}: {line.get('state')}"
                     + (f" - {line['detail']}" if line.get("detail") else ""))
    lines.append("Each line above is a check that was actually run; FAIL/WARN lines are the gaps.")
    return "\n".join(lines)


def _agent(out: Dict[str, Any]) -> str:
    name = out.get("agent_name") or out.get("agent_id", "agent")
    lines = [f"{name} - {out.get('status', '?')}"]
    if out.get("summary"):
        lines.append(str(out["summary"]))
    findings = out.get("findings") or []
    if findings:
        lines.append(f"Findings ({len(findings)}):")
        for f in findings[:_MAX_ITEMS]:
            ev = ", ".join(f.get("evidence_ids") or []) or "no evidence cited"
            lines.append(f"  - [{f.get('severity', '?')}] {f.get('title') or f.get('description', '')}"
                         f" ({ev})")
    recs = out.get("recommendations") or []
    if recs:
        lines.append(f"Recommendations ({len(recs)}):")
        for r in recs[:_MAX_ITEMS]:
            lines.append(f"  - [{r.get('priority', '?')}] {r.get('action', '')}")
    for path in out.get("artifacts") or []:
        lines.append(f"Saved: {path}")
    for warning in out.get("warnings") or []:
        lines.append(f"Note: {warning}")
    return "\n".join(lines)


def _convert(out: Dict[str, Any]) -> str:
    return f"{_scalar(out.get('value'))} {out.get('from')} = {_scalar(out.get('result'))} {out.get('to')}"


def _audit(out: Dict[str, Any]) -> str:
    ok = out.get("ok", out.get("intact"))
    head = ("Audit chain intact" if ok else "AUDIT CHAIN BROKEN") if ok is not None else "Audit chain"
    rest = [line for line in _generic({k: v for k, v in out.items() if k not in ("ok", "intact")})]
    return "\n".join([head] + rest)


def _company_answer(out: Dict[str, Any]) -> str:
    if not out.get("ok", True):
        return out.get("error", "")
    state = out.get("verification_state") or ("VALIDATED" if out.get("verified") else "FAILED")
    status = {"VERIFIED": "VERIFIED against company records",
              "VALIDATED": "VALIDATED against the evidence - not company records",
              "INSUFFICIENT_DATA": "INSUFFICIENT DATA - nothing you can access answers this"}.get(
        state, "NOT verified - " + ", ".join(out.get("validation", {}).get("unsupported_numbers", [])[:5]))
    lines = [out.get("answer", "").strip(), "",
             f"_{status} - analysis {out.get('analysis_id')}; answer written by {out.get('narrative_source')}._"]
    if out.get("note"):
        lines.append(f"_{out['note']}_")
    return "\n".join(lines)


def _company_artifact(out: Dict[str, Any]) -> str:
    if not out.get("ok"):
        return f"Could not create the file: {out.get('error')}"
    state = "verified" if out.get("verified") else "created but NOT verified"
    problems = []
    if not (out.get("file_check") or {}).get("ok"):
        problems.append((out.get("file_check") or {}).get("detail", "file check failed"))
    unsupported = (out.get("consistency") or {}).get("unsupported_numbers") or []
    if unsupported:
        problems.append("numbers not in the analysis: " + ", ".join(unsupported[:5]))
    return (f"{out.get('format', '').upper()} {state}: {out.get('path')}"
            + (f"\n{'; '.join(problems)}" if problems else "")
            + f"\nFrom analysis {out.get('analysis_id')}.")


_FILE_FACTS = (("slides", "slides"), ("rows", "rows"), ("records", "records"), ("pages", "pages"),
               ("days", "days"), ("sheets", "sheets"), ("words", "words"))


def _file_result(out: Dict[str, Any]) -> Optional[str]:
    """A tool that wrote a file returns everything it knows about it - path,
    bytes, verification, the whole analysis. The person needs the OUTCOME: what
    was made, whether it was checked, and where it is. Only what the tool
    returned is stated; a file no longer at its path is said to be missing."""
    path = out.get("saved")
    if not isinstance(path, str) or not os.path.splitext(path)[1]:
        return None
    fmt = str(out.get("format") or os.path.splitext(path)[1].lstrip(".")).upper()
    lines = [f"Produced {os.path.basename(path)} ({fmt})."]
    if out.get("verified") is True:
        lines.append("It was reopened and parsed before being listed.")
    elif out.get("verified") is False:
        lines.append("It did NOT pass its file check - see the log.")
    facts = [f"{out[key]} {label}" for key, label in _FILE_FACTS
             if isinstance(out.get(key), (int, float)) and not isinstance(out.get(key), bool)]
    if facts:
        lines.append("Contains " + ", ".join(facts) + ".")
    for key, label in (("slides_failed", "slide(s) could not be generated"),
                       ("days_failed", "day(s) could not be generated")):
        if isinstance(out.get(key), list) and out[key]:
            lines.append(f"{len(out[key])} {label}: {', '.join(map(str, out[key][:5]))}.")
    if out.get("narrative_included") is False:
        lines.append("The written narrative is not included - the facts are.")
    if out.get("demo_data") is True or out.get("demo") is True or out.get("data_source") == "demo":
        lines.append("Built from DEMO data.")
    lines.append(f"File saved to {path}" if os.path.isfile(path) else f"Expected at {path} - but no file is there now.")
    return "\n".join(lines)


def _knowledge_overview(out: Dict[str, Any]) -> str:
    if not out.get("ok", True):
        return out.get("error", "")
    lines = [f"The knowledge base holds {out.get('documents', 0)} document(s) you can see."]
    origins = ", ".join(f"{n} {o}" for o, n in (out.get("by_origin") or {}).items())
    if origins:
        lines.append(f"By origin: {origins}.")
    if out.get("datasets"):
        lines.append("Datasets: " + "; ".join(out["datasets"]) + ".")
    if out.get("procedures"):
        lines.append("Procedures: " + "; ".join(out["procedures"][:12]) + ("; ..." if len(out["procedures"]) > 12 else "") + ".")
    lines.append(out.get("next", ""))
    return "\n".join(line for line in lines if line)


_PRESENTERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "company_intelligence": _company_answer,
    "knowledge_overview": _knowledge_overview,
    "create_analysis_artifact": _company_artifact,
    "sovereignty_report": _sovereignty,
    "run_industrial_agent": _agent,
    "unit_convert": _convert,
    "verify_audit_chain": _audit,
}

_TITLES = {
    "security_status": "Security status",
    "security_events": "Security events",
    "network_status": "Network policy",
    "model_integrity_report": "Model integrity",
    "hardware_profile": "Hardware profile",
    "list_artifacts": "Generated artifacts",
    "verify_artifact": "Artifact verification",
    "plant_telemetry": "Plant telemetry",
    "engineering_calculate": "Calculation",
    "engineering_formula": "Engineering calculation",
    "solve_equation": "Equation",
    "list_engineering_formulas": "Engineering formulas",
    "run_python_sandboxed": "Sandbox run",
    "firewall_status": "Firewall",
    "list_users": "Accounts",
    "engage_lockdown": "Emergency lockdown",
    "disengage_lockdown": "Lockdown",
    "set_network_mode": "Network mode",
    "pin_trusted_models": "Trusted models",
    "agent_roster": "Agents",
    "model_routing": "Model routing",
    "pipeline_status": "Pipeline",
    "process_meeting_audio": "Meeting",
    "process_meeting_transcript": "Meeting",
    "import_readings_csv": "Readings import",
    "face_status": "Face login",
    "register_face": "Face enrolment",
    "verify_my_face": "Face check",
    "remove_face": "Face template",
    "wfp_firewall_status": "WFP firewall (tailscale/wf)",
    "apply_wfp_firewall": "WFP firewall",
    "remove_wfp_firewall": "WFP firewall",
    "wfp_blocked_connections": "Blocked connections",
}


def present(tool_name: str, output: Any) -> Optional[str]:
    if not isinstance(output, dict) or "error" in output:
        return None
    special = _PRESENTERS.get(tool_name)
    if special:
        try:
            return special(output)
        except Exception:
            pass
    produced = _file_result(output)
    if produced:
        return produced
    title = _TITLES.get(tool_name)
    if not title:
        return None
    lead = [title]
    for key in ("summary", "answer", "text", "message"):
        if isinstance(output.get(key), str) and output[key].strip():
            lead.append(output[key].strip())
            output = {k: v for k, v in output.items() if k != key}
            break
    return "\n".join(lead + _generic(output))
