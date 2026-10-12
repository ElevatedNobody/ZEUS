"""Entity resolution: one id per real thing, evidence-backed aliases, no merges
on weak similarity, human corrections that survive a rebuild."""

import sqlite3

import pytest

from knowledge_fabric import entities, store
from knowledge_fabric.entities import Extractor


def _ids(text, location=None):
    return [m.entity_id for m in Extractor().mentions(text, location)]


@pytest.mark.parametrize("text", ["Unit 5", "unit-5", "U5", "Unit #5", "U-5"])
def test_unit_spellings_share_one_id(text):
    assert "UNIT:5" in _ids(text)


def test_lowercase_u_is_not_a_unit():
    assert "UNIT:5" not in _ids("we need u5 tickets")


def test_units_at_a_stated_site_are_kept_apart():
    assert _ids("Unit 5", "Panipat Refinery") == ["UNIT:5@panipat-refinery"]


def test_a_document_saying_two_names_are_one_thing_is_an_alias():
    x = Extractor()
    assert [(m.entity_id, other) for m, other in x.equivalences("Hydrogen Generation Unit (U5) tripped.")] == \
        [("UNIT:5", "Hydrogen Generation Unit")]
    assert [(m.entity_id, other) for m, other in
            x.equivalences("Unit 5, also called the Hydrogen Generation Unit or U5, had a trip.")] == \
        [("UNIT:5", "Hydrogen Generation Unit")]


def test_a_unit_of_measure_in_brackets_is_not_a_name():
    assert Extractor().equivalences("Maintenance cost (Rs Cr) rose in 2019.") == []


def test_different_descriptors_for_one_unit_need_a_human(tmp_path):
    with store.using_database(tmp_path / "kf.db"):
        with store.connection() as conn:
            x = Extractor()
            entities.upsert_mentions(conn, "n1", x.mentions("The Hydrogen Unit 5 tripped."))
            entities.upsert_mentions(conn, "n2", x.mentions("The Crude Unit 5 tripped."))
            row = conn.execute("SELECT status FROM kf_entities WHERE entity_id = 'UNIT:5'").fetchone()
            assert row["status"] == "ambiguous"
            assert [e["entity_id"] for e in entities.ambiguous_entities(conn)] == ["UNIT:5"]


def test_similar_spelling_is_a_suggestion_never_a_merge(tmp_path):
    with store.using_database(tmp_path / "kf.db"):
        with store.connection() as conn:
            entities.upsert_mentions(conn, "n1", Extractor().mentions("Compressor C-501 failed."))
            assert entities.resolve(conn, "C-510") == [] or all(
                r["entity_id"] != "EQUIPMENT:C-501" for r in entities.resolve(conn, "C-510"))
            suggestions = entities.suggestions(conn, "C-510")
            assert any(s["entity_id"] == "EQUIPMENT:C-501" for s in suggestions)


def test_the_fixture_resolves_u5_to_unit_5_everywhere(kf):
    from tests.conftest import kf_ask
    obj = kf_ask("What was the maintenance cost of U5 in 2019?")
    assert any(e.startswith("UNIT:5") for e in obj.plan["entities"])
    assert "120" in obj.answer and "265" not in obj.answer.split("### Evidence")[0]


def test_the_hydrogen_generation_unit_is_unit_5_by_evidence(kf):
    with store.connection() as conn:
        rows = conn.execute("SELECT entity_id, source FROM kf_aliases WHERE alias_norm = 'hydrogen generation unit'"
                            ).fetchall()
    assert any(r["entity_id"].startswith("UNIT:5") and r["source"] == "evidence" for r in rows)


def test_a_human_correction_survives_a_graph_rebuild(tmp_path):
    from knowledge_fabric import ORIGIN_FIXTURE, ingest
    from pathlib import Path
    corpus = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge_fabric" / "corpus"
    with store.using_database(tmp_path / "kf.db"):
        ingest.ingest_document(corpus / "TFR_Maintenance_Report_2020.md", origin=ORIGIN_FIXTURE, embed=False)
        result = entities.correct("alias", {"entity_id": "EQUIPMENT:C-501", "alias": "Big Blue Compressor"}, "tester")
        assert result.get("ok"), result
        ingest.rebuild_graph()
        with store.connection() as conn:
            assert [r["entity_id"] for r in entities.resolve(conn, "Big Blue Compressor")] == ["EQUIPMENT:C-501"]
