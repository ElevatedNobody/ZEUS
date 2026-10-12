"""
Verification states: DRAFT -> RUNNING -> EVIDENCE_FOUND -> VALIDATED | VERIFIED |
INSUFFICIENT_DATA | FAILED. An answer is VERIFIED only when it passes validation
AND the evidence it cites is the company's own records - never on sample, demo,
general-knowledge or test material, and never without evidence.
"""

from knowledge_fabric.analysis import AnalysisObject


def _obj(**kw):
    base = dict(analysis_id="A1", question="q", username="u", created_at=0.0, plan={})
    base.update(kw)
    return AnalysisObject(**base)


def _evidence(origin, eid="E1"):
    return {"evidence_id": eid, "origin": origin, "kind": "paragraph", "text": "x"}


def test_the_life_of_an_answer():
    assert _obj().verification_state == "DRAFT"
    assert _obj(stages=[{"stage": "QUERY"}]).verification_state == "RUNNING"
    assert _obj(stages=[{"stage": "QUERY"}], evidence=[_evidence("company")]).verification_state == "EVIDENCE_FOUND"


def test_verified_needs_cited_company_records():
    ok = {"ok": True, "cited": ["E1"]}
    assert _obj(evidence=[_evidence("company")], validation=ok).verification_state == "VERIFIED"
    for origin in ("sample", "demo", "general_knowledge", "test_fixture"):
        assert _obj(evidence=[_evidence(origin)], validation=ok).verification_state == "VALIDATED", origin


def test_company_evidence_that_is_not_cited_does_not_verify():
    obj = _obj(evidence=[_evidence("sample", "E1"), _evidence("company", "E2")], validation={"ok": True, "cited": ["E1"]})
    assert obj.verification_state == "VALIDATED"


def test_a_calculation_is_as_good_as_the_facts_behind_it():
    facts = [{"id": "F1", "fact_id": 7, "origin": "company", "value": 1.0}]
    calcs = [{"id": "C1", "name": "total revenue", "inputs": [{"fact_id": 7, "value": 1.0}]}]
    obj = _obj(facts=facts, calculations=calcs, validation={"ok": True, "cited": ["C1"]})
    assert obj.cited_origins() == ["company"] and obj.verification_state == "VERIFIED"


def test_no_evidence_is_insufficient_data_and_a_failed_check_is_failed():
    assert _obj(validation={"ok": True, "cited": []}).verification_state == "INSUFFICIENT_DATA"
    assert _obj(evidence=[_evidence("company")], validation={"ok": False, "cited": ["E1"]}).verification_state == "FAILED"


def test_the_fixture_answers_are_validated_not_verified(kf):
    from tests.conftest import kf_ask
    obj = kf_ask("What was revenue in 2019?")
    assert obj.verified and obj.verification_state == "VALIDATED"
    assert "test_fixture" in obj.cited_origins()
