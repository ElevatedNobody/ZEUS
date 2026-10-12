"""
Specialist agents.

An agent here is not a second assistant with its own prompt and its own copy
of everything. It is a *responsibility boundary*: a named specialist that owns
a set of the existing registry tools, declares which model slot it needs (many
need none), and says which validator should check what it produces.

That distinction is the whole point. Every agent shares one tool registry, one
database, one memory, one document generator, one knowledge base. Adding an
agent adds routing and accountability, not a duplicate stack.

This module EXTENDS the existing `core.agents.AGENT_REGISTRY` rather than
replacing it: the entries that were already there keep their ids, purposes and
tool lists, so anything that asked "which agent owns take_screenshot" still
gets the same answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.agents import AGENT_REGISTRY, AgentSpec
from core.logger import get_logger
from core.orchestration.contracts import TaskKind

logger = get_logger("orchestration.agents")


@dataclass
class Specialist:
    """One responsibility boundary."""

    id: str
    name: str
    purpose: str
    model_slot: str                     # "" means this agent needs no model
    kinds: List[TaskKind] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    validator_kind: str = ""
    deterministic: bool = False
    # Set only on agents that OWN a domain. The orchestrator uses it to
    # relabel a request the keyword classifier called 'chat' when a route
    # actually resolved to a domain tool. Cross-cutting agents (system,
    # productivity, file) leave it unset, because "what's the time" running
    # through the System Agent does not make the request industrial data.
    primary_kind: Optional[TaskKind] = None

    def owns(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def as_dict(self) -> Dict[str, object]:
        return {"id": self.id, "name": self.name, "purpose": self.purpose,
                "model_slot": self.model_slot or "none",
                "kinds": [k.value for k in self.kinds],
                "tool_count": len(self.tools),
                "validator": self.validator_kind or "none",
                "deterministic": self.deterministic}


# ---------------------------------------------------------- the roster ----

SPECIALISTS: Dict[str, Specialist] = {
    "operations": Specialist(
        id="operations", name="Operations Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Plant-wide status: availability, what is running, what is open.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["plant_status", "monitoring_status", "equipment_trend",
               "list_equipment", "equipment_details", "register_equipment",
               "plant_data_sources", "seed_industrial_demo_data"],
        validator_kind="data"),

    "maintenance": Specialist(
        id="maintenance", name="Maintenance Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Work history, scheduling and what is due - reading and writing "
                "the maintenance record.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["maintenance_history", "log_maintenance", "schedule_maintenance",
               "maintenance_due"],
        validator_kind="data"),

    "inspection": Specialist(
        id="inspection", name="Inspection Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Inspection records, and reading an inspection report end to end: "
                "OCR, findings, the governing procedure, then an approval note.",
        model_slot="vision",
        kinds=[TaskKind.INDUSTRIAL_DATA, TaskKind.OCR, TaskKind.VISION],
        tools=["inspection_history", "log_inspection", "approval_note",
               "extract_industrial_document", "classify_industrial_document"],
        validator_kind="data"),

    "safety": Specialist(
        id="safety", name="Safety Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Safety position, permits to work and isolations. Never approves "
                "anything itself - approval is a human act.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["safety_status", "find_sop", "list_permits", "request_permit",
               "approve_permit", "list_isolations", "record_isolation",
               "set_isolation_state", "safety_report"],
        validator_kind="data"),

    "inventory": Specialist(
        id="inventory", name="Inventory Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Spares, stock levels and whether a repair can actually be made.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["inventory_summary", "find_part", "update_stock"],
        validator_kind="data"),

    "alerts": Specialist(
        id="alerts", name="Alert Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Open conditions: raising, acknowledging and closing alerts.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["list_plant_alerts", "raise_plant_alert", "acknowledge_alert",
               "resolve_alert"],
        validator_kind="data"),

    "report": Specialist(
        id="report", name="Report Agent",
        purpose="Turns one analysis into consistent deliverables - Word, Excel and "
                "PowerPoint that cannot disagree with each other.",
        model_slot="json_structured",
        kinds=[TaskKind.WORD, TaskKind.EXCEL, TaskKind.POWERPOINT],
        # safety_report is owned by the Safety Agent, not duplicated here:
        # one tool, one owner, or two agents start to drift apart.
        tools=["executive_report", "executive_workbook", "executive_presentation",
               "executive_briefing_pack", "maintenance_report",
               "operations_presentation", "equipment_presentation",
               "export_plant_data", "list_plant_datasets"],
        validator_kind=""),          # set per step from the file extension

    "document": Specialist(
        id="document", name="Document Agent",
        purpose="Free-form document generation: write a report, a deck or a "
                "spreadsheet on a topic the database does not already hold.",
        model_slot="json_structured",
        kinds=[TaskKind.WORD, TaskKind.EXCEL, TaskKind.POWERPOINT],
        tools=["write_document", "create_presentation", "create_spreadsheet",
               "list_generated_documents", "find_generated_document",
               "open_document"],
        validator_kind=""),

    "data_analysis": Specialist(
        id="data_analysis", name="Data Analysis Agent",
        primary_kind=TaskKind.DOCUMENT_READ,
        purpose="Profiles a spreadsheet or dataset - header detection, column "
                "profiling, concentration and outliers - before anything is written.",
        model_slot="json_structured",
        kinds=[TaskKind.DOCUMENT_READ, TaskKind.EXCEL],
        tools=["analyze_spreadsheet", "spreadsheet_to_presentation",
               "spreadsheet_to_report"],
        validator_kind="data"),

    "knowledge": Specialist(
        id="knowledge", name="Industrial Knowledge Agent",
        primary_kind=TaskKind.RAG,
        purpose="Answers from indexed documents only, with the source named. "
                "Says the shelf is empty rather than inventing a procedure.",
        model_slot="reasoning",
        kinds=[TaskKind.RAG],
        tools=["ask_industrial_kb", "search_industrial_evidence",
               "list_industrial_documents", "ingest_industrial_document",
               "ingest_industrial_folder", "ingest_industrial_knowledge_corpus",
               "forget_industrial_document", "clear_industrial_knowledge_base",
               "index_folder", "ask_knowledge_base", "list_knowledge_sources",
               "forget_document", "clear_knowledge_base", "industrial_term"],
        validator_kind="rag"),

    "vision": Specialist(
        id="vision", name="Vision Agent",
        primary_kind=TaskKind.VISION,
        purpose="Reads what is on screen or in an image - text first via OCR, "
                "visual reasoning only when the picture itself is the question.",
        model_slot="vision",
        kinds=[TaskKind.VISION, TaskKind.OCR],
        tools=["take_screenshot", "read_screen", "analyze_image"],
        validator_kind="ocr"),

    "engineering": Specialist(
        id="engineering", name="Engineering Agent",
        purpose="Engineering calculations and unit work, done arithmetically "
                "rather than by asking a model to do sums.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.REASONING],
        tools=["calculate", "engineering_calculate", "unit_convert", "engineering_formula",
               "solve_equation", "list_engineering_formulas"],
        validator_kind="data"),

    "coding": Specialist(
        id="coding", name="Coding Agent",
        primary_kind=TaskKind.CODE,
        purpose="Writes and repairs code, and does not claim it works until it "
                "has at least been parsed.",
        model_slot="code",
        kinds=[TaskKind.CODE],
        tools=[],
        validator_kind="code"),

    "file": Specialist(
        id="file", name="File Agent",
        purpose="Filesystem work and format conversion, with originals preserved "
                "by default.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.FILE_OPS, TaskKind.CONVERSION, TaskKind.PDF,
               TaskKind.DOCUMENT_READ],
        tools=["search_files", "read_file", "write_file", "copy_file", "move_file",
               "create_folder", "delete_file", "convert_file"],
        validator_kind="file"),

    "automation": Specialist(
        id="automation", name="Automation Agent",
        primary_kind=TaskKind.AUTOMATION,
        purpose="Desktop control - applications, windows, input, screen lock - "
                "with confirmation on anything destructive.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.AUTOMATION],
        tools=["open_application", "close_application", "list_windows",
               "focus_window", "list_running_apps", "minimize_window",
               "maximize_window", "close_window", "type_text", "press_key",
               "hotkey", "mouse_move", "mouse_click", "get_clipboard",
               "set_clipboard", "media_control", "open_browser",
               "open_file_explorer", "open_settings", "open_and_type",
               "lock_screen", "shutdown_pc", "restart_pc", "cancel_shutdown",
               "run_command"],
        validator_kind="data"),

    "memory": Specialist(
        id="memory", name="Memory Agent",
        purpose="What ZEUS keeps about the user and the work - remember, recall "
                "and forget.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.MEMORY],
        tools=["remember_fact", "list_memories", "forget_fact"],
        validator_kind="data"),

    "system": Specialist(
        id="system", name="System Agent",
        purpose="Machine health, model availability, diagnostics and the audit "
                "trail.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["get_system_info", "get_cpu_usage", "get_memory_usage",
               "get_disk_usage", "get_battery", "get_time", "show_audit_log",
               "run_diagnostics", "system_information", "view_logs",
               "industrial_status", "industrial_model_health",
               "list_industrial_agents", "industrial_agent_status",
               # orchestration introspection - structured data, never prose
               "pipeline_status", "model_routing", "list_specialists", "list_agents",
               "describe_agent", "explain_last_run", "capability_report",
               "release_models", "validation_report", "knowledge_stats",
               "index_file", "index_my_documents"],
        validator_kind="data"),

    "productivity": Specialist(
        id="productivity", name="Productivity Agent",
        purpose="Tasks, deadlines, the calendar, meetings, notes and schedules.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.MEMORY],
        tools=["add_todo", "list_todos", "complete_todo", "uncomplete_todo",
               "delete_todo", "update_todo", "overdue_tasks",
               "add_event", "list_events", "delete_event",
               "schedule_meeting", "list_meetings", "agenda_for",
               "add_note", "list_notes", "update_note", "delete_note",
               "create_schedule", "update_schedule"],
        validator_kind="data"),

    "vault": Specialist(
        id="vault", name="Secure Locker Agent",
        purpose="The encrypted file locker: storing, listing, opening, "
                "exporting and deleting confidential files, and its audit trail.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.FILE_OPS],
        tools=["vault_status", "lock_vault", "list_vault_files",
               "add_file_to_vault", "open_vault_file", "export_vault_file",
               "remove_vault_file", "vault_audit_log"],
        validator_kind="data"),

    "people": Specialist(
        id="people", name="Employee Register Agent",
        purpose="The employee register: who works here, in which department, "
                "and how to reach them.",
        model_slot="", deterministic=True,
        kinds=[TaskKind.MEMORY],
        tools=["add_employee", "list_employees", "find_employee",
               "update_employee", "remove_employee", "department_roster"],
        validator_kind="data"),

    "security": Specialist(
        id="security", name="Security Agent",
        purpose="Reports and changes ZEUS's security state - network policy, lockdown, "
                "audit chain, model integrity, accounts. Code enforces every rule; this "
                "agent only reads or requests.",
        model_slot="", deterministic=True,
        kinds=[],
        tools=["security_status", "security_events", "verify_audit_chain",
               "sovereignty_report", "network_status", "set_network_mode",
               "engage_lockdown", "disengage_lockdown", "model_integrity_report",
               "pin_trusted_models", "firewall_status", "list_users", "set_user_role",
               "face_status", "register_face", "verify_my_face", "remove_face",
               "wfp_firewall_status", "apply_wfp_firewall", "remove_wfp_firewall",
               "wfp_blocked_connections"],
        validator_kind="data"),

    "industrial_agents": Specialist(
        id="industrial_agents", name="Industrial Agent Runner",
        purpose="Runs the ten evidence-grounded industrial agents (inspection, document, "
                "engineering, coding, calculation, report/approval, research, planning, "
                "safety, operations).",
        model_slot="", deterministic=False,
        kinds=[],
        tools=["run_industrial_agent", "run_python_sandboxed", "agent_roster",
               "hardware_profile", "list_artifacts", "verify_artifact",
               "plant_telemetry", "import_readings_csv", "plant_snapshot",
               "process_meeting_audio", "process_meeting_transcript",
               "productivity_snapshot", "vault_snapshot", "vault_overview"],
        validator_kind="data"),

    # ---- ZEUS company intelligence (knowledge_fabric). Each owns a stage of
    # knowledge_fabric/service.py - the stage events name it - and the tools
    # that expose that stage. Added alongside the agents above, none replaced.
    "company_retrieval": Specialist(
        id="company_retrieval", name="Retrieval Agent",
        purpose="Security-aware hybrid retrieval over the company records: full text, vectors, entities, "
                "graph and table facts, access-filtered in the query itself; the knowledge base's upkeep.",
        model_slot="", deterministic=True, kinds=[TaskKind.RAG],
        tools=["search_company_documents", "show_evidence", "document_provenance", "knowledge_fabric_status",
               "ingest_company_document", "ingest_company_directory", "remove_company_document",
               "reindex_company_knowledge", "reembed_company_knowledge", "set_user_departments"],
        validator_kind="data"),
    "temporal": Specialist(
        id="temporal", name="Temporal Agent",
        purpose="Years, fiscal years, quarters and relative periods; the 1989-2026 coverage and timeline.",
        model_slot="", deterministic=True, kinds=[],
        tools=["company_timeline", "knowledge_coverage"], validator_kind="data"),
    "entity": Specialist(
        id="entity", name="Entity Agent",
        purpose="One id per real unit, asset, site or organisation; evidence-backed aliases; ambiguities "
                "for a person to settle.",
        model_slot="", deterministic=True, kinds=[],
        tools=["ambiguous_entities", "update_entity"], validator_kind="data"),
    "graph": Specialist(
        id="graph", name="Graph Agent",
        purpose="Typed relations with their evidence (failed due to, caused, supplied by ...).",
        model_slot="", deterministic=True, kinds=[],
        tools=["rebuild_knowledge_graph"], validator_kind="data"),
    "analysis": Specialist(
        id="analysis", name="Analysis Agent",
        purpose="Runs the company-intelligence pipeline; every number from the analytics engine.",
        model_slot="reasoning", deterministic=False, kinds=[TaskKind.RAG],
        tools=["company_intelligence", "list_analyses", "get_analysis"], validator_kind="text"),
    "financial_analysis": Specialist(
        id="financial_analysis", name="Financial Analysis Agent",
        purpose="Series, totals, change, CAGR, year-on-year, trend, correlation and scenarios over the "
                "company's financial facts - calculated, never estimated.",
        model_slot="", deterministic=True, kinds=[], tools=[], validator_kind="data"),
    "company_document": Specialist(
        id="company_document", name="Document Agent",
        purpose="Reads documents into hierarchy, tables, cells and facts with their dates, authority and "
                "classification.",
        model_slot="", deterministic=True, kinds=[], tools=["compare_document_revisions"], validator_kind="data"),
    "recommendation": Specialist(
        id="recommendation", name="Recommendation Agent",
        purpose="Operational recommendations from the practice library, attached only to drivers the "
                "evidence supports (action, owner, timeline, dependencies, cost, impact, risk, KPI, data, "
                "success condition, monitoring).",
        model_slot="", deterministic=True, kinds=[], tools=[], validator_kind="data"),
    "decision_support": Specialist(
        id="decision_support", name="Decision Support Agent",
        purpose="Patterns, drivers (evidenced / correlated / hypothesis), options and trade-offs, "
                "implementation plan, KPIs and risks.",
        model_slot="", deterministic=True, kinds=[], tools=[], validator_kind="data"),
    "artifact": Specialist(
        id="artifact", name="Artifact Agent",
        purpose="One verified analysis to Excel, Word, PowerPoint, PDF, brief or action plan - each file "
                "validated and checked against the analysis.",
        model_slot="", deterministic=True, kinds=[],
        tools=["create_analysis_artifact"], validator_kind="file"),
    "validation": Specialist(
        id="validation", name="Validation Agent",
        purpose="Conflicts between sources, coverage gaps, answer validation; the RAG evaluation.",
        model_slot="", deterministic=True, kinds=[],
        tools=["evaluate_company_rag"], validator_kind="data"),
    "citation": Specialist(
        id="citation", name="Citation Agent",
        purpose="Every statement carries its evidence, fact, relation or calculation id; a citation "
                "that does not exist fails the answer.",
        model_slot="", deterministic=True, kinds=[], tools=[], validator_kind="data"),

    # ---- ZEUS: the planning-to-execution bridge (jarvis/). Added alongside
    # everything above; nothing was replaced.
    "field_capture": Specialist(
        id="field_capture", name="Field Capture Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Turns site photos, DPRs, letters, voice memos and vernacular notes into evidence, extracted facts "
                "and activity-linked events awaiting approval - the original always kept.",
        model_slot="", deterministic=True, kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["record_field_update", "ingest_field_evidence", "resolve_activity_link", "jarvis_pending",
               "jarvis_evidence", "jarvis_evidence_trail"],
        validator_kind="data"),
    "project_controls": Specialist(
        id="project_controls", name="Project Controls Agent",
        primary_kind=TaskKind.INDUSTRIAL_DATA,
        purpose="Schedules, CPM forecast, approvals, why the finish moved, delay chronology, notice deadlines, "
                "temporal questions and learning cards - every date computed, every claim evidence-linked.",
        model_slot="", deterministic=True, kinds=[TaskKind.INDUSTRIAL_DATA],
        tools=["jarvis_projects", "jarvis_overview", "jarvis_home", "jarvis_schedule", "jarvis_versions",
               "jarvis_compare_versions", "jarvis_activity", "jarvis_why_moved", "jarvis_forecast_changes",
               "jarvis_chronology", "jarvis_notice_deadlines", "jarvis_delay_windows", "jarvis_learning_cards",
               "jarvis_search", "jarvis_ask", "jarvis_state_as_of", "jarvis_first_slip", "jarvis_model_routing",
               "jarvis_verify_events", "jarvis_contract_refs", "create_jarvis_project", "set_current_project",
               "ingest_schedule_file", "approve_schedule_version", "approve_field_update", "register_contract_notice",
               "record_notice_issued", "ingest_historical_activities", "seed_jarvis_demo", "index_jarvis_projects"],
        validator_kind="data"),
    "project_reports": Specialist(
        id="project_reports", name="Project Reports Agent",
        purpose="Progress reports, delay chronologies, evidence packs, schedule analysis and learning reports "
                "built from the project record and verified on disk.",
        model_slot="", deterministic=True, kinds=[],
        tools=["export_progress_report", "export_delay_chronology", "export_evidence_pack",
               "export_schedule_analysis", "export_learning_report"],
        validator_kind="file"),
    "offline_sync": Specialist(
        id="offline_sync", name="Offline Sync Agent",
        purpose="Keeps field devices useful without a network: queued events, bundles, conflict detection that "
                "never overwrites, and authorised resolution.",
        model_slot="", deterministic=True, kinds=[],
        tools=["jarvis_sync_status", "jarvis_sync_conflicts", "resolve_sync_conflict", "set_sync_hub",
               "export_sync_bundle", "ingest_sync_bundle", "request_sync"],
        validator_kind="data"),

    "conversation": Specialist(
        id="conversation", name="Conversation Agent",
        purpose="General questions and open discussion that is not a tool action.",
        model_slot="default",
        kinds=[TaskKind.CHAT, TaskKind.REASONING],
        tools=[],
        validator_kind="text"),
}

# Which specialist takes a task kind when no specific tool matched.
_KIND_DEFAULT: Dict[TaskKind, str] = {
    TaskKind.CHAT: "conversation",
    TaskKind.REASONING: "conversation",
    TaskKind.CODE: "coding",
    TaskKind.RAG: "knowledge",
    TaskKind.DOCUMENT_READ: "data_analysis",
    TaskKind.OCR: "vision",
    TaskKind.VISION: "vision",
    TaskKind.WORD: "document",
    TaskKind.EXCEL: "document",
    TaskKind.POWERPOINT: "document",
    TaskKind.PDF: "file",
    TaskKind.CONVERSION: "file",
    TaskKind.FILE_OPS: "file",
    TaskKind.INDUSTRIAL_DATA: "operations",
    TaskKind.AUTOMATION: "automation",
    TaskKind.MEMORY: "memory",
    TaskKind.MULTI_STEP: "report",
    TaskKind.UNKNOWN: "conversation",
}

# Names that genuinely denote writing a file. Everything else is validated
# from what the step actually returned, or by its agent's declared kind.
_PRODUCES_FILE = re.compile(
    r"^(executive_|create_|write_|build_|generate_|export_|make_)"
    r"|_(report|presentation|workbook|deck|note|pack|docx|xlsx|pptx|pdf)$")

_TOOL_INDEX: Dict[str, str] = {}
for _specialist in SPECIALISTS.values():
    for _tool in _specialist.tools:
        _TOOL_INDEX.setdefault(_tool, _specialist.id)


def agent_for_tool(tool_name: str) -> Optional[str]:
    """Which specialist owns a tool. Falls back to the original agent registry
    so nothing that was already mapped becomes unmapped."""
    if tool_name in _TOOL_INDEX:
        return _TOOL_INDEX[tool_name]
    from core.agents import get_agent_for_tool
    return get_agent_for_tool(tool_name)


def agent_for_kind(kind: TaskKind) -> str:
    return _KIND_DEFAULT.get(kind, "conversation")


def get(agent_id: str) -> Optional[Specialist]:
    return SPECIALISTS.get(agent_id)


def model_slot_for(agent_id: str) -> str:
    specialist = SPECIALISTS.get(agent_id)
    return specialist.model_slot if specialist else "default"


def validator_for(agent_id: str, tool_name: str = "") -> str:
    """The validator kind for what this agent produces. A document tool is
    checked against its own file extension, which the tool name implies."""
    # Only tools that actually PRODUCE a file are judged by name, and only
    # when the name says so plainly. `analyze_spreadsheet` contains the word
    # "spreadsheet" but writes nothing - guessing from a substring made the
    # pipeline try to open an analysis dictionary as a workbook.
    if tool_name and _PRODUCES_FILE.match(tool_name.lower()):
        lowered = tool_name.lower()
        if "pack" in lowered or lowered.endswith("_bundle"):
            return "bundle"          # several files - every one of them, or it is not done
        if "presentation" in lowered or "deck" in lowered or lowered.endswith("_pptx"):
            return "pptx"
        if "workbook" in lowered or lowered.endswith("_xlsx"):
            return "xlsx"
        if ("report" in lowered or "document" in lowered or "note" in lowered
                or lowered.endswith("_docx")):
            return "docx"
    specialist = SPECIALISTS.get(agent_id)
    return specialist.validator_kind if specialist else ""


def describe_agents() -> List[Dict[str, object]]:
    return [s.as_dict() for s in SPECIALISTS.values()]


def _publish_to_legacy_registry():
    """Make every specialist visible to the pre-existing agent registry, so
    'list agents' in the UI shows one roster rather than two. Existing entries
    are left exactly as they were."""
    for specialist in SPECIALISTS.values():
        if specialist.id in AGENT_REGISTRY:
            continue
        AGENT_REGISTRY[specialist.id] = AgentSpec(
            id=specialist.id, name=specialist.name, purpose=specialist.purpose,
            backend=(f"deterministic (Python tools)" if specialist.deterministic
                     else f"{specialist.model_slot} model slot"),
            owned_tools=list(specialist.tools), offline=True,
            status="active", implemented=True)


_publish_to_legacy_registry()
