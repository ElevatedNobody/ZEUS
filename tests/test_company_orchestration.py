"""The orchestrator's company-intelligence path: company questions reach the
knowledge fabric, general questions and exact routes are left exactly where
they were, and requested files come from the same analysis."""

import pytest

from core.orchestration import agents, task_graph
from core.orchestration.classifier import classify


def _plan(text, direct=None):
    return task_graph.build_plan(text, classify(text), direct)


@pytest.mark.parametrize("question", [
    "Why did we make losses between 2019 and 2021?",
    "What was our revenue in 2019?",
    "How can we reduce downtime?",
    "Show me the company history",
    "What was the maintenance cost over the last five years?",
])
def test_company_questions_go_to_company_intelligence(question):
    plan = _plan(question)
    assert plan and plan[0].tool == "company_intelligence" and plan[0].agent == "analysis"
    assert plan[0].args["question"] == question


@pytest.mark.parametrize("question", [
    "What is EBITDA?",
    "Tell me a joke",
    "how is net profit calculated",
    "Summarise C:/reports/profit.xlsx",
])
def test_general_questions_are_not_captured(question):
    plan = _plan(question)
    assert not plan or plan[0].tool != "company_intelligence"


def test_an_exact_route_still_wins():
    plan = _plan("What was our revenue in 2019?", direct=("plant_status", {}))
    assert plan[0].tool == "plant_status"


def test_requested_files_are_separate_steps_from_the_same_analysis():
    plan = _plan("Why did we make losses between 2019 and 2021? Create an Excel file and a presentation")
    tools = [(s.tool, s.args.get("format"), s.depends_on) for s in plan]
    assert tools[0][0] == "company_intelligence"
    assert ("create_analysis_artifact", "xlsx", ["s1"]) in tools
    assert ("create_analysis_artifact", "pptx", ["s1"]) in tools


@pytest.mark.parametrize("request_text, fmt", [
    ("Now create a management presentation from this analysis", "pptx"),
    ("Create a detailed Word report from the same analysis", "docx"),
])
def test_a_follow_up_renders_the_saved_analysis(request_text, fmt, monkeypatch):
    monkeypatch.setattr(task_graph, "_has_saved_analysis", lambda: True)
    plan = _plan(request_text)
    assert [(s.tool, s.args.get("format")) for s in plan] == [("create_analysis_artifact", fmt)]


def test_a_follow_up_beats_a_route_that_would_write_a_new_document(monkeypatch):
    monkeypatch.setattr(task_graph, "_has_saved_analysis", lambda: True)
    plan = _plan("Create a detailed Word report from the same analysis", direct=("write_document", {"topic": "x"}))
    assert plan[0].tool == "create_analysis_artifact"


def test_without_a_saved_analysis_the_follow_up_is_not_captured(monkeypatch):
    monkeypatch.setattr(task_graph, "_has_saved_analysis", lambda: False)
    plan = _plan("Create a detailed Word report from the same analysis", direct=("write_document", {"topic": "x"}))
    assert plan[0].tool == "write_document"


def test_a_multi_step_reply_leads_with_the_answer():
    from core.orchestration.contracts import OrchestrationResult, PlanStep, Status
    from core.orchestration.orchestrator import Orchestrator
    from core.agni_present import present
    orchestrator = Orchestrator(formatter=lambda tool, output: present(tool, output) or str(output))
    answer = PlanStep(id="s1", tool="company_intelligence", description="Answer", status=Status.OK,
                      output={"ok": True, "answer": "Losses came from downtime [E1].", "verified": True,
                              "analysis_id": "A1", "narrative_source": "deterministic"})
    xlsx = PlanStep(id="s2", tool="create_analysis_artifact", description="XLSX", status=Status.OK,
                    output={"ok": True, "format": "xlsx", "path": "C:/x.xlsx", "verified": True})
    text = orchestrator._multi_step_text([answer, xlsx], OrchestrationResult())
    assert text.startswith("Losses came from downtime [E1].") and "C:/x.xlsx" in text


def test_the_acceptance_scenario_end_to_end(kf):
    """Losses analysis -> Excel; then a deck and a Word report FROM THE SAME
    analysis - three files, one analysis id, every one verified."""
    from knowledge_fabric import service
    from tests.conftest import kf_ask
    first = kf_ask("Why did we make losses between 2019 and 2021 and what can we do? Create an Excel file.",
                   make_artifacts=True)
    deck = kf_ask("Now create a management presentation from this analysis", make_artifacts=True)
    report = kf_ask("Create a detailed Word report from the same analysis", make_artifacts=True)
    assert first.analysis_id == deck.analysis_id == report.analysis_id
    formats = {a["format"]: a for a in report.artifacts}
    assert {"xlsx", "pptx", "docx"} <= set(formats)
    assert all(formats[f]["verified"] for f in ("xlsx", "pptx", "docx"))


def test_the_new_agents_exist_and_nothing_was_removed():
    ids = set(agents.SPECIALISTS)
    for new in ("company_retrieval", "temporal", "entity", "graph", "analysis", "financial_analysis",
                "company_document", "recommendation", "decision_support", "artifact", "validation", "citation"):
        assert new in ids
    for existing in ("operations", "maintenance", "document", "knowledge", "report", "conversation", "security"):
        assert existing in ids
    assert agents.agent_for_tool("company_intelligence") == "analysis"
    assert agents.agent_for_tool("create_analysis_artifact") == "artifact"


def test_new_tools_need_the_right_role():
    from security.policy import profile_for
    assert profile_for("company_intelligence").min_role == "viewer"
    assert profile_for("create_analysis_artifact").min_role == "operator"
    assert profile_for("ingest_company_directory").min_role == "engineer"
    assert profile_for("update_entity").min_role == "engineer"
    for admin_only in ("remove_company_document", "reindex_company_knowledge", "rebuild_knowledge_graph",
                       "set_user_departments"):
        assert profile_for(admin_only).min_role == "admin"
        assert profile_for(admin_only).access == "write"


def test_the_chat_shows_the_answer_and_its_verification_state():
    """VERIFIED only on cited company records; checked non-company evidence is VALIDATED."""
    from core.agni_present import present
    base = {"ok": True, "answer": "Net profit fell [C1].", "verified": True, "analysis_id": "A1",
            "narrative_source": "deterministic"}
    text = present("company_intelligence", {**base, "verification_state": "VERIFIED"})
    assert "Net profit fell [C1]." in text and "VERIFIED against company records" in text
    text = present("company_intelligence", {**base, "verification_state": "VALIDATED"})
    assert "VALIDATED against the evidence - not company records" in text and "VERIFIED" not in text
    assert "VALIDATED" in present("company_intelligence", base)          # no state given: never claims VERIFIED
    text = present("company_intelligence", {**base, "verified": False, "verification_state": "INSUFFICIENT_DATA"})
    assert "INSUFFICIENT DATA" in text


def test_stage_events_name_their_agents(kf):
    from tests.conftest import kf_ask
    obj = kf_ask("Why did C-501 fail?")
    searching = next(s for s in obj.stages if s["stage"] == "SEARCHING")
    assert "company_retrieval" in searching["agents"]
    assert all(a in agents.SPECIALISTS for s in obj.stages for a in s["agents"])
