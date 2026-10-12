"""The analytics engine calculates, the model interprets, the answer is held
to the evidence; decision support is operational and labelled by certainty."""

import pytest

from knowledge_fabric import analysis, analytics, planner
from tests.conftest import kf_ask


def _facts(metric, rows, unit="Rs Cr", authority=4):
    return [{"metric_norm": metric, "metric": metric, "year": y, "value": v, "unit": unit, "fact_id": i,
             "document_id": f"doc{doc}", "node_id": f"n{i}", "title": f"Report {doc}", "authority": authority,
             "doc_year": doc} for i, (y, v, doc) in enumerate(rows, 1)]


# ----------------------------------------------------------------- analytics --

def test_the_later_document_wins_a_restatement_and_the_conflict_is_kept():
    series = analytics.build_series(_facts("net profit", [(2021, -85, 2021), (2021, -80, 2023)]), "net profit")
    assert series.points[0].value == -80
    assert series.conflicts and series.conflicts[0]["others"][0]["value"] == -85


def test_more_authoritative_sources_win():
    facts = _facts("revenue", [(2020, 100, 2020)], authority=5) + _facts("revenue", [(2020, 110, 2020)], authority=2)
    for i, f in enumerate(facts):
        f["fact_id"] = i
    assert analytics.build_series(facts, "revenue").points[0].value == 110


def test_values_in_different_units_are_not_mixed():
    facts = _facts("revenue", [(2019, 100, 2019), (2020, 120, 2020)]) + \
        [{**_facts("revenue", [(2021, 9e9, 2021)])[0], "unit": "INR", "fact_id": 99}]
    series = analytics.build_series(facts, "revenue")
    assert series.unit == "Rs Cr" and [p.value for p in series.points] == [100, 120]
    assert any("different units" in n for n in series.notes)
    assert any("2021" in n and "left out" in n for n in series.notes)


def test_cagr_needs_positive_ends():
    series = analytics.build_series(_facts("net profit", [(2016, 295, 2016), (2021, -80, 2021)]), "net profit")
    calc = analytics.cagr(series)
    assert not calc.ok and "positive" in calc.notes[0]


def test_cagr_value():
    series = analytics.build_series(_facts("revenue", [(2015, 4200, 2015), (2019, 4300, 2019)]), "revenue")
    assert analytics.cagr(series).value == round(((4300 / 4200) ** 0.25 - 1) * 100, 3)


def test_a_weak_trend_is_not_extrapolated():
    series = analytics.build_series(_facts("x", [(2015, 1, 1), (2016, 9, 1), (2017, 2, 1), (2018, 8, 1)]), "x")
    calc = analytics.extrapolate(series)
    assert not calc.ok and "not extrapolated" in calc.notes[0]


def test_correlation_needs_four_years_and_says_it_is_not_causation():
    a = analytics.build_series(_facts("a", [(2015, 1, 1), (2016, 2, 1), (2017, 3, 1)]), "a")
    b = analytics.build_series(_facts("b", [(2015, 2, 1), (2016, 4, 1), (2017, 6, 1)]), "b")
    assert not analytics.correlation(a, b).ok
    a = analytics.build_series(_facts("a", [(2015, 1, 1), (2016, 2, 1), (2017, 3, 1), (2018, 5, 1)]), "a")
    b = analytics.build_series(_facts("b", [(2015, 2, 1), (2016, 4, 1), (2017, 6, 1), (2018, 9, 1)]), "b")
    calc = analytics.correlation(a, b)
    assert calc.ok and any("not causation" in n for n in calc.notes)


def test_every_calculation_lists_its_inputs():
    series = analytics.build_series(_facts("revenue", [(2019, 10, 1), (2020, 12, 1), (2021, 15, 1)]), "revenue")
    for calc in analytics.describe(series):
        assert calc.inputs and all(i["document_id"] for i in calc.inputs)


# ----------------------------------------------------------------- planning --

@pytest.mark.parametrize("question, intent", [
    ("Why did we make losses in 2020?", "ROOT_CAUSE"),
    ("What can we do to reduce maintenance cost?", "RECOMMENDATION"),
    ("Compare revenue in 2016 and 2023", "COMPARISON"),
    ("Forecast net profit for next year", "FORECAST"),
    ("What if maintenance cost falls 10%?", "SCENARIO"),
    ("Should we defer the Unit 7 expansion?", "DECISION_SUPPORT"),
    ("How do we start up the hydrogen unit?", "PROCEDURE"),
    ("Create an Excel workbook of the losses", "ARTIFACT_GENERATION"),
])
def test_the_planner_recognises_intents(question, intent):
    assert intent in planner.plan(question, latest_year=2023).intents


def test_an_excel_report_is_an_excel_file():
    assert planner.plan("Create an Excel report of revenue").formats == ["xlsx"]


# --------------------------------------------------------------- validation --

def test_an_invented_number_fails_validation(kf):
    obj = kf_ask("What was the net profit trend over the last five years?")
    assert obj.validation["ok"]
    bad = analysis.validate_answer(obj, "Net profit will reach 777 Rs Cr next year [C1].")
    assert not bad["ok"] and "777" in bad["unsupported_numbers"]


def test_a_citation_that_does_not_exist_fails_validation(kf):
    obj = kf_ask("What was the net profit trend over the last five years?")
    bad = analysis.validate_answer(obj, f"See [E{len(obj.evidence) + 5}].")
    assert not bad["ok"] and bad["invalid_citations"]


@pytest.mark.parametrize("citation", ["[F2, F999]", "[E1; E99]", "see C999 for this"])
def test_every_citation_form_is_checked(kf, citation):
    obj = kf_ask("What was the net profit trend over the last five years?")
    result = analysis.validate_answer(obj, f"Net profit fell {citation}.")
    assert not result["ok"] and result["invalid_citations"]


def _model_answers(monkeypatch, chat):
    """Patch the CLASSES. Patching the shared instances leaves a bound method
    behind on undo, which then shadows every later patch of the class."""
    from core.model_router import ModelRouter
    from core.ollama_client import OllamaClient
    monkeypatch.setattr(ModelRouter, "route", lambda self, task_type="default": "test-model")
    monkeypatch.setattr(OllamaClient, "chat", chat)


def test_the_model_summary_is_discarded_when_it_invents(kf, monkeypatch):
    _model_answers(monkeypatch, lambda *a, **k: "Profit collapsed by 999 Rs Cr [C1].")
    obj = kf_ask("What was the net profit trend over the last five years?", use_model=True)
    assert obj.narrative_source == "deterministic" and "999" not in obj.answer
    assert "discarded" in obj.retrieval["narrative"]


STRUCTURED = """Answer: Net profit was lowest in 2020 at -120 Rs Cr [C5] and recovered by 2023 [E1].

Facts:
- Net profit was -120 Rs Cr in 2020 [C5].
- The 2023 report records the recovery [E1].

Inferences:
- Inference: the recovery followed the end of the losses the records describe [E1][C5].

Assumptions:
- The records give no single stated cause for the recovery."""


def test_the_model_reasoning_is_kept_when_it_separates_facts_from_inference(kf, monkeypatch):
    """A complex question: the reasoning model (DeepSeek on this machine) must keep what
    the records state apart from what it infers and what it assumes - each part checked."""
    prompts = []
    _model_answers(monkeypatch, lambda self, model, messages, **k: prompts.append(messages[-1]["content"]) or STRUCTURED)
    obj = kf_ask("What was the net profit trend over the last five years?", use_model=True)
    assert obj.narrative_source == "model (validated)" and obj.validation["ok"]
    assert len(obj.reasoning["facts"]) == 2 and obj.reasoning["inferences"][0].startswith("Inference:")
    assert obj.reasoning["assumptions"] and "reasoning validated" in obj.retrieval["narrative"]
    assert "Facts:" in prompts[0] and "Inferences:" in prompts[0] and "Assumptions:" in prompts[0]


def test_unseparated_reasoning_on_a_complex_question_falls_back_to_the_verified_answer(kf, monkeypatch):
    _model_answers(monkeypatch,
                   lambda *a, **k: "Net profit was lowest in 2020 at -120 Rs Cr [C5] and recovered by 2023 [E1].")
    obj = kf_ask("What was the net profit trend over the last five years?", use_model=True)
    assert obj.narrative_source == "deterministic" and obj.verified
    assert "did not separate its facts" in obj.retrieval["narrative"]


def test_an_uncited_fact_or_an_ungrounded_inference_discards_the_reasoning(kf, monkeypatch):
    bad = STRUCTURED.replace("- Net profit was -120 Rs Cr in 2020 [C5].", "- Net profit was -120 Rs Cr in 2020.")
    _model_answers(monkeypatch, lambda *a, **k: bad)
    obj = kf_ask("What was the net profit trend over the last five years?", use_model=True)
    assert obj.narrative_source == "deterministic" and "a fact without a citation" in obj.retrieval["narrative"]


def test_the_model_being_unavailable_still_gives_a_verified_answer(kf, monkeypatch):
    def down(*args, **kwargs):
        raise ConnectionError("ollama not running")
    _model_answers(monkeypatch, down)
    obj = kf_ask("What was the net profit trend over the last five years?", use_model=True)
    assert obj.verified and obj.narrative_source == "deterministic"


# ---------------------------------------------------------- decision support --

REC_FIELDS = ("action", "owner", "timeline", "dependencies", "cost_category", "expected_impact", "risk", "kpi",
              "data_required", "success_condition", "monitoring_frequency")


def test_losses_analysis_is_complete_and_implementable(kf):
    obj = kf_ask("Why did we make losses between 2019 and 2021 and what can we do?")
    assert obj.verified
    kinds = {d["kind"] for d in obj.drivers}
    assert "evidenced" in kinds and "correlated" in kinds
    assert all(d["evidence_ids"] for d in obj.drivers if d["kind"] == "evidenced")
    assert obj.recommendations and all(all(r.get(f) for f in REC_FIELDS) for r in obj.recommendations)
    assert obj.options and obj.implementation_plan and obj.kpis and obj.risks
    assert any(c["type"] == "numeric" and c["period"] == "2021" for c in obj.contradictions)


def test_expected_impact_is_never_an_invented_number(kf):
    import re
    obj = kf_ask("Why did we make losses between 2019 and 2021 and what can we do?")
    for rec in obj.recommendations:
        assert not re.search(r"\d", rec["expected_impact"])


def test_scenario_arithmetic_is_labelled_as_an_assumption(kf):
    obj = kf_ask("What if maintenance cost falls 10%?")
    impact = next(c for c in obj.calculations if c["name"].startswith("net profit if")) \
        if any(c["name"].startswith("net profit if") for c in obj.calculations) else None
    scenario = next(c for c in obj.calculations if c["name"].startswith("scenarios for maintenance cost"))
    assert scenario["value"][0]["value"] == 180.0            # 200 in 2023, minus 10%
    assert any("not predictions" in n for n in scenario["notes"])
    if impact:
        assert "not a forecast" in impact["notes"][0]
