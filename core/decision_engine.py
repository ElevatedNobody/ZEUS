"""
ZEUS Decision Engine
The central orchestrator: USER REQUEST -> INTENT -> PLAN -> TOOLS -> RESPONSE.
This is what main.py calls for every piece of user input.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.model_router import model_router
from core.ollama_client import ollama_client
from core.planner import IntentType, classify_intent, decompose
from core.task_manager import TaskStatus, task_manager
from core.tool_registry import registry

logger = get_logger("engine")

# ---------------------------------------------------------------------------
# Deterministic command routing. Every pattern here resolves DIRECTLY to a
# registered tool call - no LLM round trip, no Ollama dependency. This is
# what makes "open chrome", "lock the screen", "delete file X" etc. actually
# execute instead of silently falling through to a chat response.
# ---------------------------------------------------------------------------

_ROUTES: List[Tuple[re.Pattern, str, Optional[Callable[[re.Match], dict]]]] = [
    (re.compile(r"^(?:take a )?screenshot$"), "take_screenshot", None),
    (re.compile(r"^cpu(?: usage)?$"), "get_cpu_usage", None),
    (re.compile(r"^(?:memory|ram)(?: usage)?$"), "get_memory_usage", None),
    (re.compile(r"^disk(?: usage)?$"), "get_disk_usage", None),
    (re.compile(r"^battery(?: status| level)?$"), "get_battery", None),
    (re.compile(r"^system(?: info| information| status)?$"), "get_system_info", None),
    (re.compile(r"^what(?:'s| is) the time\??$"), "get_time", None),
    (re.compile(r"^what(?:'s| is) the date\??$"), "get_time", None),
    (re.compile(r"^what(?:'s| is) today'?s? date\??$"), "get_time", None),

    (re.compile(r"^open (?:the )?(?:web )?browser$"), "open_browser", None),
    (re.compile(r"^open (?:file explorer|files|explorer)$"), "open_file_explorer", None),
    (re.compile(r"^open settings$"), "open_settings", None),
    (re.compile(r"^open (.+?) and type (.+)$"),
     "open_and_type", lambda m: {"app": m.group(1).strip(), "text": m.group(2).strip()}),
    # Ahead of the generic "open <anything>": otherwise a document name is
    # handed to the application launcher, which cannot open it.
    (re.compile(r"^open (?:the )?(?:generated )?document (.+)$"),
     "open_document", lambda m: {"name_or_path": m.group(1).strip()}),
    # --- Vault and employees ------------------------------------------------
    # These sit ABOVE `open (.+)` deliberately. That route is a catch-all that
    # sends anything else to open_application, so "open the vault" placed after
    # it would try to launch a Windows program called "the vault" and report
    # that it could not find one. Anything more specific than a catch-all has
    # to precede it - the same reason `open_document` is already up here.
    (re.compile(r"^(?:open|unlock) (?:the |my )?(?:secure )?(?:vault|locker|file locker)$"),
     "vault_status", None),
    (re.compile(r"^lock (?:the |my )?(?:secure )?(?:vault|locker|file locker)$"),
     "lock_vault", None),
    (re.compile(r"^(?:show|list|what(?:'s| is) in) (?:the |my )?"
                r"(?:secure )?(?:vault|locker|file locker)(?: files| contents)?$"),
     "list_vault_files", None),
    (re.compile(r"^(?:show|list) (?:the |my )?(?:secure )?vault (.+?) files?$"),
     "list_vault_files", lambda m: {"category": m.group(1).strip()}),
    (re.compile(r"^(?:vault|locker) status$"), "vault_status", None),
    (re.compile(r"^(?:show|check) (?:the )?vault (?:audit|log|audit log|history)$"),
     "vault_audit_log", None),
    (re.compile(r"^(?:add|put|store|lock) (.+?) (?:in|into|to) (?:the |my )?"
                r"(?:secure )?(?:vault|locker|file locker)$"),
     "add_file_to_vault", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^open (?:the )?vault file (.+)$"),
     "open_vault_file", lambda m: {"name": m.group(1).strip()}),
    (re.compile(r"^(?:export|get|take) (.+?) (?:out ?of|from) (?:the |my )?"
                r"(?:secure )?(?:vault|locker) (?:to|into) (.+)$"),
     "export_vault_file",
     lambda m: {"name": m.group(1).strip(), "destination": m.group(2).strip()}),
    (re.compile(r"^(?:delete|remove) (.+?) from (?:the |my )?"
                r"(?:secure )?(?:vault|locker|file locker)$"),
     "remove_vault_file", lambda m: {"name": m.group(1).strip()}),

    (re.compile(r"^(?:show|list) (?:all )?(?:the )?(?:employees|staff|personnel|people)$"),
     "list_employees", None),
    (re.compile(r"^(?:show|list) (?:the )?(?:employees|staff|people) in (.+)$"),
     "list_employees", lambda m: {"department": m.group(1).strip()}),
    # Anchored on an explicit employee word. An earlier draft accepted
    # "who is (.+)" and "find (.+)", which swallowed "who is the plant
    # manager" and every ordinary question starting with "find" - a route
    # broad enough to catch general chat is worse than no route at all.
    (re.compile(r"^(?:who is|find|look up|lookup|show me) "
                r"(?:the )?(?:employee|staff member|colleague) (.+)$"),
     "find_employee", lambda m: {"query": m.group(1).strip()}),
    (re.compile(r"^(?:who is|find|look up|lookup) (.+?) "
                r"in the (?:employee )?(?:register|directory|roster)$"),
     "find_employee", lambda m: {"query": m.group(1).strip()}),
    (re.compile(r"^(?:show|list) (?:the )?department (?:roster|headcount|breakdown)$"),
     "department_roster", None),
    (re.compile(r"^(?:remove|delete) employee (.+)$"),
     "remove_employee", lambda m: {"employee_id": m.group(1).strip()}),

    (re.compile(r"^open (.+)$"), "open_application", lambda m: {"name": m.group(1).strip()}),
    (re.compile(r"^(?:close|quit|exit) (.+)$"), "close_application", lambda m: {"name": m.group(1).strip()}),
    (re.compile(r"^(?:focus|switch to) (.+)$"), "focus_window", lambda m: {"title": m.group(1).strip()}),
    (re.compile(r"^list windows$"), "list_windows", None),
    (re.compile(r"^(?:list |show )?(?:running )?apps$"), "list_running_apps", None),
    (re.compile(r"^what(?:'s| is) running\??$"), "list_running_apps", None),

    (re.compile(r"^lock(?: the)? (?:screen|pc|computer)$"), "lock_screen", None),
    (re.compile(r"^shut ?down(?: the)? ?(?:pc|computer)?$"), "shutdown_pc", None),
    (re.compile(r"^restart(?: the)? ?(?:pc|computer)?$"), "restart_pc", None),
    (re.compile(r"^cancel shutdown$"), "cancel_shutdown", None),

    (re.compile(r"^(?:play|resume)(?: music| the music)?$"), "media_control", lambda m: {"action": "play"}),
    (re.compile(r"^pause(?: music| the music)?$"), "media_control", lambda m: {"action": "pause"}),
    (re.compile(r"^next(?: track| song)?$"), "media_control", lambda m: {"action": "next"}),
    (re.compile(r"^(?:previous|last)(?: track| song)?$"), "media_control", lambda m: {"action": "previous"}),
    (re.compile(r"^(?:increase|raise|turn up)(?: the)? volume$"), "media_control", lambda m: {"action": "volume_up"}),
    (re.compile(r"^(?:decrease|lower|turn down)(?: the)? volume$"), "media_control", lambda m: {"action": "volume_down"}),
    (re.compile(r"^mute$"), "media_control", lambda m: {"action": "mute"}),

    (re.compile(r"^(?:read|scan) (?:my |the )?screen$"), "read_screen", None),
    (re.compile(r"^what(?:'s| is| does it say) on (?:my |the )?screen\??$"), "read_screen", None),
    # "read this excel and make a deck" is an ANALYSIS request, not a file read -
    # so it has to be matched before the generic "read <anything>" route.
    (re.compile(r"^(?:read|take|use) (?:the |this |that )?(?:spreadsheet|excel|workbook|csv)"
                r"(?: (.+?))? and (?:create|make|build) (?:a |an )?"
                r"(?:presentation|deck|powerpoint|ppt)\.?$"),
     "spreadsheet_to_presentation", lambda m: {"path": (m.group(1) or "").strip()}),
    (re.compile(r"^(?:read|take|use) (?:the |this |that )?(?:spreadsheet|excel|workbook|csv)"
                r"(?: (.+?))? and (?:create|make|write) (?:a |an )?(?:word )?report\.?$"),
     "spreadsheet_to_report", lambda m: {"path": (m.group(1) or "").strip()}),
    (re.compile(r"^read(?: file)? (.+)$"), "read_file", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^create(?: a)?(?: new)? folder(?: called| named)? (.+)$"),
     "create_folder", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^delete to-?do (\d+)$"), "delete_todo", lambda m: {"todo_id": int(m.group(1))}),
    (re.compile(r"^delete event (\d+)$"), "delete_event", lambda m: {"event_id": int(m.group(1))}),
    (re.compile(r"^delete note (\d+)$"), "delete_note", lambda m: {"note_id": int(m.group(1))}),
    (re.compile(r"^delete(?: file)? (.+)$"), "delete_file", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^copy (.+) to clipboard$"), "set_clipboard", lambda m: {"text": m.group(1).strip()}),
    (re.compile(r"^copy (.+) to (.+)$"), "copy_file", lambda m: {"src": m.group(1).strip(), "dst": m.group(2).strip()}),
    (re.compile(r"^move (.+) to (.+)$"), "move_file", lambda m: {"src": m.group(1).strip(), "dst": m.group(2).strip()}),
    (re.compile(r"^(?:find|search for) (\*?\.?\w+) files?(?: in (.+))?$"),
     "search_files", lambda m: {
         "pattern": m.group(1) if m.group(1).startswith("*") else f"*.{m.group(1).lstrip('.')}",
         "root": (m.group(2).strip() if m.group(2) else str(__import__('pathlib').Path.home())),
     }),

    # "remember that ..." is handled deterministically so a spoken preference
    # is stored and confirmed without needing a model round trip. The same
    # fact is also captured by MemoryManager when the message is recorded -
    # both paths use the same key, so this is idempotent, not a second store.
    (re.compile(r"^remember (?:that |this[:,]? )?(.+)$"),
     "remember_fact", lambda m: {"fact": m.group(1).strip()}),
    (re.compile(r"^forget (?:that |about )?(.+)$"),
     "forget_fact", lambda m: {"fact": m.group(1).strip()}),
    (re.compile(r"^what(?:'s| is| do you) (?:you )?remember(?:ed)?\??$"), "list_memories", None),
    (re.compile(r"^(?:show|list) (?:my )?(?:memories|memory|facts)$"), "list_memories", None),
    (re.compile(r"^(?:show|open) (?:the )?(?:security|audit) log$"), "show_audit_log", None),

    (re.compile(r"^type (.+)$"), "type_text", lambda m: {"text": m.group(1).strip()}),
    (re.compile(r"^press (?:the )?(\w+)(?: key)?$"), "press_key", lambda m: {"key": m.group(1).strip()}),
    (re.compile(r"^(?:hotkey|shortcut) (.+)$"), "hotkey", lambda m: {"combo": m.group(1).strip()}),
    (re.compile(r"^what(?:'s| is) (?:on|in) (?:my |the )?clipboard\??$"), "get_clipboard", None),

    (re.compile(r"^minimize (.+)$"), "minimize_window", lambda m: {"title": m.group(1).strip()}),
    (re.compile(r"^maximize (.+)$"), "maximize_window", lambda m: {"title": m.group(1).strip()}),

    (re.compile(r"^(?:watch|monitor) (?:the )?folder (.+)$"), "watch_folder", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^stop watching (.+)$"), "stop_watching_folder", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^remind me (?:to |in )?(.+?) in (\d+(?:\.\d+)?) minutes?$"),
     "remind_me", lambda m: {"message": m.group(1).strip(), "minutes": float(m.group(2))}),
    (re.compile(r"^remind me in (\d+(?:\.\d+)?) minutes? to (.+)$"),
     "remind_me", lambda m: {"message": m.group(2).strip(), "minutes": float(m.group(1))}),
    # Executive deliverables come FIRST, ahead of the generic "presentation
    # about anything" route: these are rendered from the shared analysis layer
    # (real records, consistent numbers), not written by the model.
    (re.compile(r"^(?:create|make|build|generate) (?:me )?(?:an?|the) "
                r"(?:executive|management|board) (?:presentation|deck|powerpoint|ppt)"
                r"(?: (?:for|on|about) (.+?))?\.?$"),
     "executive_presentation", lambda m: {"subject": (m.group(1) or "").strip()}),
    (re.compile(r"^(?:create|make|build|generate) (?:me )?(?:an?|the) "
                r"(?:analysis|executive|management) workbook"
                r"(?: (?:for|on|about) (.+?))?\.?$"),
     "executive_workbook", lambda m: {"subject": (m.group(1) or "").strip()}),
    (re.compile(r"^(?:create|make|build|generate) (?:me )?(?:an?|the) "
                r"(?:executive|analysis|management) report"
                r"(?: (?:for|on|about) (.+?))?\.?$"),
     "executive_report", lambda m: {"subject": (m.group(1) or "").strip()}),
    (re.compile(r"^(?:create|make|build|generate) (?:me )?(?:an?|the) "
                r"(?:briefing|board|management) pack"
                r"(?: (?:for|on|about) (.+?))?\.?$"),
     "executive_briefing_pack", lambda m: {"subject": (m.group(1) or "").strip()}),

    # Spreadsheet -> analysis -> deliverable. "read this excel and make a ppt"
    # must ANALYSE the file, not paste its rows onto slides.
    (re.compile(r"^(?:analy[sz]e|examine|profile|read) (?:the |this |my )?"
                r"(?:spreadsheet|excel|workbook|csv|file) (.+?)\.?$"),
     "analyze_spreadsheet", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^(?:create|make|build) (?:a |an )?(?:presentation|deck|powerpoint|ppt) "
                r"from (?:the |this |that )?(?:spreadsheet|excel|workbook|csv|file)"
                r"(?: (.+?))?\.?$"),
     "spreadsheet_to_presentation", lambda m: {"path": (m.group(1) or "").strip()}),

    # Industrial decks come FIRST: the generic presentation route below matches
    # any topic, so a specific "presentation for equipment P-204" would
    # otherwise be answered by an invented deck instead of that pump's records.
    (re.compile(r"^(?:make|create|build|generate) (?:me )?(?:a |an |the )?"
                r"(?:presentation|deck|powerpoint|ppt) (?:for|about|reviewing|on) "
                r"(?:equipment |unit |item )([A-Za-z]{1,4}[- ]?\d{2,5}[A-Za-z]?)\.?$"),
     "equipment_presentation", lambda m: {"equipment": m.group(1).strip()}),
    (re.compile(r"^(?:make|create|build|generate) (?:me )?(?:a |an |the )?"
                r"(?:management |operations? |plant )?"
                r"(?:presentation|deck|powerpoint|ppt) (?:of|on|about|summarising|"
                r"summarizing) (?:the )?(?:current |today'?s? )?"
                r"(?:operations?|plant|maintenance)(?: situation| status| position)?\.?$"),
     "operations_presentation", None),

    # "make a 10-slide presentation on X" - the slide count was previously
    # swallowed into the topic, so every deck came out at the default length.
    (re.compile(r"^(?:make|create|build) (?:me )?(?:a |an )?(?:(\d{1,2})[- ]slide )?"
                r"(?:powerpoint|ppt|presentation|slides?|deck)(?: on| about| for)? (.+)$"),
     "create_presentation",
     lambda m: ({"topic": m.group(2).strip(), "slide_count": int(m.group(1))}
                if m.group(1) else {"topic": m.group(2).strip()})),
    # "create an excel SHEET for X" used to capture "sheet for X" as the topic.
    (re.compile(r"^(?:make|create|build) (?:me )?(?:a |an )?(?:excel|spreadsheet|xlsx|workbook)"
                r"(?: (?:sheet|file|workbook|spreadsheet|table))?"
                r"(?: (?:of|on|about|for|with))? (.+)$"),
     "create_spreadsheet", lambda m: {"topic": m.group(1).strip()}),

    # Casual / short forms that users actually type
    (re.compile(r"^(?:make|create|build|generate|write|prepare) (?:me )?(?:a |an |the )?(?:excel|spreadsheet|xlsx|workbook|sheet)"
                r"(?: (?:file|sheet|table|of|for|on|about))?\s*(.+?)\.?$", re.IGNORECASE),
     "create_spreadsheet", lambda m: {"topic": m.group(1).strip()}),
    (re.compile(r"^(?:excel|spreadsheet|xlsx)\s+(?:of|for|on|about)?\s*(.+?)\.?$", re.IGNORECASE),
     "create_spreadsheet", lambda m: {"topic": m.group(1).strip()}),
    (re.compile(r"^cancel task (\d+)$"), "cancel_task", lambda m: {"task_id": int(m.group(1))}),

    (re.compile(r"^(?:run|start) diagnostics$"), "run_diagnostics", None),
    (re.compile(r"^(?:medha,? )?(?:run a )?(?:system )?health check$"), "run_diagnostics", None),
    (re.compile(r"^(?:list|show) (?:your |the |all )?agents$"), "list_agents", None),
    (re.compile(r"^what agents do you have\??$"), "list_agents", None),
    (re.compile(r"^(?:which agent handles|what agent owns|describe agent) (.+)$"),
     "describe_agent", lambda m: {"agent_or_tool": m.group(1).strip()}),
    (re.compile(r"^(?:run|execute)(?: this)? command:? (.+)$"), "run_command", lambda m: {"command": m.group(1).strip()}),

    (re.compile(r"^index (?:folder|documents in|my documents in) (.+)$"),
     "index_folder", lambda m: {"path": m.group(1).strip()}),

    (re.compile(r"^(?:index|add) (?:this |the )?(?:file|document|pdf|doc) (.+)$", re.IGNORECASE),
     "index_file", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^(?:index) (?:my )?(?:documents|docs|medha documents?|knowledge base)\.?$", re.IGNORECASE),
     "index_my_documents", lambda m: {}),
    # "search my documents for X" is deliberately NOT matched here: it belongs
    # to the grounded, cited ask_knowledge_base route further down. This
    # keyword search route (added with the knowledge pack) used to shadow it.
    (re.compile(r"^(?:search|find|query) (?:knowledge|knowledge base|index)(?: for)? (.+)$", re.IGNORECASE),
     "search_knowledge", lambda m: {"query": m.group(1).strip()}),
    (re.compile(r"^(?:rag|grounded) (?:write|document|report)(?: on| about| for)? (.+)$", re.IGNORECASE),
     "rag_write_document", lambda m: {"topic": m.group(1).strip()}),
    (re.compile(r"^(?:knowledge|rag) (?:stats|status|info)\.?$", re.IGNORECASE),
     "knowledge_stats", lambda m: {}),

    (re.compile(r"^(?:install|load|index) (?:knowledge|company|jarvis) pack\.?$", re.IGNORECASE),
     "index_my_documents", lambda m: {}),
    (re.compile(r"^(?:tell me about|what is|who is|describe) (nexus(?: precision)?|the company)\??$", re.IGNORECASE),
     "ask_knowledge_base", lambda m: {"query": "Nexus Precision company profile history overview"}),
    (re.compile(r"^(?:company history|nexus history)\.?$", re.IGNORECASE),
     "ask_knowledge_base", lambda m: {"query": "Nexus Precision full company history timeline"}),


    (re.compile(r"^(?:forget document|remove from knowledge base) (.+)$"),
     "forget_document", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^clear (?:my )?knowledge base$"), "clear_knowledge_base", None),
    (re.compile(r"^(?:list|show) (?:my )?(?:knowledge sources|indexed documents|what have i indexed)$"),
     "list_knowledge_sources", None),
    (re.compile(r"^what have i indexed\??$"), "list_knowledge_sources", None),
    (re.compile(r"^(?:search my documents for|ask my documents|search my notes for) (.+)$"),
     "ask_knowledge_base", lambda m: {"query": m.group(1).strip()}),

    (re.compile(r"^(?:analyze|describe) (?:this |the )?screen(?:shot)?$"),
     "analyze_image", lambda m: {"prompt": ""}),
    (re.compile(r"^what(?:'s| is) on (?:my |the )?screen visually\??$"),
     "analyze_image", lambda m: {"prompt": ""}),
    (re.compile(r"^(?:analyze|describe) (?:the )?image (.+)$"),
     "analyze_image", lambda m: {"image_path": m.group(1).strip()}),

    (re.compile(r"^(?:add|create) (?:a )?to-?do:? (.+)$"), "add_todo", lambda m: {"text": m.group(1).strip()}),
    (re.compile(r"^(?:show|list) (?:my )?to-?dos?$"), "list_todos", None),
    (re.compile(r"^(?:complete|finish|check off|mark done) to-?do (\d+)$"),
     "complete_todo", lambda m: {"todo_id": int(m.group(1))}),

    (re.compile(r"^add event (.+?) (?:on|for) (today|tomorrow|\d{4}-\d{2}-\d{2})"
                r"(?: at (\d{1,2}:\d{2}))?$"),
     "add_event", lambda m: {"title": m.group(1).strip(), "date": m.group(2),
                              "event_time": m.group(3) or ""}),
    (re.compile(r"^(?:show|list) (?:my )?(?:calendar|events)(?: for (today|tomorrow|\d{4}-\d{2}-\d{2}))?$"),
     "list_events", lambda m: {"date": m.group(1) or ""}),

    (re.compile(r"^(?:add|save|take|create) (?:a )?notes?:? (.+)$"), "add_note", lambda m: {"content": m.group(1).strip()}),
    (re.compile(r"^(?:show|list) (?:my )?notes$"), "list_notes", None),

    (re.compile(r"^convert (.+?) to (.+)$"), "convert_file", lambda m: {"src": m.group(1).strip(), "dst": m.group(2).strip()}),

    (re.compile(r"^calculate (.+)$"), "calculate", lambda m: {"expression": m.group(1).strip()}),
    # Industrial workbench commands. Appended LAST so every pre-existing route
    # keeps priority. These exist so a SPOKEN industrial command reaches the
    # industrial tools deterministically - the same path a typed one takes -
    # instead of depending on the model to guess a tool call.
    (re.compile(r"^(?:industrial|workbench) status$"), "industrial_status", None),
    (re.compile(r"^(?:industrial|engineering) (?:term|glossary|meaning of) (.+)$"),
     "industrial_term", lambda m: {"term": m.group(1).strip()}),
    (re.compile(r"^what does ([A-Za-z&/ .\-]{2,40}) mean in industry\??$"),
     "industrial_term", lambda m: {"term": m.group(1).strip()}),
    (re.compile(r"^(?:ingest|analyz[es]e?) (?:the )?(?:industrial )?(?:document|report|file) (.+)$"),
     "ingest_industrial_document", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^(?:ingest|index) (?:the )?industrial folder (.+)$"),
     "ingest_industrial_folder", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^(?:classify|identify) (?:the )?(?:industrial )?document (.+)$"),
     "classify_industrial_document", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^extract (?:the )?(?:fields? from )?(?:industrial )?document (.+)$"),
     "extract_industrial_document", lambda m: {"path": m.group(1).strip()}),
    (re.compile(r"^ask (?:my |the )?industrial (?:knowledge base|documents?|kb)"
                r"(?: about| for)? (.+)$"),
     "ask_industrial_kb", lambda m: {"query": m.group(1).strip()}),
    (re.compile(r"^(?:find|search for) (?:industrial )?evidence (?:about |for |on )?(.+)$"),
     "search_industrial_evidence", lambda m: {"query": m.group(1).strip()}),
    (re.compile(r"^(?:list|show) (?:my )?industrial documents$"),
     "list_industrial_documents", None),
    (re.compile(r"^(?:list|show) (?:my |the )?industrial agents$"), "list_industrial_agents", None),
    (re.compile(r"^(?:industrial )?model health$"), "industrial_model_health", None),
    (re.compile(r"^ingest (?:the )?industrial (?:knowledge )?corpus$"),
     "ingest_industrial_knowledge_corpus", None),

    # ---------------------------------------------------------------- plant --
    # Industrial plant commands. Deterministic, so they work with Ollama down
    # and so a SPOKEN command reaches the same tool a typed one does.
    (re.compile(r"^(?:show |what(?:'s| is) )?(?:the )?(?:plant|operations?|operational) "
                r"(?:status|summary|overview)\??$"), "plant_status", None),
    (re.compile(r"^(?:show|list) (?:the |all )?(?:current )?equipment"
                r"(?: list| register| status)?$"),
     "list_equipment", None),
    (re.compile(r"^(?:show|list) (?:the )?equipment in (.+)$"),
     "list_equipment", lambda m: {"location": m.group(1).strip()}),
    (re.compile(r"^(?:tell me )?(?:show me )?everything (?:stored )?(?:about|on) (.+?)\.?$"),
     "equipment_details", lambda m: {"equipment": m.group(1).strip()}),
    (re.compile(r"^(?:show|get|what(?:'s| is) the) (?:details|status|information|info) "
                r"(?:for|about|on|of) (.+?)\??$"),
     "equipment_details", lambda m: {"equipment": m.group(1).strip()}),

    (re.compile(r"^(?:show|list|get) (?:the |me the )?maintenance history "
                r"(?:for|of|on) (.+?)\.?$"),
     "maintenance_history", lambda m: {"equipment": m.group(1).strip()}),
    (re.compile(r"^(?:show|list) (?:the )?maintenance history$"), "maintenance_history", None),
    (re.compile(r"^(?:show|list|get) (?:me )?(?:the )?(.+?) maintenance history$"),
     "maintenance_history", lambda m: {"equipment": m.group(1).strip()}),
    (re.compile(r"^(?:show|list|get) (?:me )?(?:the )?(.+?) inspection history$"),
     "inspection_history", lambda m: {"equipment": m.group(1).strip()}),
    (re.compile(r"^(?:show|list) (?:the )?(?:maintenance )?(?:schedule|tasks?) "
                r"(?:for )?(?:today|this week)$"), "maintenance_due", None),
    (re.compile(r"^(?:show|list) (?:the )?maintenance due(?: within (\d+) days?)?$"),
     "maintenance_due", lambda m: {"days": int(m.group(1))} if m.group(1) else {}),
    (re.compile(r"^schedule maintenance (?:for|on) (.+?)(?: on (\d{4}-\d{2}-\d{2}))?$"),
     "schedule_maintenance",
     lambda m: {"equipment": m.group(1).strip(), "due_date": m.group(2) or ""}),
    (re.compile(r"^(?:show|list) (?:the )?inspection history (?:for|of|on) (.+?)\.?$"),
     "inspection_history", lambda m: {"equipment": m.group(1).strip()}),

    (re.compile(r"^(?:show|list) (?:the )?(?:active |open |current )?"
                r"(?:industrial |plant )?alerts$"), "list_plant_alerts", None),
    (re.compile(r"^(?:show|list) (?:the )?(?:all )?(critical|high|medium|low) "
                r"(?:priority )?alerts$"),
     "list_plant_alerts", lambda m: {"severity": m.group(1).lower()}),
    (re.compile(r"^acknowledge alert (.+)$"),
     "acknowledge_alert", lambda m: {"alert_id": m.group(1).strip()}),
    (re.compile(r"^resolve alert (.+)$"),
     "resolve_alert", lambda m: {"alert_id": m.group(1).strip()}),

    (re.compile(r"^(?:show|list) (?:the )?(?:equipment |spare[- ]?parts?|parts )?inventory$"),
     "inventory_summary", None),
    (re.compile(r"^(?:show|list) (?:the )?(?:parts |items )?(?:below|under) minimum(?: stock)?$"),
     "inventory_summary", lambda m: {"low_stock_only": True}),
    (re.compile(r"^(?:find|look up|search for) (?:the )?part (.+)$"),
     "find_part", lambda m: {"query": m.group(1).strip()}),

    (re.compile(r"^(?:show|check) (?:the )?(?:condition |plant )?monitoring(?: status| data)?$"),
     "monitoring_status", None),
    (re.compile(r"^(?:show|plot) (?:the )?trend (?:for|of) (.+?)$"),
     "equipment_trend", lambda m: {"equipment": m.group(1).strip()}),

    (re.compile(r"^(?:show|check) (?:the )?safety (?:status|position|summary)$"),
     "safety_status", None),
    (re.compile(r"^(?:find|search for|get) (?:the )?(?:sop|procedure|manual|instruction)s? "
                r"(?:for|on|about) (.+?)\.?$"), "find_sop", lambda m: {"topic": m.group(1).strip()}),

    (re.compile(r"^(?:show|list) (?:the )?permits?(?: to work)?$"), "list_permits", None),
    (re.compile(r"^(?:show|list) (?:the )?(?:active )?(?:isolations?|lockouts?|loto)(?: records?)?$"),
     "list_isolations", None),

    (re.compile(r"^export (?:the |today'?s? )?(?:equipment |spare[- ]?parts? |plant )?"
                r"(equipment|maintenance|inspections?|alerts?|"
                r"inventory|permits?|loto|readings|tasks)(?: history| data| list| records?)? "
                r"to (excel|xlsx|csv)$"),
     "export_plant_data",
     lambda m: {"dataset": _plural_dataset(m.group(1)),
                "file_format": "csv" if m.group(2).lower() == "csv" else "xlsx"}),
    (re.compile(r"^(?:create|make) (?:a |an )?(?:csv|excel|spreadsheet) of (?:the |recent )?"
                r"(equipment|maintenance|inspections?|alerts?|inventory|permits?|loto|readings|"
                r"tasks)(?: history| data| records?)?$"),
     "export_plant_data", lambda m: {"dataset": _plural_dataset(m.group(1))}),

    (re.compile(r"^(?:generate|create|make|write) (?:a |an |the )?maintenance report"
                r"(?: (?:for|on|about) (.+?))?(?: (?:and )?save (?:it )?as word)?\.?$"),
     "maintenance_report", lambda m: {"equipment": (m.group(1) or "").strip()}),
    (re.compile(r"^(?:generate|create|make|write) (?:a |an |the )?safety report\.?$"),
     "safety_report", None),
    (re.compile(r"^(?:generate|create|make|write) (?:a |an |the )?(?:inspection )?"
                r"(?:word )?(?:approval|acceptance) note (?:for|on) (.+?)\.?$"),
     "approval_note", lambda m: {"equipment": m.group(1).strip()}),
    (re.compile(r"^(?:generate|create|make) (?:a |an |the )?(?:management |operations? )?"
                r"(?:presentation|deck|powerpoint|ppt) (?:of|on|about|summarising|summarizing) "
                r"(?:the )?(?:current |today'?s? )?(?:operations?|plant|maintenance)"
                r"(?: situation| status| position)?\.?$"), "operations_presentation", None),
    (re.compile(r"^(?:generate|create|make) (?:a |an |the )?(?:presentation|deck|powerpoint|ppt) "
                r"(?:for|about|reviewing) (?:equipment )?([A-Za-z]{1,4}[- ]?\d{2,5}[A-Za-z]?)\.?$"),
     "equipment_presentation", lambda m: {"equipment": m.group(1).strip()}),

    # ---- orchestration introspection. Placed before the generic system
    # routes so "show the pipeline" is not swallowed by "show the ... status".
    (re.compile(r"^(?:show|display|what(?:'s| is)) (?:the |your )?"
                r"(?:current )?(?:pipeline|orchestrat(?:or|ion))"
                r"(?: status| doing)?$"), "pipeline_status", None),
    (re.compile(r"^(?:show|list|what) (?:are |is )?(?:the |your )?"
                r"(?:specialist |available )?agents?(?: roster| list| do you have)?$"),
     "list_specialists", None),
    (re.compile(r"^(?:show|list|display) (?:the |your )?"
                r"(?:model )?(?:routing|router)(?: table| map)?$"), "model_routing", None),
    (re.compile(r"^(?:which|what) models? (?:do you )?use(?: for what| for which task)?$"),
     "model_routing", None),
    (re.compile(r"^(?:release|unload|free) (?:the |all )?models?"
                r"(?: from memory| memory)?$"), "release_models", None),
    (re.compile(r"^(?:show|list) (?:the )?validat(?:ors|ion)(?: report| layer)?$"),
     "validation_report", None),
    (re.compile(r"^how do you validate(?: (?:the )?output)?$"), "validation_report", None),
    (re.compile(r"^(?:explain|describe) (?:the )?last (?:run|request|task)$"),
     "explain_last_run", None),
    (re.compile(r"^how did you do that$"), "explain_last_run", None),

    (re.compile(r"^(?:show|display) (?:the )?system (?:information|info|status)$"),
     "system_information", None),
    (re.compile(r"^(?:show|view|open) (?:the )?logs?(?: file)?$"), "view_logs", None),
    (re.compile(r"^(?:show|view) (?:the )?(?:recent )?(error|warning)s?(?: from the)? logs?$"),
     "view_logs", lambda m: {"level": m.group(1).upper()}),
    (re.compile(r"^(?:show|list) (?:the )?(?:plant )?data sources$"), "plant_data_sources", None),
    (re.compile(r"^(?:show|list) (?:my |the )?(?:recent |generated |created )?"
                r"(?:documents|reports|files)(?: i(?:'ve)? (?:made|generated|created))?$"),
     "list_generated_documents", None),
    # Filtered by type, so "show generated presentations" reaches the same
    # listing rather than falling through to the model.
    (re.compile(r"^(?:show|list) (?:my |the )?(?:recent |generated |created )?"
                r"(word documents?|documents?|presentations?|decks?|slides?|"
                r"spreadsheets?|workbooks?|pdfs?|reports?)$"),
     "list_generated_documents",
     lambda m: {"kind": {"presentation": "pptx", "presentations": "pptx",
                         "deck": "pptx", "decks": "pptx",
                         "slide": "pptx", "slides": "pptx",
                         "spreadsheet": "xlsx", "spreadsheets": "xlsx",
                         "workbook": "xlsx", "workbooks": "xlsx",
                         "pdf": "pdf", "pdfs": "pdf",
                         "word document": "docx", "word documents": "docx",
                         }.get(m.group(1).strip(), "")}),
    (re.compile(r"^(?:show|list) (?:my |the )?(?:generated )?(docx|xlsx|pptx|pdf|csv|word|"
                r"excel|powerpoint) (?:documents|files|reports)$"),
     "list_generated_documents",
     lambda m: {"kind": {"word": "docx", "excel": "xlsx",
                         "powerpoint": "pptx"}.get(m.group(1), m.group(1))}),
    (re.compile(r"^(?:find|search for) (?:the )?(?:generated )?document (.+)$"),
     "find_generated_document", lambda m: {"query": m.group(1).strip()}),
    (re.compile(r"^(?:load|seed) (?:the )?(?:industrial |plant )?demo data$"),
     "seed_industrial_demo_data", None),

    # Phrasings that put the noun before the format word, or the day before
    # the noun. Real people say both ways round.
    (re.compile(r"^(?:make|create|build) (?:me )?(?:an? |the )?(?:equipment |spare[- ]?parts? )?"
                r"inventory (?:spreadsheet|excel|xlsx|list|export)$"),
     "export_plant_data", lambda m: {"dataset": "inventory"}),
    (re.compile(r"^(?:make|create|build) (?:me )?(?:an? |the )?"
                r"(equipment|maintenance|inspections?|alerts?|permits?|readings) "
                r"(?:spreadsheet|excel|xlsx|export)$"),
     "export_plant_data", lambda m: {"dataset": _plural_dataset(m.group(1))}),
    (re.compile(r"^(?:show|list) (?:the )?(?:today'?s?|this week'?s?) maintenance "
                r"(?:schedule|tasks?|jobs?)$"), "maintenance_due", None),
    (re.compile(r"^(?:show|list) (?:the )?maintenance (?:schedule|plan)$"),
     "maintenance_due", None),

    # --- Tasks, meetings and the day's agenda -------------------------------
    # Appended, so every route above keeps its precedence. The plain
    # "add todo X" / "list todos" forms already exist further up and are
    # untouched; these add the deadline, owner and meeting phrasings.
    (re.compile(r"^(?:add|create|new) (?:a )?task (.+?) (?:due|by) (.+)$"),
     "add_todo", lambda m: {"text": m.group(1).strip(), "due_date": m.group(2).strip()}),
    (re.compile(r"^(?:remind me to|i need to) (.+?) (?:due |by )(.+)$"),
     "add_todo", lambda m: {"text": m.group(1).strip(), "due_date": m.group(2).strip()}),
    (re.compile(r"^(?:add|create|new) (?:an? )?urgent task (.+)$"),
     "add_todo", lambda m: {"text": m.group(1).strip(), "priority": "high"}),
    (re.compile(r"^(?:show|list|what(?:'s| is)) (?:my |the )?"
                r"(?:overdue|late|outstanding) (?:tasks?|items?|to-?dos?)$"),
     "overdue_tasks", None),
    (re.compile(r"^what(?:'s| is) (?:due |overdue )(?:today|now)$"),
     "overdue_tasks", None),
    (re.compile(r"^(?:show|list) (?:my |the )?tasks? (?:due )?(?:for |on )?(today|tomorrow|this week)$"),
     "list_todos", lambda m: {"due_before": m.group(1).strip(),
                              "include_completed": False}),

    (re.compile(r"^(?:schedule|set up|arrange|book) (?:a )?meeting "
                r"(?:about |on |for |re )?(.+?) (?:on |for )(today|tomorrow|\d{4}-\d{2}-\d{2}) "
                r"at (\d{1,2}[:.]\d{2})$"),
     "schedule_meeting", lambda m: {"title": m.group(1).strip(),
                                    "date": m.group(2).strip(),
                                    "time": m.group(3).strip().replace(".", ":")}),
    (re.compile(r"^(?:schedule|set up|arrange|book) (?:a )?meeting "
                r"(?:about |on |for |re )?(.+?) (?:on |for )(today|tomorrow|\d{4}-\d{2}-\d{2})$"),
     "schedule_meeting", lambda m: {"title": m.group(1).strip(),
                                    "date": m.group(2).strip()}),
    (re.compile(r"^(?:schedule|set up|arrange|book) (?:a )?meeting (?:about |on |re )?(.+)$"),
     "schedule_meeting", lambda m: {"title": m.group(1).strip()}),
    (re.compile(r"^(?:show|list) (?:my |the )?meetings?(?: for (today|tomorrow|this week))?$"),
     "list_meetings", lambda m: {"date": (m.group(1) or "").strip()}),
    (re.compile(r"^(?:show|what(?:'s| is)) (?:my |the )?"
                r"(?:agenda|schedule|day|plan)(?: for)? ?(today|tomorrow|\d{4}-\d{2}-\d{2})?$"),
     "agenda_for", lambda m: {"date": (m.group(1) or "today").strip()}),
    (re.compile(r"^what(?:'s| is) (?:on |happening )?(?:today|on today)$"),
     "agenda_for", lambda m: {"date": "today"}),

    (re.compile(r"^what(?:'s| is) ([\d\s()+\-*/.%]+)\??$"), "calculate", lambda m: {"expression": m.group(1).strip()}),
]

# ---------------------------------------------------------------------------
# ZEUS routes: the specialist agents, the engineering engine, the security
# centre. Prepended so they win over the generic catch-alls ("open (.+)",
# "convert (.+) to (.+)", "calculate (.+)"), and anchored tightly so they do
# not swallow anything those routes used to answer.
# ---------------------------------------------------------------------------
_TAG = r"([A-Za-z]{1,4}-?\d{2,5}[A-Za-z]?)"
_FILE = r"[\"']?([A-Za-z]:[\\/][^\"']+|[^\"'\s]+\.(?:pdf|docx|xlsx|xlsm|csv|pptx|png|jpe?g|tiff?|bmp|txt|md))[\"']?"


def _agent_args(agent: str, rest: str) -> dict:
    rest = (rest or "").strip()
    file_match = re.search(_FILE, rest, re.IGNORECASE)
    tag_match = re.fullmatch(_TAG, rest)
    args = {"agent": agent, "task": rest}
    if file_match:
        args["document_path"] = file_match.group(1)
    elif tag_match:
        args["equipment"] = tag_match.group(1).upper()
    return args


_AGNI_ROUTES: List[Tuple[re.Pattern, str, Optional[Callable[[re.Match], dict]]]] = [
    (re.compile(r"^(?:run|use|start|ask) (?:the )?(inspection|document|engineering|coding|code|"
                r"calculation|report|approval|research|planning|safety|operations) agent"
                r"(?: (?:on|for|to|with|about))?(?: (.+?))?\.?$"),
     "run_industrial_agent", lambda m: _agent_args(m.group(1), m.group(2) or "")),
    (re.compile(r"^inspect (?:the )?(?:inspection )?(?:report|document|scan|image|photo|file)? ?" + _FILE + r"$",
                re.IGNORECASE),
     "run_industrial_agent", lambda m: {"agent": "inspection", "task": "Review this inspection report",
                                        "document_path": m.group(1)}),
    (re.compile(r"^(?:summari[sz]e|read and summari[sz]e) (?:the )?(?:document|file|report|pdf)? ?" + _FILE + r"$",
                re.IGNORECASE),
     "run_industrial_agent", lambda m: {"agent": "document", "task": "Summarise this document.",
                                        "document_path": m.group(1)}),
    (re.compile(r"^(?:write|create|generate) (?:a |some )?(?:python )?(?:script|code|program) "
                r"(?:to|that|which) (.+)$"),
     "run_industrial_agent", lambda m: {"agent": "coding", "task": m.group(1).strip()}),
    (re.compile(r"^(?:research|investigate) (.+)$"),
     "run_industrial_agent", lambda m: {"agent": "research", "task": m.group(1).strip()}),
    (re.compile(r"^(?:make|create|draw up|build) (?:a |an )?(?:work )?plan (?:for|to) (.+)$"),
     "run_industrial_agent", lambda m: {"agent": "planning", "task": m.group(1).strip()}),
    (re.compile(r"^(?:calculate|compute|what is|what's|find) (?:the )?(pump (?:shaft )?power|"
                r"pressure drop|corrosion rate|remaining life|(?:minimum |required )?wall thickness|"
                r"mawp|maximum allowable working pressure|reynolds number|heat duty|lmtd|"
                r"log mean temperature difference|npsh(?:a| available)?|flow velocity|"
                r"head from pressure)\b(.*)$"),
     "run_industrial_agent", lambda m: {"agent": "calculation", "task": m.group(0)}),
    (re.compile(r"^convert (-?\d+(?:\.\d+)?) ?([a-z°/0-9^*]+) (?:to|in|into) ([a-z°/0-9^*]+)$"),
     "unit_convert", lambda m: {"value": m.group(1), "from_unit": m.group(2), "to_unit": m.group(3)}),
    (re.compile(r"^solve (.+=.+) for ([a-z]\w*)$"),
     "solve_equation", lambda m: {"equation": m.group(1).strip(), "unknown": m.group(2)}),
    (re.compile(r"^(?:evaluate|compute exactly) (.+)$"),
     "engineering_calculate", lambda m: {"expression": m.group(1).strip()}),
    (re.compile(r"^(?:show|list) (?:the )?engineering formulas?$"), "list_engineering_formulas", None),

    (re.compile(r"^(?:(?:show|check) (?:the )?security(?: status| centre| center| state)?|"
                r"(?:the )?security (?:status|centre|center|state))\??$"),
     "security_status", None),
    (re.compile(r"^(?:prove it|are we sovereign|(?:show |run )?(?:the )?sovereign(?:ty)? "
                r"(?:report|check|mode|status))\??$"), "sovereignty_report", None),
    (re.compile(r"^(?:(?:verify|check) (?:the )?audit(?: chain| log| trail)?|audit chain(?: status)?)\??$"),
     "verify_audit_chain", None),
    (re.compile(r"^(?:(?:show|check) (?:the )?(?:network|egress)(?: status| policy)?|"
                r"(?:network|egress) (?:status|policy|mode))\??$"),
     "network_status", None),
    (re.compile(r"^(?:engage |start )?(?:emergency )?lockdown(?: now)?$"), "engage_lockdown", None),
    (re.compile(r"^(?:(?:show|check|verify) (?:the )?)?(?:model integrity|trusted models)\??$"),
     "model_integrity_report", None),
    (re.compile(r"^(?:(?:show|check) (?:the )?(?:hardware|gpu|machine)(?: profile| status)?|"
                r"(?:hardware|gpu|machine) (?:profile|status))\??$"),
     "hardware_profile", None),
    (re.compile(r"^(?:show|list) (?:the )?models$"), "model_routing", None),
    (re.compile(r"^(?:(?:show|what is|what's) (?:the )?)?active agents?\??$"), "pipeline_status", None),
    (re.compile(r"^(?:(?:show|list) (?:my |the )?)?(?:artifacts|generated files)\??$"), "list_artifacts", None),
    (re.compile(r"^(?:show|check) (?:the )?(?:field |plant )?telemetry$"), "plant_telemetry", None),
    (re.compile(r"^(?:show |check )?(?:the )?face (?:login |recognition )?status\??$"),
     "face_status", None),
    (re.compile(r"^(?:enrol|enroll|register|set up|add) (?:my )?face(?: login| id)?$"),
     "register_face", None),
    (re.compile(r"^(?:verify|test|check) (?:my )?face(?: login)?$"), "verify_my_face", None),
    (re.compile(r"^(?:remove|delete|forget) (?:my )?face(?: login| template)?$"), "remove_face", None),
    (re.compile(r"^(?:show |check )?(?:the )?wfp(?: firewall)?(?: status)?\??$"),
     "wfp_firewall_status", None),
    (re.compile(r"^(?:apply|enable|install|turn on) (?:the )?(?:wfp |sovereign )?firewall$"),
     "apply_wfp_firewall", None),
    (re.compile(r"^(?:remove|disable|uninstall|turn off) (?:the )?(?:wfp |sovereign )?firewall$"),
     "remove_wfp_firewall", None),
    (re.compile(r"^(?:show |list )?(?:the )?(?:blocked connections|what (?:did )?the firewall block(?:ed)?)\??$"),
     "wfp_blocked_connections", None),
    (re.compile(r"^import (?:the )?(?:plant |equipment |sensor )?readings? (?:from )?[\"']?(.+?\.csv)[\"']?$"),
     "import_readings_csv", lambda m: {"path": m.group(1).strip(), "source": "operator CSV import"}),
    (re.compile(r"^(?:process|transcribe|summari[sz]e) (?:the )?meeting (?:recording|audio) (.+)$"),
     "process_meeting_audio", lambda m: {"audio_path": m.group(1).strip().strip("\"'")}),
]
_ROUTES[:0] = _AGNI_ROUTES


_HELP_PATTERN = re.compile(r"^(?:help|what can you do|list tools|show tools|show commands)\??$")

# ZEUS (jarvis/): field updates, project questions, reports and sync. Checked
# before the document writer and the personal schedule planner, so "what changed
# between the 1 and 10 September schedule updates" is a project question, not a
# request to edit the weekly planner, and "generate the progress report" is built
# from the project record, not written as an essay.
_JARVIS_FIELD_UPDATE = re.compile(r"^(?:jarvis[,:]?\s+)?(?:field|site|progress) (?:update|report|note|entry)"
                                  r"\s*[:\-,]?\s+(.+)$", re.IGNORECASE | re.DOTALL)
_JARVIS_QUESTIONS = re.compile(
    r"\bwhy did (?:the |our |my )?(?:project |forecast |package )?(?:finish|completion|end|handover)(?: date)?"
    r" (?:move|slip|change|shift)"
    r"|^what (?:has )?changed between .+\b(?:schedule|update|version|programme)s?\b"
    r"|^what did we know about\b|^when did we first (?:know|learn|see|hear)\b"
    r"|^(?:what|which) evidence (?:existed|was there|did we have) before\b"
    r"|^(?:show |give me |build )?(?:the )?(?:delay )?chronology\b|\bdelay chronology\b"
    r"|^(?:show |list )?(?:the )?(?:contract )?notice deadlines?\b"
    r"|^(?:show |give me )?(?:the )?(?:project[- ])?learning cards?\b"
    r"|^how long did (?:comparable|similar|past)\b", re.IGNORECASE)
_JARVIS_REPORTS = [
    (re.compile(r"^(?:generate|create|make|export|build|prepare) (?:the |a )?(?:project )?progress report$", re.I),
     "export_progress_report", {}),
    (re.compile(r"^(?:generate|create|make|export|build|prepare) (?:the |a )?(?:formal )?delay chronology"
                r"(?: (?:as |in )?(?:a )?(docx|xlsx|pdf|word|excel))?$", re.I), "export_delay_chronology", "fmt"),
    (re.compile(r"^(?:generate|create|make|export|build|prepare) (?:the |an )?evidence pack(?: for (\S+))?$", re.I),
     "export_evidence_pack", "activity"),
    (re.compile(r"^(?:generate|create|make|export|build|prepare) (?:the |a )?schedule analysis(?: workbook)?$", re.I),
     "export_schedule_analysis", {}),
]
_JARVIS_SIMPLE = [
    (re.compile(r"^(?:jarvis )?sync(?: now| the field (?:data|updates))?$|^synchron[iy][sz]e(?: now)?$", re.I),
     "request_sync"),
    (re.compile(r"^(?:(?:show |check )?(?:the )?(?:offline|sync) status|are we (?:online|offline))\??$", re.I),
     "jarvis_sync_status"),
    (re.compile(r"^load (?:the )?(?:jarvis )?demo(?: project| scenario| data)?$", re.I), "seed_jarvis_demo"),
    (re.compile(r"^(?:show |check )?(?:the )?(?:project|schedule) (?:status|health)\??$", re.I), "jarvis_overview"),
]


def _extract_jarvis_command(original: str, lowered: str) -> Optional[tuple]:
    text = original.strip()
    m = _JARVIS_FIELD_UPDATE.match(text)
    if m and m.group(1).strip():
        return "record_field_update", {"text": m.group(1).strip()}
    for pattern, tool_name, arg in _JARVIS_REPORTS:
        m = pattern.match(lowered)
        if m:
            args = {}
            if arg == "fmt":
                fmt = (m.group(1) or "docx").lower()
                args["fmt"] = {"word": "docx", "excel": "xlsx"}.get(fmt, fmt)
            elif arg == "activity" and m.group(1):
                args["activity"] = text.split()[-1].strip(".\"'").upper()
            return tool_name, args
    for pattern, tool_name in _JARVIS_SIMPLE:
        if pattern.match(lowered):
            return tool_name, {}
    if _JARVIS_QUESTIONS.search(text):
        return "jarvis_ask", {"question": text}
    return None

# UI-control commands: these don't execute a tool - they tell the dashboard
# to change itself (switch tabs, focus a panel). The response carries a
# 'ui_action' the dashboard applies on its main thread after the signal
# round-trip, so a typed/spoken command can genuinely drive the UI, not
# just the other way around.
_UI_ACTION_PATTERN = re.compile(
    r"^(?:switch to|show|go to|open|focus)(?: the)? (voice|system|tasks|memory|calendar|todo|notes|to-?do)"
    r"(?: tab| view| panel)?$"
)


def match_ui_action(text: str) -> Optional[Dict[str, Any]]:
    m = _UI_ACTION_PATTERN.match(text.lower().strip().rstrip("?.! "))
    if not m:
        return None
    tab = m.group(1).upper().replace("TO-DO", "TODO")
    return {"type": "switch_tab", "tab": tab}

def _plural_dataset(word: str) -> str:
    """Map what a person says to the dataset name plant_docs exports.
    'alert', 'alerts', 'inspection' and 'inspections' are all the same table."""
    word = (word or "").strip().lower()
    return {"alert": "alerts", "inspection": "inspections", "permit": "permits",
            "task": "tasks", "reading": "readings"}.get(word, word)


_WRITE_TRIGGER = re.compile(r"^(?:can you |could you |please )?"
                            r"(?:write|create|make|generate|draft|produce)\b")
_WRITE_DOC_TYPES = ("word document", "word doc", "word file", "docx", "assignment",
                    "essay", "report", "article", "paper", "document", "memo", "letter")
_WRITE_CONNECTOR = re.compile(r"^(?:on|about|for|covering|regarding)\s+")

# Documents that are built from the plant database rather than written by the
# model. These have their own routes further down; the generic writer must not
# intercept them or "generate a maintenance report" would produce an essay
# about maintenance instead of the actual records for that pump.
_PLANT_DOCUMENT_PHRASES = re.compile(
    r"\b(?:maintenance report|safety report|approval note|acceptance note|"
    r"operations presentation|equipment presentation|inspection approval|"
    r"executive report|analysis report|management report|executive presentation|"
    r"management presentation|board presentation|analysis workbook|"
    r"executive workbook|briefing pack|management pack|board pack)\b")


def _extract_write_topic(lowered: str) -> Optional[str]:
    """
    Flexible parser for document requests - handles arbitrary phrasing
    ('write my assignment on X', 'can you write an essay about X', 'create a
    Word document about X', 'write me a report for X') that a single rigid
    regex kept missing.
    """
    if not _WRITE_TRIGGER.match(lowered):
        return None
    if _PLANT_DOCUMENT_PHRASES.search(lowered):
        return None
    for doc_type in _WRITE_DOC_TYPES:
        idx = lowered.find(doc_type)
        if idx == -1:
            continue
        rest = lowered[idx + len(doc_type):].strip()
        rest = _WRITE_CONNECTOR.sub("", rest).strip()
        if rest:
            return rest
    return None


_SCHEDULE_CREATE_TRIGGERS = (
    "plan my week", "plan my schedule", "plan my day", "plan out my week",
    "help me plan my week", "help me plan my day", "schedule my week",
    "make me a schedule", "build me a schedule", "create a schedule",
    "create my schedule", "make a weekly schedule", "plan a weekly schedule",
)
_SCHEDULE_UPDATE_TRIGGERS = (
    "update my schedule", "update the schedule", "change my schedule",
    "reschedule", "update schedule", "modify my schedule", "edit my schedule",
    "adjust my schedule",
)


def _extract_schedule_request(original: str, lowered: str) -> Optional[str]:
    """Scheduling requests are long free-form paragraphs of constraints,
    not short commands - preserve the original text (casing/specifics
    intact) rather than matching a rigid regex against the lowercased text."""
    if any(t in lowered for t in _SCHEDULE_UPDATE_TRIGGERS):
        return None  # let the update path handle it, checked separately
    if any(t in lowered for t in _SCHEDULE_CREATE_TRIGGERS):
        return original.strip()
    return None


def _extract_schedule_update(original: str, lowered: str) -> Optional[str]:
    if any(t in lowered for t in _SCHEDULE_UPDATE_TRIGGERS):
        return original.strip()
    return None


class DecisionEngine:
    def __init__(self):
        self.registry = registry
        # Ensure plugin-provided tools (e.g. calculator) are registered even
        # if the caller constructs DecisionEngine without going through
        # main.py's startup sequence.
        try:
            from plugins.plugin_loader import discover_plugins
            discover_plugins()
        except Exception as e:
            logger.warning(f"Plugin discovery skipped: {e}")

        # The orchestrator runs the pipeline (classify -> plan -> route ->
        # execute -> validate -> auto-fix -> audit) but does NOT own the
        # command table or the chat behaviour: those are injected from here,
        # so every route and every reply is produced by exactly the same code
        # as before this layer existed.
        self.orchestrator = None
        try:
            from core.orchestration.orchestrator import get_orchestrator
            self.orchestrator = get_orchestrator(
                direct_matcher=self._match_direct_command,
                chat_handler=self._chat_response,
                formatter=self._format_tool_output)
        except Exception as e:
            logger.warning(f"Orchestrator unavailable, using the direct path: {e}")

    # ---------------------------------------------------------- fast path --

    @staticmethod
    def _restore_original_case(args: dict, match: re.Match, original: str) -> dict:
        """Route patterns are matched against lowercased text, which used to
        mean a captured file path came back lowercased ('D:/Reports' ->
        'd:/reports'). Re-read each captured group from the ORIGINAL string
        using the match spans so paths, tags and names keep their case.
        Values an arg_fn transformed (e.g. '*.pdf' built from 'pdf') are left
        alone because they no longer equal any captured group."""
        spans = {}
        for index in range(1, (match.re.groups or 0) + 1):
            captured = match.group(index)
            if captured is None:
                continue
            start, end = match.span(index)
            spans[captured.strip()] = original[start:end].strip()
        return {k: (spans.get(v, v) if isinstance(v, str) else v) for k, v in args.items()}

    def _match_direct_command(self, text: str) -> Optional[tuple]:
        original = text.strip().rstrip("?.! ")
        lowered = text.lower().strip().rstrip("?.! ")

        jarvis = _extract_jarvis_command(original, lowered)
        if jarvis:
            return jarvis

        write_topic = _extract_write_topic(lowered)
        if write_topic:
            return "write_document", {"topic": write_topic}

        schedule_update = _extract_schedule_update(text, lowered)
        if schedule_update:
            return "update_schedule", {"change_description": schedule_update}

        schedule_request = _extract_schedule_request(text, lowered)
        if schedule_request:
            return "create_schedule", {"details": schedule_request}

        for pattern, tool_name, arg_fn in _ROUTES:
            match = pattern.match(lowered)
            if match:
                args = arg_fn(match) if arg_fn else {}
                # len() guard: lower() preserves length for ASCII, which is
                # what command text is; if some locale-specific character
                # changed length the spans would be wrong, so keep the old
                # behaviour rather than slice incorrectly.
                if args and len(original) == len(lowered):
                    args = self._restore_original_case(args, match, original)
                return tool_name, args
        return None

    # ------------------------------------------------------------- chat ----

    WRITTEN_STYLE = (
        "You are ZEUS, a knowledgeable personal AI assistant and sovereign industrial "
        "AI workbench running entirely on this machine with local models only. You help engineers and operators with "
        "industrial work - process, maintenance, inspection, reliability, safety, "
        "documents and data - and you can discuss any other topic too. Never claim "
        "to have run a tool, read a file or checked live plant data unless that "
        "result is in the conversation.\n\n"
        "Answer style: be short and crisp by default - lead with the actual answer, "
        "not a preamble. Match response length to the question: a factual question "
        "gets a few sentences, not a essay; a genuinely complex or 'explain in depth' "
        "request earns more room, but stay relevant throughout - no padding, no "
        "repeating the question back, no unnecessary caveats. Use plain, direct language. "
        "If you don't know something or it's beyond your knowledge, say so briefly rather "
        "than guessing at length."
    )

    def _chat_response(self, text: str, history: List[Dict[str, str]],
                       spoken: bool = False, model: Optional[str] = None,
                       think: bool = False, memory_context: str = "") -> str:
        """Answer in conversation.

        `spoken` selects the style. It is driven by HOW THE REQUEST ARRIVED,
        not by whether `--voice` was passed:

          - typed question, voice on  -> written style, spoken by speech_prep
          - spoken question           -> speakable style, on screen and aloud

        This matters on a CPU-only machine. Generating the answer twice, once
        for the screen and once for the ear, would double the slowest step in
        the system for no benefit anyone asked for. Instead the request's own
        modality decides: someone who spoke to ZEUS is having a conversation
        and wants a conversational reply on screen too, while someone typing
        wants the tables and bullet lists that make a written answer readable.

        Documents, reports and every generated artifact go through their own
        tools and never reach this method, so they are unaffected either way.
        """
        if not ollama_client.is_available():
            return ("I can't reach the local AI model right now. "
                    "Make sure Ollama is running (`ollama serve`) and a model is pulled "
                    "(`ollama pull qwen2.5:7b`). I can still run direct commands like "
                    "'open chrome' or 'take a screenshot'.")

        if not model:
            task_type = model_router.classify_task(text)
            model = model_router.route(task_type)

        style = self.WRITTEN_STYLE
        if spoken:
            try:
                from voice.offline import config_bool
                from voice.speech_prep import voice_system_prompt
                if config_bool("voice_style", True):
                    style = voice_system_prompt()
            except Exception as error:
                # A voice import problem must never cost the user an answer.
                logger.debug(f"voice style unavailable, answering in text: {error}")

        user_content = text
        if memory_context:
            # Recalled memory is background DATA: the rule that says so is in
            # the (trusted, fixed) system prompt; the memories themselves are
            # fenced in the user turn, where they carry no authority.
            from memory.recall import MEMORY_RULE, user_turn
            style = f"{style}\n\n{MEMORY_RULE}"
            user_content = user_turn(memory_context, text)
        messages = [{"role": "system", "content": style}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        return ollama_client.chat(model, messages, think=think, task_type="chat",
                                  purpose="Conversation")

    # ------------------------------------------------------- multi-step ----

    def _run_multistep_task(self, text: str, spoken: bool = False) -> str:
        task = task_manager.create(text)
        task_manager.update(task.id, status=TaskStatus.PLANNING)

        steps = decompose(text)
        if not steps:
            task_manager.update(task.id, status=TaskStatus.FAILED,
                                 errors=["Could not decompose task into tool calls"])
            # Fall back to a plain chat answer rather than dead-ending.
            return self._chat_response(text, [], spoken=spoken)

        task_manager.update(task.id, status=TaskStatus.RUNNING)
        results = []
        for step in steps:
            if task.is_cancelled():
                task_manager.update(task.id, status=TaskStatus.CANCELLED)
                return f"Task #{task.id} cancelled."

            task_manager.add_step(task.id, step["description"])
            from security.policy import origin_scope
            with origin_scope("planner"):
                result = self.registry.execute(step["tool"], **step["args"])
            results.append({"step": step["description"], "success": result.success,
                             "output": result.output, "error": result.error})
            if not result.success:
                task_manager.update(task.id, status=TaskStatus.FAILED,
                                     errors=task.errors + [result.error or "unknown error"])
                return (f"Task #{task.id} failed at step '{step['description']}': "
                        f"{result.error}")

        task_manager.update(task.id, status=TaskStatus.COMPLETED, progress=1.0, result=results)

        summary_lines = [f"Task #{task.id} complete ({len(results)} step(s)):"]
        for r in results:
            summary_lines.append(f"  - {r['step']}: done")
        return "\n".join(summary_lines)

    # -------------------------------------------------------- entry point --

    def process(self, text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = context or {}
        history = context.get("history", [])
        # Set only when the request arrived through the microphone. Commands,
        # tool output and document generation ignore it entirely - it reaches
        # nothing but the conversational answer's phrasing.
        spoken = bool(context.get("spoken"))

        if _HELP_PATTERN.match(text.lower().strip()):
            catalogue = self.registry.describe_all()
            return {"response": f"Here's what I can do directly:\n{catalogue}",
                    "intent": IntentType.QUESTION.value, "refresh": []}

        ui_action = match_ui_action(text)
        if ui_action:
            return {"response": f"Switched to {ui_action['tab'].title()} view.",
                    "intent": IntentType.COMMAND.value, "refresh": [], "ui_action": ui_action}

        # ---- the pipeline. Falls through to the original path below if the
        # orchestration layer could not be loaded, so a problem there degrades
        # to the previous behaviour instead of taking the assistant down.
        if self.orchestrator is not None:
            try:
                return self.orchestrator.run(text, context).to_engine_response()
            except Exception as e:
                logger.error(f"Orchestrated run failed, falling back: {e}")

        direct = self._match_direct_command(text)
        if direct:
            tool_name, args = direct
            result = self.registry.execute(tool_name, **args)
            if result.success:
                response = self._format_tool_output(tool_name, result.output)
            else:
                response = f"Couldn't do that: {result.error}"
            from core.agents import get_agent_for_tool
            return {"response": response, "intent": IntentType.COMMAND.value, "refresh": [],
                    "agent": get_agent_for_tool(tool_name)}

        intent = classify_intent(text)

        if intent == IntentType.MULTI_STEP_TASK:
            response = self._run_multistep_task(text, spoken=spoken)
            return {"response": response, "intent": intent.value, "refresh": ["tasks"]}

        if intent == IntentType.TASK:
            # Treat simple tasks as a single-step plan attempt, falling back to chat.
            response = self._run_multistep_task(text)
            return {"response": response, "intent": intent.value, "refresh": ["tasks"]}

        response = self._chat_response(text, history, spoken=spoken)
        return {"response": response, "intent": intent.value, "refresh": [], "agent": "conversation"}

    @staticmethod
    def _format_tool_output(tool_name: str, output: Any) -> str:
        try:
            from core.agni_present import present
            readable = present(tool_name, output)
            if readable:
                return readable
        except Exception as error:
            logger.debug(f"presenter for {tool_name} failed: {error}")
        if isinstance(output, dict):
            if "error" in output:
                return f"Error: {output['error']}"
            parts = [f"{k}: {v}" for k, v in output.items()]
            return " | ".join(parts)
        return str(output)
