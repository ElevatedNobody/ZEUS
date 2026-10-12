"""
Industrial plant capabilities: equipment, maintenance, inspections, alerts,
inventory, permits, LOTO, exports, reports, routing and memory integration.

Every test runs against a real temporary SQLite database and produces real
files - nothing about the data layer or the document generators is mocked.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory import database  # noqa: E402

DB_PATH = Path(tempfile.gettempdir()) / "medha_test_plant.db"

from core.security_manager import security_manager  # noqa: E402
from core.tool_registry import registry  # noqa: E402
from industrial import plant_data as pd  # noqa: E402
from industrial import plant_docs as docs  # noqa: E402
from industrial import plant_ops as ops  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="medha_plant_docs_"))


def _fresh():
    database.DB_PATH = DB_PATH
    database.set_active_user(None)
    if DB_PATH.exists():
        DB_PATH.unlink()
    database.init_db()
    pd.init_plant_schema(force=True)
    pd.seed_demo_data(force=True)


def setup_function(function):
    database.DB_PATH = DB_PATH
    pd._INITIALISED = False
    security_manager.set_confirm_callback(lambda message: True)
    # Generated documents go to a temporary folder, not the user's Documents.
    docs.office.documents_dir = lambda subfolder="": OUT


def setup_module(module):
    _fresh()


# ------------------------------------------------------------ data layer --

def test_plant_tables_live_in_the_existing_database():
    """No second database: the plant tables are in the same SQLite file."""
    _fresh()
    conn = database.get_connection()
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    for table in ("plant_equipment", "plant_maintenance", "plant_alerts", "plant_inventory",
                  "plant_permits", "plant_loto", "plant_readings"):
        assert table in names, f"{table} missing from the shared database"
    # and the pre-existing tables are untouched
    for original in ("messages", "facts", "todos", "document_chunks", "industrial_documents"):
        assert original in names, f"{original} disappeared"


def test_demo_rows_are_labelled_as_demo():
    """Seeded data must never be mistaken for real plant measurements."""
    _fresh()
    sources = pd.data_sources()
    assert sources["demo_rows"] > 0
    assert sources["live_sensor_feed"] is False
    assert "DEMO" in sources["note"] or "demo" in sources["note"].lower()
    for row in pd.list_equipment(limit=100):
        assert row["source"] == "DEMO"


def test_a_reading_must_name_its_source():
    """The integration point for real data refuses anonymous readings."""
    _fresh()
    import pytest
    with pytest.raises(ValueError):
        pd.record_reading("P-204", "vibration_rms", 3.3, source="")
    pd.record_reading("P-204", "vibration_rms", 3.3, source="historian-export")
    assert any(r["source"] == "historian-export" for r in pd.latest_readings("P-204"))


def test_equipment_resolution_handles_natural_phrasing():
    _fresh()
    assert pd.resolve_equipment("P-204")["tag"] == "P-204"
    assert pd.resolve_equipment("Pump P-204")["tag"] == "P-204"
    assert pd.resolve_equipment("the crude feed pump")["tag"] == "P-204"
    assert pd.resolve_equipment("nothing like this") is None


# ------------------------------------------------------------ capability --

def test_operations_summary_counts_what_is_stored():
    _fresh()
    summary = ops.operations_summary()
    assert summary["equipment_total"] == len(pd.list_equipment(limit=999))
    assert summary["open_alerts"] == len(pd.list_alerts(status="open"))
    assert summary["provenance"]["contains_demo_data"] is True
    assert "operational" in ops.operations_status_text()


def test_monitoring_does_not_pretend_to_have_live_sensors():
    _fresh()
    snapshot = ops.monitoring_snapshot()
    assert snapshot["live_feed_connected"] is False
    assert "record_reading" in snapshot["integration_note"]


def test_equipment_dossier_pulls_every_source_together():
    _fresh()
    dossier = ops.equipment_dossier("P-204")
    assert dossier["equipment"]["tag"] == "P-204"
    assert dossier["maintenance_count"] >= 1
    assert dossier["inspections"]
    assert dossier["open_alerts"]
    assert dossier["spare_parts"]
    assert "P-204" in ops.equipment_summary_text("P-204")


def test_maintenance_scheduling_uses_the_existing_calendar():
    """No second task manager: a scheduled job lands in the ZEUS calendar."""
    _fresh()
    before = len(database.list_events() or [])
    result = ops.schedule_maintenance("P-204", "Bearing inspection", due_date="2026-12-01")
    assert result["scheduled"] is True
    assert len(database.list_events() or []) == before + 1


def test_inventory_flags_items_below_minimum():
    _fresh()
    summary = ops.inventory_summary()
    assert summary["item_count"] > 0
    for item in summary["below_minimum"]:
        assert item["quantity"] <= item["minimum_stock"]


def test_alert_lifecycle():
    _fresh()
    alert = pd.raise_alert("Test condition", equipment_tag="P-204", severity="high",
                           source="LOCAL")
    assert alert["status"] == "open"
    acknowledged = pd.acknowledge_alert(alert["alert_id"], by="operator")
    assert acknowledged["status"] == "acknowledged"
    resolved = pd.resolve_alert(alert["alert_id"], by="operator")
    assert resolved["status"] == "resolved"


# --------------------------------------------------------------- safety --

def test_permit_is_never_approved_automatically():
    _fresh()
    permit = ops.request_permit("hot work", "P-204", description="weld a flange")
    assert permit["permit"]["status"] == "requested"
    # An approval without human confirmation must be refused.
    refused = ops.approve_permit(permit["permit"]["permit_id"], "Site Manager", confirmed=False)
    assert refused.get("approved") is False
    assert pd.get_permit(permit["permit"]["permit_id"])["status"] == "requested"


def test_permit_approval_requires_a_named_approver():
    _fresh()
    permit = ops.request_permit("confined space", "V-410")
    result = ops.approve_permit(permit["permit"]["permit_id"], "", confirmed=True)
    assert result.get("approved") is False


def test_approving_a_permit_through_the_tool_requires_confirmation():
    """The registry must refuse the tool outright when the user declines."""
    _fresh()
    permit = ops.request_permit("hot work", "P-204")
    permit_id = permit["permit"]["permit_id"]

    security_manager.set_confirm_callback(lambda message: False)
    declined = registry.execute("approve_permit", permit_id=permit_id, approver="Manager")
    assert declined.success is False
    assert pd.get_permit(permit_id)["status"] == "requested"

    security_manager.set_confirm_callback(lambda message: True)
    approved = registry.execute("approve_permit", permit_id=permit_id, approver="Manager")
    assert approved.success is True
    assert pd.get_permit(permit_id)["status"] == "approved"


def test_loto_release_requires_confirmation_and_a_person():
    _fresh()
    record = ops.record_isolation("P-204", isolation_points="V-1, V-2, MCC breaker")
    loto_id = record["loto"]["loto_id"]
    blocked = ops.set_isolation_state(loto_id, "released", "R. Sharma", confirmed=False)
    assert blocked.get("changed") is False
    assert pd.get_loto(loto_id)["isolation_status"] == "not_isolated"

    anonymous = ops.set_isolation_state(loto_id, "released", "", confirmed=True)
    assert anonymous.get("changed") is False

    done = ops.set_isolation_state(loto_id, "released", "R. Sharma", confirmed=True)
    assert done["changed"] is True
    assert pd.get_loto(loto_id)["released_by"] == "R. Sharma"


def test_loto_and_permit_tools_are_high_risk():
    """Safety-critical tools must sit above the confirmation threshold."""
    from core.security_manager import CONFIRM_THRESHOLD
    for name in ("approve_permit", "set_isolation_state"):
        tool = registry.get(name)
        assert tool is not None, f"{name} is not registered"
        assert tool.risk >= CONFIRM_THRESHOLD, f"{name} would run without confirmation"


def test_safety_overview_declares_its_limits():
    _fresh()
    overview = ops.safety_overview()
    assert "not a substitute" in overview["caution"].lower()


def test_find_sop_reports_an_empty_knowledge_base_honestly():
    _fresh()
    result = ops.find_sop("pump maintenance")
    assert "results" in result or "error" in result
    if result.get("error"):
        assert "empty" in result["error"].lower() or "unavailable" in result["error"].lower()


# ------------------------------------------------------------ documents --

def _assert_real_file(result, extension, minimum_bytes=2000):
    assert "error" not in result, result
    assert result.get("verified") is True, result
    path = Path(result["saved"])
    assert path.exists() and path.is_file()
    assert path.suffix.lower() == f".{extension}"
    assert path.stat().st_size > minimum_bytes
    return path


def test_maintenance_report_produces_a_real_docx():
    _fresh()
    result = docs.maintenance_report_docx("P-204", narrative=False, open_after=False)
    path = _assert_real_file(result, "docx")
    from docx import Document
    text = "\n".join(p.text for p in Document(str(path)).paragraphs)
    assert "P-204" in text
    assert result["records"] >= 1


def test_report_states_when_the_data_is_demo():
    _fresh()
    result = docs.maintenance_report_docx("P-204", narrative=False, open_after=False)
    from docx import Document
    text = "\n".join(p.text for p in Document(str(result["saved"])).paragraphs)
    assert "DEMO" in text


def test_report_works_without_a_language_model():
    """Ollama is not running in this environment - the factual report must
    still be produced, and must say the narrative was skipped."""
    _fresh()
    result = docs.maintenance_report_docx("P-204", narrative=True, open_after=False)
    _assert_real_file(result, "docx")
    assert result["narrative_included"] is False


def test_safety_report_produces_a_real_docx():
    _fresh()
    _assert_real_file(docs.safety_report_docx(narrative=False, open_after=False), "docx")


def test_approval_note_is_not_pre_approved():
    _fresh()
    result = docs.inspection_note_docx("P-204", open_after=False)
    path = _assert_real_file(result, "docx")
    from docx import Document
    text = "\n".join(p.text for p in Document(str(path)).paragraphs).lower()
    assert "not approved until" in text


def test_operations_deck_produces_a_real_pptx():
    _fresh()
    result = docs.operations_deck_pptx(narrative=False, open_after=False)
    _assert_real_file(result, "pptx")
    assert result["slides"] >= 5


def test_equipment_deck_produces_a_real_pptx():
    _fresh()
    result = docs.equipment_deck_pptx("P-204", open_after=False)
    _assert_real_file(result, "pptx")


def test_dataset_export_produces_a_real_xlsx_with_provenance():
    _fresh()
    result = docs.export_dataset_xlsx("inventory", open_after=False)
    path = _assert_real_file(result, "xlsx", minimum_bytes=1000)
    from openpyxl import load_workbook
    workbook = load_workbook(str(path))
    assert "About" in workbook.sheetnames
    about = "\n".join(str(c.value) for row in workbook["About"].iter_rows() for c in row)
    assert "DEMO" in about
    assert workbook["Inventory"].max_row == result["rows"] + 1


def test_every_dataset_exports():
    _fresh()
    for dataset in docs.available_datasets():
        result = docs.export_dataset_xlsx(dataset, open_after=False)
        assert "error" not in result, f"{dataset}: {result}"
        assert result["verified"] is True


def test_csv_export_is_real():
    _fresh()
    result = docs.export_dataset_csv("alerts", open_after=False)
    path = Path(result["saved"])
    assert path.exists()
    assert "alert_id" in path.read_text(encoding="utf-8").splitlines()[0]


def test_unknown_dataset_is_refused_not_faked():
    _fresh()
    result = docs.export_dataset_xlsx("unicorns", open_after=False)
    assert "error" in result
    assert "available" in result


# ---------------------------------------------------------------- tools --

def test_industrial_tools_are_registered():
    expected = ("plant_status", "list_equipment", "equipment_details", "maintenance_history",
                "schedule_maintenance", "inspection_history", "list_plant_alerts",
                "acknowledge_alert", "inventory_summary", "find_part", "safety_status",
                "find_sop", "list_permits", "request_permit", "approve_permit",
                "list_isolations", "record_isolation", "set_isolation_state",
                "export_plant_data", "maintenance_report", "safety_report",
                "operations_presentation", "equipment_presentation", "approval_note",
                "system_information", "view_logs", "plant_data_sources")
    names = {t.name for t in registry.list_tools()}
    missing = [name for name in expected if name not in names]
    assert not missing, f"not registered: {missing}"


def test_original_tools_survive():
    """The office and RAG tools this build already had must still be there."""
    names = {t.name for t in registry.list_tools()}
    for original in ("write_document", "create_presentation", "create_spreadsheet",
                     "convert_file", "index_folder", "ask_knowledge_base",
                     "ask_industrial_kb", "remember_fact"):
        assert original in names, f"{original} disappeared"


def test_tools_execute_through_the_registry():
    _fresh()
    assert registry.execute("plant_status").success
    assert registry.execute("list_plant_alerts").success
    assert registry.execute("inventory_summary").success
    assert registry.execute("system_information").success


def test_optional_parameters_are_not_required():
    """A documented-but-defaulted parameter must not block a tool call."""
    _fresh()
    result = registry.execute("maintenance_history")
    assert result.success, result.error


def test_required_parameters_are_still_enforced():
    assert registry.execute("read_file").success is False
    assert "Missing required" in registry.execute("read_file").error


def test_system_information_hides_no_secrets():
    _fresh()
    info = ops.system_information()
    blob = str(info).lower()
    for secret in ("password", "password_hash", "argon2", "bcrypt"):
        assert secret not in blob, f"system information leaked '{secret}'"


# --------------------------------------------------------------- memory --

def test_pronoun_reference_resolves_to_the_last_equipment():
    """'analyse P-204' then 'the report for that pump' - one memory, no new store."""
    _fresh()
    from industrial import plant_tools
    registry.execute("equipment_details", equipment="Pump P-204")
    assert plant_tools.last_equipment() == "P-204"
    result = registry.execute("maintenance_history", equipment="that pump")
    assert result.output["equipment"] == "P-204"


def test_an_empty_filter_means_everything_not_the_last_equipment():
    """The bug this guards: an omitted filter silently narrowed to one pump."""
    _fresh()
    registry.execute("equipment_details", equipment="P-204")
    alerts = registry.execute("list_plant_alerts").output
    assert alerts["alert_count"] == len(pd.list_alerts(status="open"))
    assert alerts["alert_count"] > 1


def test_memory_uses_the_existing_facts_table():
    _fresh()
    from industrial import plant_tools
    registry.execute("equipment_details", equipment="P-204")
    assert database.get_fact("last_equipment") == "P-204"


# -------------------------------------------------------------- offline --

def test_industrial_modules_import_no_network_client():
    for module in ("industrial.plant_data", "industrial.plant_ops", "industrial.plant_docs",
                   "industrial.plant_tools", "core.office"):
        source = Path(module.replace(".", "/") + ".py")
        text = (Path(__file__).resolve().parent.parent / source).read_text(encoding="utf-8")
        for banned in ("fastapi", "flask", "uvicorn", "django", "boto3", "openai",
                       "http://", "https://"):
            assert banned not in text.lower(), f"{module} references {banned}"


def test_no_web_framework_is_loaded():
    forbidden = {"fastapi", "flask", "uvicorn", "django", "starlette", "tornado"}
    assert not (forbidden & set(sys.modules))
