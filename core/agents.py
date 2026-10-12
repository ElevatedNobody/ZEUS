"""
ZEUS Agent Registry
Organizes the existing, already-working tool set into agents, each with a
single clear responsibility - matching the "Agent != Model" principle:
most agents share a small offline model set (mistral/qwen2.5 reasoning,
qwen2.5vl vision, bge-m3 embeddings) or use no model at all (deterministic
Python), rather than requiring a separate large model per agent.

This is deliberately an ORGANIZATIONAL layer on top of the existing,
tested tool_registry - it does not change what executes, only how it's
labeled and made observable.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AgentSpec:
    id: str
    name: str
    purpose: str
    backend: str
    owned_tools: List[str] = field(default_factory=list)
    offline: bool = True
    # Added for the industrial workbench: agents whose environment and
    # interfaces are prepared but whose logic is not implemented yet are
    # listed honestly as 'prepared' rather than pretending to be active.
    # Defaults keep every existing agent exactly as it was.
    status: str = "active"
    implemented: bool = True


AGENT_REGISTRY: Dict[str, AgentSpec] = {
    "orchestrator": AgentSpec(
        id="orchestrator", name="Orchestrator Agent",
        purpose="Classify each request and route it to the right specialist agent; "
                "answers 'which agent handles X' and lists the full agent roster.",
        backend="deterministic (regex/keyword routing) + reasoning model for ambiguous free text",
        owned_tools=[],
    ),
    "conversation": AgentSpec(
        id="conversation", name="Conversation Agent",
        purpose="General Q&A and open-ended reasoning that isn't a specific tool action.",
        backend="mistral:7b-instruct-q4_K_M", owned_tools=[],
    ),
    "automation": AgentSpec(
        id="automation", name="Automation Agent",
        purpose="Control the computer: launch/close apps, manage windows, keyboard/mouse, "
                "clipboard, media keys, run shell commands.",
        backend="deterministic (pyautogui/pygetwindow/pyperclip/subprocess)",
        owned_tools=[
            "open_application", "close_application", "list_windows", "focus_window",
            "list_running_apps", "minimize_window", "maximize_window", "close_window",
            "type_text", "press_key", "hotkey", "mouse_move", "mouse_click",
            "get_clipboard", "set_clipboard", "media_control", "open_browser",
            "open_file_explorer", "open_settings", "open_and_type",
            "lock_screen", "shutdown_pc", "restart_pc", "cancel_shutdown", "run_command",
        ],
    ),
    "file": AgentSpec(
        id="file", name="File Agent",
        purpose="Search, read, write, copy, move, delete files, and convert between formats.",
        backend="deterministic (filesystem + Pillow/python-docx/pandas)",
        owned_tools=["search_files", "read_file", "write_file", "copy_file", "move_file",
                     "create_folder", "delete_file", "convert_file"],
    ),
    "document_generation": AgentSpec(
        id="document_generation", name="Document Generation Agent",
        purpose="Write assignments/essays (Word), presentations (PowerPoint), and "
                "spreadsheets (Excel) using the local reasoning model.",
        backend="qwen2.5:3b-instruct-q4_K_M (structured) + mistral:7b-instruct (prose)",
        owned_tools=["write_document", "create_presentation", "create_spreadsheet"],
    ),
    "vision": AgentSpec(
        id="vision", name="Vision Agent",
        purpose="Understand images and screen content - OCR text extraction and genuine "
                "visual reasoning about layout, diagrams, and objects.",
        backend="EasyOCR (deterministic) + qwen2.5vl:3b-q4_K_M (visual reasoning)",
        owned_tools=["take_screenshot", "read_screen", "analyze_image"],
    ),
    "knowledge": AgentSpec(
        id="knowledge", name="Knowledge Agent (RAG)",
        purpose="Index local documents and answer questions grounded in them with source citations.",
        backend="bge-m3 (embeddings) + mistral:7b-instruct (grounded answering)",
        owned_tools=["index_folder", "ask_knowledge_base", "list_knowledge_sources",
                     "forget_document", "clear_knowledge_base"],
    ),
    "productivity": AgentSpec(
        id="productivity", name="Productivity Agent",
        purpose="Manage to-dos, calendar events, notes, and weekly schedules.",
        backend="deterministic (SQLite) + qwen2.5:3b-instruct for schedule generation",
        owned_tools=["add_todo", "list_todos", "complete_todo", "uncomplete_todo", "delete_todo",
                     "add_event", "list_events", "delete_event", "add_note", "list_notes",
                     "update_note", "delete_note", "create_schedule", "update_schedule", "list_memories"],
    ),
    "background": AgentSpec(
        id="background", name="Background Agent",
        purpose="Run tasks that persist without blocking the conversation: folder watching and reminders.",
        backend="deterministic (watchdog + threading.Timer)",
        owned_tools=["watch_folder", "stop_watching_folder", "remind_me", "cancel_reminder", "cancel_task"],
    ),
    "math": AgentSpec(
        id="math", name="Math Agent",
        purpose="Evaluate arithmetic and expressions deterministically.",
        backend="deterministic (AST-based safe evaluator)", owned_tools=["calculate"],
    ),
    "system": AgentSpec(
        id="system", name="System Agent",
        purpose="Report system health, resource usage, security audit history, self-diagnostics.",
        backend="deterministic (psutil + dependency/connection checks)",
        owned_tools=["get_system_info", "get_cpu_usage", "get_memory_usage", "get_disk_usage",
                     "get_battery", "get_time", "show_audit_log", "run_diagnostics"],
    ),
}


def get_agent_for_tool(tool_name: str) -> Optional[str]:
    for agent_id, spec in AGENT_REGISTRY.items():
        if tool_name in spec.owned_tools:
            return agent_id
    return None


def list_agents() -> List[dict]:
    return [
        {"id": s.id, "name": s.name, "purpose": s.purpose, "backend": s.backend,
         "tool_count": len(s.owned_tools), "offline": s.offline, "status": s.status}
        for s in AGENT_REGISTRY.values()
    ]


def describe_agent(agent_id: str) -> Optional[dict]:
    spec = AGENT_REGISTRY.get(agent_id)
    if not spec:
        return None
    return {"id": spec.id, "name": spec.name, "purpose": spec.purpose,
            "backend": spec.backend, "owned_tools": spec.owned_tools, "offline": spec.offline,
            "status": spec.status, "implemented": spec.implemented}
