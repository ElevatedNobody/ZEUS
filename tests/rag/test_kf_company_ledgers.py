"""
Company ledgers: a daily P&L with site codes, annual statements, a monthly
roll-up, a sites register and a milestone list - the shape of a real company's
records (the ZEUS Unicorn corpus has exactly these files).

Everything here is synthetic and built per run ("Test Ledger Co" does not
exist). What is checked:

  * a CSV longer than 5,000 rows is read whole (it used to be cut silently);
  * daily rows become monthly company facts AND yearly per-site facts, and a
    site row never leaks into the company's series;
  * site codes are named from the register ('AA' -> 'Alpha City');
  * two periods are compared whole; no measure named -> the headline figures;
  * a milestone that contradicts the recorded figures is reported, not cited as fact;
  * stated monthly figures vs sums of daily rows are a reconciliation, summarised;
  * a register never answers for a measure it does not hold.
"""

import csv
import datetime as dt
import statistics
from pathlib import Path

import pytest

from knowledge_fabric import analytics, contradictions, registers, store, temporal

SITES = ("AA", "BB", "CC")
SITE_NAMES = {"AA": "Alpha City", "BB": "Beta Town", "CC": "Gamma Port"}
YEARS = list(range(2010, 2026))
REVENUE = {y: round(100 * 1.1 ** (y - 2010), 2) for y in YEARS}                 # INR Cr
PAT = {y: round(REVENUE[y] * 0.1, 2) for y in YEARS}
PAT[2020] = round(PAT[2019] * 0.8, 2)                                           # the one fall
START, END = dt.date(2010, 1, 1), dt.date(2025, 12, 31)


def _daily_rows():
    rows, day, i = [], START, 0
    while day <= END:
        rows.append([day.isoformat(), day.year, day.month, SITES[i % 3], 900000 + (i % 7) * 10000, 12.5])
        day += dt.timedelta(days=1)
        i += 1
    return rows


DAILY = _daily_rows()


def _workbook(path: Path, sheets: dict) -> Path:
    from openpyxl import Workbook
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        ws = book.create_sheet(name)
        for row in rows:
            ws.append(row)
    book.save(path)
    return path


@pytest.fixture(scope="module")
def ledger_db(tmp_path_factory):
    from knowledge_fabric import ingest
    folder = tmp_path_factory.mktemp("ledgers")
    db = folder / "kf_ledgers.db"
    daily = folder / "Daily_PL_2010_2025.csv"
    with open(daily, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Date", "Year", "Month", "Site_Code", "Revenue_INR", "Margin_Pct"])
        writer.writerows(DAILY)
    stated_2015 = {}
    for row in DAILY:
        if row[1] == 2015:
            stated_2015[row[2]] = stated_2015.get(row[2], 0) + row[4]
    files = [
        daily,
        _workbook(folder / "Annual_Financials.xlsx",
                  {"Annual_PL": [["FY_Year", "Revenue_Cr", "PAT_Cr"]] + [[y, REVENUE[y], PAT[y]] for y in YEARS]}),
        # the monthly report states 5% more than the ledger adds up to
        _workbook(folder / "Monthly_PL.xlsx",
                  {"Monthly_Rollup": [["Year", "Month", "Revenue_INR"]]
                   + [[2015, m, round(stated_2015[m] * 1.05, 2)] for m in range(1, 13)]}),
        _workbook(folder / "Company_Sites_Milestones.xlsx", {
            "Site_Openings": [["Site_Code", "City", "Opened_On", "Status"]]
                             + [[s, SITE_NAMES[s], f"{2008 + i}-04-01", "Active"] for i, s in enumerate(SITES)],
            "Milestones": [["Date", "Year", "Milestone", "Category", "Impact"],
                           ["2012-06-30", 2012, "Second plant commissioned", "Site", "Capacity"],
                           ["2014-03-31", 2014, "Crosses INR 500 Cr annual revenue", "Financial", "Scale marker"],
                           ["2020-04-01", 2020, "COVID continuity protocol", "Risk", "Volume shock"],
                           ["2023-11-15", 2023, "Digital control room live", "Ops", "Visibility"]]}),
    ]
    store.reset_cache()
    with store.using_database(db):
        for f in files:
            result = ingest.ingest_document(f, origin="test_fixture", embed=False)
            assert result.status == "ingested", f"{f.name}: {result.detail}"
    return db


@pytest.fixture
def led(ledger_db):
    with store.using_database(ledger_db):
        yield ledger_db


def ctx(role="admin"):
    from tests.conftest import kf_ctx
    return kf_ctx(role)


def ask(question):
    from tests.conftest import kf_ask
    return kf_ask(question, role="admin")


def _facts(conn, filename, where="", params=()):
    return store.rows(conn.execute(
        "SELECT f.* FROM kf_facts f JOIN kf_documents d ON d.document_id = f.document_id "
        f"WHERE d.filename = ? {where}", (filename,) + tuple(params)))


# ------------------------------------------------------------------ ingest ---

def test_a_csv_longer_than_5000_rows_is_read_whole(led):
    assert len(DAILY) > 5000
    with store.connection() as conn:
        last = _facts(conn, "Daily_PL_2010_2025.csv", "AND f.period = '2025-12' AND f.entity_label IS NULL")
    assert last, "December 2025 is row ~5,840 - it must not be cut off"


def test_daily_rows_are_summed_per_month_for_the_company(led):
    expected = sum(r[4] for r in DAILY if r[1] == 2013 and r[2] == 7)
    with store.connection() as conn:
        july = _facts(conn, "Daily_PL_2010_2025.csv",
                      "AND f.metric_norm = 'revenue' AND f.period = '2013-07' AND f.entity_label IS NULL")
        margin = _facts(conn, "Daily_PL_2010_2025.csv", "AND f.metric LIKE '%Margin%'")
    assert len(july) == 1 and july[0]["value"] == pytest.approx(expected)
    assert july[0]["source_text"].startswith("sum of 31 daily rows") and "(sum)" in july[0]["cell_ref"]
    assert margin == []                                  # a percentage is never summed


def test_daily_rows_are_summed_per_year_and_site_in_crore(led):
    expected = sum(r[4] for r in DAILY if r[1] == 2018 and r[3] == "BB") / 1e7
    with store.connection() as conn:
        bb = _facts(conn, "Daily_PL_2010_2025.csv",
                    "AND f.metric_norm = 'revenue' AND f.year = 2018 AND f.entity_label = 'BB' AND f.month IS NULL")
    assert len(bb) == 1
    assert bb[0]["value"] == pytest.approx(expected, abs=1e-3) and bb[0]["unit"] == "INR Cr"
    assert "converted from INR" in bb[0]["source_text"]


def test_a_multi_year_workbook_is_not_dated_by_its_most_written_year(tmp_path):
    """A roll-up running 2001-2012 month by month, whose name carries no year, spans
    its table periods - it is not 'a 2001 document' because 2001 is written most."""
    from knowledge_fabric import ingest
    rows = [["Year", "Month", "Revenue_INR"]] + [[y, m, 1000000 + y * 10 + m] for y in range(2001, 2013) for m in range(1, 13)]
    path = _workbook(tmp_path / "Monthly_Rollup_Book.xlsx", {"Monthly_Rollup": rows})
    with store.using_database(tmp_path / "kf.db"):
        assert ingest.ingest_document(path, origin="test_fixture", embed=False).status == "ingested"
        with store.connection() as conn:
            doc = dict(conn.execute("SELECT year, time_start, time_end FROM kf_documents").fetchone())
            node_years = {r[0] for r in conn.execute("SELECT DISTINCT year FROM kf_nodes WHERE kind = 'row_range'")}
    assert doc == {"year": None, "time_start": 2001, "time_end": 2012}
    assert node_years - {None} and node_years != {2001}          # each block of rows keeps its own year


def test_a_byte_identical_copy_is_stored_once_even_when_forced(tmp_path):
    from knowledge_fabric import ingest
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for folder in ("a", "b"):
        (tmp_path / folder / f"Board_Pack_2019_{folder}.md").write_text("# Board pack 2019" + chr(10) + "Revenue rose." + chr(10), encoding="utf-8")
    with store.using_database(tmp_path / "kf.db"):
        first = ingest.ingest_directory(tmp_path, origin="test_fixture", embed=False)
        again = ingest.ingest_directory(tmp_path, origin="test_fixture", embed=False, force=True)
        with store.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM kf_documents").fetchone()[0]
    assert (first["ingested"], first["skipped"]) == (1, 1)
    assert (again["updated"], again["skipped"], again["ingested"]) == (1, 1, 0) and count == 1


def test_year_spans_do_not_date_a_passage_but_a_year_heading_does(tmp_path):
    from knowledge_fabric import ingest
    path = tmp_path / "SOP_TEST_UNIT.md"
    text = ["# SOP - TEST UNIT", "**Years:** 1990-2026 | **Department:** Operations", "", "General rules of the unit.", "",
            "## FY1995", "Start-up checklist revised after the trip.", "", "## FY2016", "Digital logbook introduced.", ""]
    path.write_text(chr(10).join(text), encoding="utf-8")
    with store.using_database(tmp_path / "kf.db"):
        assert ingest.ingest_document(path, origin="test_fixture", embed=False).status == "ingested"
        with store.connection() as conn:
            years = {r["text"][:20]: r["year"] for r in conn.execute("SELECT text, year FROM kf_nodes WHERE kind = 'paragraph'")}
    assert years["Start-up checklist r"] == 1995 and years["Digital logbook intr"] == 2016
    assert all(y != 1990 for y in years.values()), years


def test_site_codes_are_named_by_the_register(led):
    assert registers.names_for_codes(ctx(), ["AA", "CC", "ZZ"]) == {"AA": "Alpha City", "CC": "Gamma Port"}


# ---------------------------------------------------------------- answers ---

def test_a_site_row_never_leaks_into_the_company_figure(led):
    obj = ask("What was revenue in 2015?")
    assert obj.answer.startswith(f"### Answer\nRevenue in 2015: {REVENUE[2015]:g} INR Cr [F")
    assert obj.verified


def test_which_site_is_ranked_from_the_ledger_and_reconciled_with_the_company(led):
    obj = ask("Which site had the highest revenue in 2018?")
    totals = {s: sum(r[4] for r in DAILY if r[1] == 2018 and r[3] == s) / 1e7 for s in SITES}
    top = max(totals, key=totals.get)
    first = obj.answer.split("\n", 2)[1]
    assert first.startswith(f"Highest total revenue (2018): {top} ({SITE_NAMES[top]}) with ")
    whole = next(c for c in obj.calculations if c["name"] == "revenue: entities vs company")
    assert whole["value"]["company_total"] == pytest.approx(REVENUE[2018])
    assert whole["value"]["entities_total"] == pytest.approx(sum(totals.values()), abs=1e-2)
    assert "sums of the daily ledger rows" in first and "stated company figure" in first
    assert obj.verified


def test_two_periods_are_compared_whole_with_the_headline_figures(led):
    obj = ask("Compare 2010-2015 with 2020-2025")
    assert obj.plan["scope"]["periods"] == [[2010, 2015], [2020, 2025]]
    assert obj.plan["metrics"][:2] == ["revenue", "net profit"]
    calc = next(c for c in obj.calculations if c["name"] == "periods of revenue")
    first, last = calc["value"]["periods"]
    assert first["average"] == pytest.approx(statistics.fmean(REVENUE[y] for y in range(2010, 2016)), abs=1e-3)
    assert last["total"] == pytest.approx(sum(REVENUE[y] for y in range(2020, 2026)), abs=1e-3)
    assert "Revenue - 2010-2015: average" in obj.answer and "Highest total" not in obj.answer
    assert obj.verified


def test_no_loss_and_the_one_fall_are_stated(led):
    obj = ask("Were there any losses between 2015 and 2022?")
    assert "No loss is recorded in 2015-2022" in obj.answer
    assert "Net profit fell in 1 year(s): 2020" in obj.answer


def test_a_dated_event_in_the_period_joins_a_why_question(led):
    obj = ask("Why did profit fall in 2020?")
    assert "COVID continuity protocol" in obj.answer.split("### Evidence")[0]


def test_a_milestone_that_contradicts_the_figures_is_reported(led):
    obj = ask("What was revenue in 2014?")
    statement = [c for c in obj.contradictions if c["type"] == "statement"]
    assert statement and statement[0]["claimed"] == 500 and statement[0]["recorded"] == pytest.approx(REVENUE[2014])
    assert "disagrees with the recorded figures" in obj.answer
    assert obj.verified


def test_stated_monthly_figures_against_daily_sums_are_one_reconciliation_line(led):
    obj = ask("What was revenue in 2015?")
    summary = [c for c in obj.contradictions if c["type"] == "reconciliation_summary"]
    assert len(summary) == 1 and summary[0]["count"] == 12
    assert summary[0]["largest_gap_percent"] == pytest.approx(-4.76, abs=0.01)     # ledger 1/1.05 of the report
    assert summary[0]["first_period"] == "2015-01"                                  # never '2015 M1' - read as a citation
    assert obj.validation["ok"] and not obj.validation["invalid_citations"]


def test_the_timeline_carries_the_dated_events_and_the_dataset(led):
    from knowledge_fabric import coverage
    report = coverage.report(ctx(), 2010, 2025)
    by_year = {r["year"]: r for r in report["years"]}
    assert by_year[2020]["event_count"] == 1
    assert by_year[2020]["events"][0]["label"] == "2020-04-01 COVID continuity protocol (Risk; impact: Volume shock)"
    detail = coverage.year_detail(ctx(), 2014)
    assert [e["label"] for e in detail["events"]] == ["2014-03-31 Crosses INR 500 Cr annual revenue (Financial; impact: Scale marker)"]


def test_a_register_does_not_answer_for_a_measure_it_lacks(led):
    assert registers.answer("Which site had the highest revenue?", ctx()) is None
    assert registers.answer("Which sites were opened?", ctx()) is not None


# ------------------------------------------------------------------- units ---

def test_two_ranges_in_one_question_are_two_periods():
    scope = temporal.parse("compare 2010-2015 with 2020-2025")
    assert scope.periods == [[2010, 2015], [2020, 2025]] and (scope.start, scope.end) == (2010, 2025)
    assert temporal.parse("revenue from 2010 to 2015").periods == []


def _series(values):
    return analytics.Series("revenue", None, "INR Cr", [
        analytics.Point(y, v, "INR Cr", i, "doc", None) for i, (y, v) in enumerate(sorted(values.items()))])


def test_a_period_with_missing_years_says_so():
    calc = analytics.compare_periods(_series({2010: 10, 2011: 12, 2020: 30}), [[2010, 2012], [2020, 2021]])
    assert calc.value["periods"][0]["years"] == 2 and calc.value["periods"][1]["years"] == 1
    assert "2010-2012: figures for 2 of 3 years" in calc.notes
    assert calc.value["average_change"]["absolute"] == pytest.approx(30 - 11)


def test_monthly_figures_are_not_added_on_top_of_yearly_ones():
    facts = [{"metric_norm": "revenue", "entity_label": "AA", "year": 2020, "value": 120.0, "unit": "INR Cr"},
             {"metric_norm": "revenue", "entity_label": "AA", "year": 2020, "month": 1, "value": 10.0, "unit": "INR Cr"},
             {"metric_norm": "revenue", "entity_label": "BB", "year": 2020, "month": 1, "value": 7.0, "unit": "INR Cr"}]
    ranking = analytics.rank_entities(facts, "revenue")
    assert [(r["entity"], r["total"]) for r in ranking.value] == [("AA", 120.0), ("BB", 7.0)]


def test_the_same_site_year_in_two_documents_is_counted_once():
    facts = [{"metric_norm": "revenue", "entity_label": "AA", "year": 2025, "value": 600.0, "unit": "INR Cr",
              "fact_id": 1, "document_id": "ledger"},
             {"metric_norm": "revenue", "entity_label": "AA", "year": 2025, "value": 600.0, "unit": "INR Cr",
              "fact_id": 2, "document_id": "sample sheet of the ledger"},
             {"metric_norm": "revenue", "entity_label": "BB", "year": 2025, "value": 500.0, "unit": "INR Cr",
              "fact_id": 3, "document_id": "ledger"}]
    ranking = analytics.rank_entities(facts, "revenue")
    assert [(r["entity"], r["total"]) for r in ranking.value] == [("AA", 600.0), ("BB", 500.0)]


def test_a_derived_sum_against_a_stated_figure_is_a_reconciliation():
    stated = {"metric_norm": "revenue", "year": 2015, "month": 3, "value": 105.0, "document_id": "a", "title": "Report"}
    summed = {"metric_norm": "revenue", "year": 2015, "month": 3, "value": 100.0, "document_id": "b", "title": "Ledger",
              "cell_ref": "R2-R32C5 (sum)", "source_text": "sum of 31 daily rows"}
    [conflict] = contradictions.numeric_conflicts([summed, stated])
    assert conflict["type"] == "reconciliation" and conflict["period"] == "2015-03"
    assert conflict["suggested_preference"]["document_id"] == "a" and conflict["percent"] == pytest.approx(-4.76, abs=0.01)
    assert "(summed rows)" in contradictions.line(conflict)
