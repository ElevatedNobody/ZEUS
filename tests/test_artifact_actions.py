"""Artifact workspace actions: the list carries format, size, hash and the analysis a
file came from; 'remove from list' keeps the file; an unknown id is refused."""

from pathlib import Path

from core import artifacts


def test_a_registered_file_lists_its_format_and_analysis(tmp_path):
    f = tmp_path / "Revenue_Comparison.xlsx"
    f.write_bytes(b"PK\x03\x04 not a real workbook")
    artifact_id = artifacts.register_artifact(str(f), kind="xlsx", creator_agent="ArtifactAgent",
                                              request="compare revenue", sources=["analysis:A20260927-1", "Annual PL"])
    assert artifact_id
    row = next(r for r in artifacts.list_artifacts(200) if r["id"] == artifact_id)
    assert row["format"] == "XLSX" and row["analysis_id"] == "A20260927-1"
    assert row["size_bytes"] == f.stat().st_size and len(row["sha256"]) == 64 and row["exists"]


def test_remove_from_list_keeps_the_file(tmp_path):
    f = tmp_path / "Brief.docx"
    f.write_bytes(b"PK\x03\x04 brief")
    artifact_id = artifacts.register_artifact(str(f), kind="docx")
    assert artifacts.forget_record(artifact_id) is True
    assert f.exists() and artifacts.get_artifact(artifact_id) is None
    assert artifacts.forget_record(artifact_id) is False


def test_the_tools_refuse_what_is_not_in_the_list():
    from core.agni_tools import remove_artifact_record, reveal_artifact
    assert remove_artifact_record("999999")["ok"] is False
    assert reveal_artifact("not-a-number")["ok"] is False
    assert reveal_artifact("999999")["ok"] is False
