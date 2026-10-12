"""
Document quality: the shared analysis layer and the three renderers.

The requirement was not "ZEUS can make a PPT" but "ZEUS turns industrial
information into professional deliverables". These tests check the properties
that distinguish the two: consistent numbers across formats, a real storyline,
real Excel formulas that recalculate, typed cells, and no bullet spam.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory import database  # noqa: E402

DB_PATH = Path(tempfile.gettempdir()) / "medha_test_docquality.db"

from core import office  # noqa: E402
from core.security_manager import security_manager  # noqa: E402
from core.tool_registry import registry  # noqa: E402
from industrial import analysis as an  # noqa: E402
from industrial import deck_builder, report_builder, workbook_builder  # noqa: E402
from industrial import plant_data as pd  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="medha_docquality_"))


def setup_module(module):
    database.DB_PATH = DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()
    database.init_db()
    pd.init_plant_schema(force=True)
    pd.seed_demo_data(force=True)


def setup_function(function):
    database.DB_PATH = DB_PATH
    database.set_active_user(None)
    security_manager.set_confirm_callback(lambda message: True)
    office.documents_dir = lambda subfolder="": OUT


def _analysis():
    return an.build_operations_analysis(narrative=False)


# ------------------------------------------------------- analysis layer --

def test_analysis_is_built_from_records_not_invented():
    analysis = _analysis()
    assert analysis.kpis and analysis.findings and analysis.tables
    equipment = pd.list_equipment(limit=999)
    register = analysis.table("Equipment register")
    assert len(register.rows) == len(equipment)
    alerts = analysis.table("Open alerts")
    assert len(alerts.rows) == len(pd.list_alerts(status="open"))


def test_analysis_flags_demo_data():
    analysis = _analysis()
    assert analysis.contains_demo_data is True
    assert "DEMO" in analysis.demo_banner()


def test_analysis_has_a_headline_that_names_the_problem():
    analysis = _analysis()
    assert analysis.headline
    assert any(char.isdigit() for char in analysis.headline), \
        "a headline without a number is an opinion"


def test_every_finding_carries_a_magnitude():
    analysis = _analysis()
    assert analysis.findings
    for finding in analysis.findings:
        assert finding.magnitude, f"finding without a magnitude: {finding.title}"


def test_narrative_is_optional_and_absent_without_ollama():
    """The whole deliverable must survive the model being down."""
    from core.ollama_client import ollama_client
    if ollama_client.is_available():
        return
    analysis = an.build_operations_analysis(narrative=True)
    assert analysis.narrative_included is False
    assert analysis.executive_summary, "a deterministic summary must still be written"


# ---------------------------------------------------------- consistency --

def test_one_analysis_gives_identical_numbers_in_all_three_files():
    """The point of the shared layer: the deck cannot disagree with the
    workbook."""
    analysis = _analysis()
    deck_path = OUT / "consistency.pptx"
    book_path = OUT / "consistency.xlsx"
    docx_path = OUT / "consistency.docx"
    deck_builder.build_deck(analysis, deck_path)
    workbook_builder.build_workbook(analysis, book_path)
    report_builder.build_report(analysis, docx_path)

    headline_value = analysis.kpis[0].value          # availability, e.g. "75.0%"

    from pptx import Presentation
    deck_text = " ".join(shape.text_frame.text for slide in Presentation(str(deck_path)).slides
                         for shape in slide.shapes if shape.has_text_frame)
    from openpyxl import load_workbook
    book = load_workbook(str(book_path))
    book_text = " ".join(str(c.value) for row in book["Summary"].iter_rows() for c in row
                         if c.value is not None)
    from docx import Document
    document = Document(str(docx_path))
    doc_text = " ".join(p.text for p in document.paragraphs)
    doc_text += " ".join(cell.text for table in document.tables
                         for row in table.rows for cell in row.cells)

    for blob, name in ((deck_text, "deck"), (book_text, "workbook"), (doc_text, "report")):
        assert headline_value in blob, f"{name} is missing the headline KPI {headline_value}"
        assert analysis.headline[:40] in blob, f"{name} is missing the recommendation"


# ---------------------------------------------------------------- deck --

def test_deck_has_a_storyline_not_a_pile_of_bullets():
    analysis = _analysis()
    path = OUT / "story.pptx"
    result = deck_builder.build_deck(analysis, path)
    assert result["verified"] is True

    from pptx import Presentation
    deck = Presentation(str(path))
    assert 7 <= len(deck.slides) <= 16, "a deck this size should be 7-16 slides"

    text = [" ".join(shape.text_frame.text for shape in slide.shapes
                     if shape.has_text_frame) for slide in deck.slides]
    joined = " ".join(text).lower()
    for expected in ("recommendation", "diagnosis", "recommended actions", "risks",
                     "roadmap", "decision"):
        assert expected in joined, f"the deck has no {expected} section"

    assert any(shape.has_chart for slide in deck.slides for shape in slide.shapes), \
        "no chart in the deck"
    assert any(shape.has_table for slide in deck.slides for shape in slide.shapes), \
        "no evidence table in the deck"


def test_deck_uses_readable_type_sizes():
    """Nothing below 8.5 pt, and body text at 9 pt or more."""
    analysis = _analysis()
    path = OUT / "typesize.pptx"
    deck_builder.build_deck(analysis, path)
    from pptx import Presentation
    sizes = []
    for slide in Presentation(str(path)).slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    if run.font.size is not None:
                        sizes.append(run.font.size.pt)
    assert sizes
    assert min(sizes) >= 8.5, f"text as small as {min(sizes)} pt found"


def test_deck_content_fits_on_the_slide():
    """The bug this guards: a fourth diagnosis card ran off the bottom."""
    analysis = _analysis()
    path = OUT / "fits.pptx"
    deck_builder.build_deck(analysis, path)
    from pptx import Presentation
    from pptx.util import Emu
    deck = Presentation(str(path))
    height = Emu(deck.slide_height).inches
    width = Emu(deck.slide_width).inches
    for index, slide in enumerate(deck.slides, 1):
        for shape in slide.shapes:
            if shape.top is None or shape.height is None:
                continue
            bottom = Emu(shape.top).inches + Emu(shape.height).inches
            right = Emu(shape.left).inches + Emu(shape.width).inches
            assert bottom <= height + 0.02, f"slide {index}: shape overflows the bottom"
            assert right <= width + 0.02, f"slide {index}: shape overflows the right edge"


def test_deck_states_when_the_data_is_demo():
    analysis = _analysis()
    path = OUT / "demo.pptx"
    deck_builder.build_deck(analysis, path)
    from pptx import Presentation
    text = " ".join(shape.text_frame.text for slide in Presentation(str(path)).slides
                    for shape in slide.shapes if shape.has_text_frame)
    assert "DEMO" in text


# ------------------------------------------------------------ workbook --

def test_workbook_is_multi_sheet_with_a_management_view():
    analysis = _analysis()
    path = OUT / "book.xlsx"
    result = workbook_builder.build_workbook(analysis, path)
    assert result["verified"] is True
    from openpyxl import load_workbook
    book = load_workbook(str(path))
    assert len(book.sheetnames) >= 4, "a flat single sheet is not a workbook"
    assert book.sheetnames[0] == "Summary", "the management view must come first"
    assert "Analysis" in book.sheetnames
    assert "Sources" in book.sheetnames


def test_workbook_uses_real_formulas_not_pasted_numbers():
    analysis = _analysis()
    path = OUT / "formulas.xlsx"
    workbook_builder.build_workbook(analysis, path)
    from openpyxl import load_workbook
    book = load_workbook(str(path))
    formulas = [c.value for sheet in book.worksheets for row in sheet.iter_rows()
                for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert formulas, "the workbook contains no formulas at all"
    joined = " ".join(formulas)
    assert "SUM(" in joined
    assert "COUNTIF(" in joined
    assert any("'" in f for f in formulas), "no cross-sheet formula"


def test_workbook_numbers_are_numbers():
    """A spreadsheet of strings looks right and sorts wrong."""
    analysis = _analysis()
    path = OUT / "typed.xlsx"
    workbook_builder.build_workbook(analysis, path)
    from openpyxl import load_workbook
    sheet = load_workbook(str(path))["Maintenance history"]
    header_row = next(r for r in range(1, 12)
                      if sheet.cell(row=r, column=1).value == "Equipment")
    hours_column = next(c for c in range(1, 12)
                        if sheet.cell(row=header_row, column=c).value == "Hours")
    values = [sheet.cell(row=r, column=hours_column).value
              for r in range(header_row + 1, header_row + 5)]
    assert any(isinstance(v, (int, float)) for v in values), "hours stored as text"


def test_workbook_has_filters_freeze_panes_and_conditional_formatting():
    analysis = _analysis()
    path = OUT / "usable.xlsx"
    workbook_builder.build_workbook(analysis, path)
    from openpyxl import load_workbook
    book = load_workbook(str(path))
    data_sheets = [book[name] for name in book.sheetnames
                   if name not in ("Summary", "Analysis", "Sources")]
    assert data_sheets
    assert any(s.auto_filter.ref for s in data_sheets), "no autofilter on any data sheet"
    assert any(s.freeze_panes for s in data_sheets), "no frozen header"
    assert any(len(list(s.conditional_formatting)) for s in data_sheets), \
        "no conditional formatting"
    assert any(len(s._charts) for s in book.worksheets), "no chart in the workbook"


# -------------------------------------------------------------- report --

def test_report_is_structured_with_headings_and_tables():
    analysis = _analysis()
    path = OUT / "report.docx"
    result = report_builder.build_report(analysis, path, include_approval=True)
    assert result["verified"] is True
    from docx import Document
    document = Document(str(path))
    headings = [p.text for p in document.paragraphs if p.style.name.startswith("Heading")]
    for expected in ("Executive summary", "Key numbers", "Recommended actions", "Risks",
                     "Sources and assumptions", "Approval"):
        assert expected in headings, f"the report has no '{expected}' section"
    assert len(document.tables) >= 5


def test_report_approval_block_is_never_pre_approved():
    analysis = _analysis()
    path = OUT / "approval.docx"
    report_builder.build_report(analysis, path, include_approval=True, approver="A Manager")
    from docx import Document
    document = Document(str(path))
    text = " ".join(p.text for p in document.paragraphs).lower()
    assert "not approved until" in text
    approval = document.tables[-1]
    approved_row = [r for r in approval.rows if r.cells[0].text == "Approved by"][0]
    assert approved_row.cells[1].text.strip() == "", "the approver box was pre-filled"
    assert approved_row.cells[2].text.strip() == "", "the signature box was pre-filled"


# ------------------------------------------------- spreadsheet analysis --

def _sample_workbook(path: Path):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Costs"
    sheet["A1"] = "Quarterly maintenance spend"
    sheet["A2"] = "Prepared for testing"
    sheet.append([])
    sheet.append(["Unit", "Quarter", "Spend", "Jobs"])
    for row in [["CDU", "Q1", 120, 8], ["CDU", "Q2", 140, 9], ["CDU", "Q3", 900, 11],
                ["VDU", "Q1", 60, 4], ["VDU", "Q2", 75, 5], ["VDU", "Q3", 80, 6]]:
        sheet.append(row)
    book.save(str(path))
    return path


def test_spreadsheet_analysis_finds_the_header_below_a_title_block():
    path = _sample_workbook(OUT / "sample.xlsx")
    analysis = an.build_workbook_analysis(str(path), narrative=False)
    block = analysis.tables[0]
    assert block.columns[:4] == ["Unit", "Quarter", "Spend", "Jobs"]
    assert len(block.rows) == 6


def test_spreadsheet_analysis_computes_totals_and_finds_the_outlier():
    path = _sample_workbook(OUT / "sample2.xlsx")
    analysis = an.build_workbook_analysis(str(path), narrative=False)
    labels = " ".join(k.label + k.value for k in analysis.kpis)
    assert "1,375" in labels, "the Spend total was not computed"
    assert any("dominates" in f.title for f in analysis.findings), \
        "the 900 outlier was not detected"


def test_spreadsheet_to_deck_analyses_rather_than_pasting():
    path = _sample_workbook(OUT / "sample3.xlsx")
    analysis = an.build_workbook_analysis(str(path), narrative=False)
    deck_path = OUT / "from_sheet.pptx"
    result = deck_builder.build_deck(analysis, deck_path)
    assert result["verified"] is True
    from pptx import Presentation
    text = " ".join(shape.text_frame.text for slide in Presentation(str(deck_path)).slides
                    for shape in slide.shapes if shape.has_text_frame).lower()
    assert "recommendation" in text and "decision" in text, \
        "the deck is a table dump, not an analysis"


def test_missing_file_is_reported_not_faked():
    analysis = an.build_workbook_analysis("/nonexistent/nothing.xlsx", narrative=False)
    assert "does not exist" in analysis.headline


# --------------------------------------------------------------- tools --

def test_executive_tools_are_registered_and_run():
    for name in ("executive_presentation", "executive_workbook", "executive_report",
                 "executive_briefing_pack", "analyze_spreadsheet",
                 "spreadsheet_to_presentation", "spreadsheet_to_report"):
        assert registry.get(name) is not None, f"{name} is not registered"

    result = registry.execute("executive_presentation", narrative=False)
    assert result.success and result.output.get("verified") is True
    result = registry.execute("executive_workbook", narrative=False)
    assert result.success and result.output.get("verified") is True


def test_briefing_pack_produces_three_verified_files():
    result = registry.execute("executive_briefing_pack", narrative=False)
    assert result.success
    output = result.output
    assert output["verified"] is True
    assert set(output["pack"]) == {"report", "workbook", "presentation"}
    for path in output["pack"].values():
        assert Path(path).exists() and Path(path).stat().st_size > 5000


def test_executive_commands_route_before_the_generic_generators():
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()
    expected = {
        "Create an executive presentation": "executive_presentation",
        "Create a management presentation for P-204": "executive_presentation",
        "Create an analysis workbook": "executive_workbook",
        "Create an executive report for P-204": "executive_report",
        "Create a briefing pack": "executive_briefing_pack",
        "read this excel and create a presentation": "spreadsheet_to_presentation",
        "read file C:/notes.txt": "read_file",
        "Write a report about refinery safety": "write_document",
        "Create a PowerPoint about refinery operations": "create_presentation",
    }
    for sentence, tool in expected.items():
        match = engine._match_direct_command(sentence)
        assert match is not None, f"no route for {sentence!r}"
        assert match[0] == tool, f"{sentence!r} routed to {match[0]}, expected {tool}"
