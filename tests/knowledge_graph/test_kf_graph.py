"""The knowledge graph: typed relations with their evidence, access-filtered."""

from knowledge_fabric import graph, store
from knowledge_fabric.entities import Extractor


def test_a_stated_failure_cause_becomes_a_relation():
    relations = graph.extract_relations("Compressor C-501 failed due to bearing wear.", Extractor())
    assert [(r["src"].entity_id, r["relation"]) for r in relations] == [("EQUIPMENT:C-501", "FAILED_DUE_TO")]
    assert "bearing" in relations[0]["dst"].entity_id


def test_one_phrase_is_one_relation():
    relations = graph.extract_relations("Pump P-502 failed due to cavitation.", Extractor())
    assert len(relations) == 1


def test_the_fixture_graph_links_failures_to_causes(kf):
    with store.connection() as conn:
        edges = graph.neighbours(conn, ["EQUIPMENT:C-501"], min_confidence=0.5)
    causes = {e["dst_name"] for e in edges if e["relation"] == "FAILED_DUE_TO"}
    assert any("bearing" in c for c in causes)
    assert all(e.get("evidence_node") for e in edges)          # every edge points at its evidence


def test_edges_from_documents_a_user_cannot_see_are_not_returned(kf):
    from tests.conftest import kf_ctx
    from knowledge_fabric.access import allowed_document_ids
    with store.connection() as conn:
        viewer_docs = allowed_document_ids(conn, kf_ctx("viewer"))
        confidential = {r["document_id"] for r in conn.execute(
            "SELECT document_id FROM kf_documents WHERE confidentiality = 'confidential'")}
        edges = graph.neighbours(conn, ["UNIT:5", "UNIT:7"], allowed_documents=viewer_docs)
    assert confidential and not confidential & viewer_docs
    assert not {e.get("document_id") for e in edges} & confidential


def test_root_cause_answers_cite_the_graph(kf):
    from tests.conftest import kf_ask
    obj = kf_ask("Why did C-501 fail?")
    assert "bearing wear" in obj.answer
    assert any(e["relation"] == "FAILED_DUE_TO" for e in obj.graph_edges)
    assert "[G1]" in obj.answer and obj.validation["ok"]


def test_rebuilding_the_graph_reproduces_it(tmp_path):
    from pathlib import Path
    from knowledge_fabric import ORIGIN_FIXTURE, ingest
    corpus = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge_fabric" / "corpus"
    with store.using_database(tmp_path / "kf.db"):
        ingest.ingest_document(corpus / "TFR_Maintenance_Report_2020.md", origin=ORIGIN_FIXTURE, embed=False)
        with store.connection() as conn:
            before = conn.execute("SELECT COUNT(*) FROM kf_relations").fetchone()[0]
        result = ingest.rebuild_graph()
        with store.connection() as conn:
            after = conn.execute("SELECT COUNT(*) FROM kf_relations").fetchone()[0]
    assert result["ok"] and before == after > 0
