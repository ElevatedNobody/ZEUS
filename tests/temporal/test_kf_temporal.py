"""Temporal RAG: time expressions -> periods, context windows, dated evidence."""

import pytest

from knowledge_fabric import temporal


@pytest.mark.parametrize("question, start, end, points", [
    ("What was revenue in 2019?", 2019, 2019, []),
    ("Why did profit fall between 2019 and 2021?", 2019, 2021, []),
    ("net profit from 2015 to 2020", 2015, 2020, []),
    ("revenue since 2015", 2015, 2023, []),
    ("maintenance cost over the last five years", 2019, 2023, []),
    ("Compare revenue in 2016 and 2023", None, None, [2016, 2023]),
    ("revenue 2016 vs 2023", None, None, [2016, 2023]),
    ("revenue in 2016, 2019 and 2023", None, None, [2016, 2019, 2023]),
    ("what happened in the 1990s", 1990, 1999, []),
    ("what did we spend last year", 2022, 2022, []),
])
def test_periods(question, start, end, points):
    scope = temporal.parse(question, latest_year=2023, earliest_year=1989)
    assert (scope.start, scope.end) == (start, end)
    assert scope.points == points


def test_relative_periods_resolve_against_the_records_not_the_calendar():
    scope = temporal.parse("the last three years", latest_year=2011)
    assert (scope.start, scope.end) == (2009, 2011)
    assert any("latest year in the records" in a for a in scope.assumptions)


def test_quarters_and_months():
    q = temporal.parse("downtime in Q2 2021")
    assert (q.start, q.quarter) == (2021, 2)
    m = temporal.parse("the trip in March 2020")
    assert (m.start, m.month) == (2020, 3)


def test_fiscal_year_labels():
    scope = temporal.parse("profit in FY2019-20")
    assert (scope.start, scope.end) == (2019, 2020)
    assert scope.fiscal == "FY2019-20"


def test_cause_questions_widen_the_window_for_a_baseline():
    scope = temporal.parse("why did we make losses between 2019 and 2021", latest_year=2023)
    assert (scope.start, scope.end) == (2019, 2021)
    assert scope.window() == (2018, 2021)            # the year before, to compare against


def test_period_labels_in_table_headers():
    assert temporal.period_of("2019")["year"] == 2019
    assert temporal.period_of("Q3 2021")["quarter"] == 3


def test_a_future_year_in_a_paragraph_does_not_date_it(kf):
    """Board minutes of 2021 say a project was deferred 'to 2023' - that
    paragraph reports on 2021, not 2023."""
    from knowledge_fabric import store
    with store.connection() as conn:
        row = conn.execute("SELECT n.year FROM kf_nodes n JOIN kf_documents d ON d.document_id = n.document_id "
                           "WHERE d.filename = 'TFR_Board_Minutes_2021.md' AND n.text LIKE '%defer%'").fetchone()
    assert row["year"] == 2021


def test_retrieval_respects_the_period(kf):
    from tests.conftest import kf_ask
    obj = kf_ask("What was the maintenance cost in 2019?")
    assert obj.facts and all(f["year"] == 2019 for f in obj.facts)
    # A passage is either about 2019, or it is a table that holds a 2019 figure
    # (the 2020 maintenance report's table has a 2019 column).
    fact_nodes = {f["node_id"] for f in obj.facts}
    assert all(e["year"] in (2019, None) or e["node_id"] in fact_nodes
               for e in obj.evidence if e["source"] == "fabric")


def test_no_records_for_a_year_is_an_answer_not_an_invention(kf):
    from tests.conftest import kf_ask
    obj = kf_ask("What was revenue in 1995?")
    assert not obj.evidence and not obj.facts
    assert "No document you can access" in obj.answer
    assert obj.gaps.get("missing_years") == [1995]
