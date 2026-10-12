# Document Generation Report

How ZEUS turns industrial data into professional deliverables, and what makes
these different from "a script that writes a table into a file".

**23 document-quality tests pass. Every generated file is verified by reopening
it before ZEUS reports success.**

---

## 1. The shared analysis layer

The requirement that shaped everything else: PPTX, XLSX and DOCX must not
disagree with each other. So none of them read the database. They all render
one object.

```
                    industrial/analysis.py
                    IndustrialAnalysis
                      headline, executive_summary
                      kpis[]          findings[]
                      tables[]        financials
                      risks[]         recommendations[]
                      timeline[]      decision_request
                      source_data[]   citations[]
                      contains_demo_data
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  report_builder        workbook_builder       deck_builder
      .docx                 .xlsx                 .pptx
```

Builders: `build_operations_analysis()` (whole plant), `build_equipment_analysis(tag)`
(one asset), `build_workbook_analysis(path)` (a spreadsheet the user supplies).

A test proves the point: it renders all three from one analysis and asserts the
headline KPI and the recommendation appear verbatim in each file.

**The narrative is optional.** Facts, tables and figures are computed from
records. When Ollama is up, `add_narrative()` layers an executive summary on
top, constrained to the figures it was given. When Ollama is down the report is
still produced, with a deterministic summary, and says so. Ollama is not running
in the build environment, which is how that path got tested.

## 2. PowerPoint

Built from slide **types**, each answering a question, not from a bullet list:

| Slide | Answers |
|---|---|
| Title | what is this, and what is the recommendation (with a KPI row) |
| Executive summary | where things stand, numbers beside the prose |
| Diagnosis | what is causing it - one card per cause, each with its magnitude and a severity chip |
| Metrics | a native, editable PowerPoint chart plus a written read-out of what it shows |
| Evidence | the actual records, paginated so nothing is crushed |
| Recommended actions | numbered, with impact and effort |
| Risks | likelihood, impact, mitigation, severity-coloured |
| Roadmap | phases with actions and the outcome each is judged on |
| Decision | the ask, the data sources, and how the deck was produced |

Rules enforced in code rather than left to judgement:

- **Nothing overflows.** Cards are sized from the space that exists. A test
  walks every shape on every slide and fails if anything crosses the edge - it
  was written because the fourth diagnosis card originally ran off the bottom.
- **Nothing below 8.5 pt**, asserted by test.
- **Tables paginate** rather than shrink.
- **Colour means something**: severity and tone only.
- **Charts are native** `c:chart` parts, so they stay editable in PowerPoint.
- Every deck ends with its sources, and carries a DEMO banner when the figures
  are sample data.

Typical output: 11 slides, ~59 KB, 1 chart, 4 evidence tables.

## 3. Excel

Multi-sheet, with each sheet serving a different reader:

| Sheet | For |
|---|---|
| **Summary** | management: recommendation, executive summary, KPI table with tone colouring, numbered actions with why/impact/effort/owner, the decision requested |
| *(one per dataset)* | the analyst: full records, frozen header, autofilter, sized columns, totals row, conditional formatting |
| **Analysis** | findings, a live severity profile, a chart, and cross-sheet record counts |
| **Sources** | provenance: what produced every figure, and what is DEMO |

Two things separate this from an export:

**Real formulas.** Totals are `=SUM(F7:F14)`. The severity profile is
`=COUNTIF(A8:A16,"Critical")` with `=IFERROR(B20/COUNTA(...),0)` for the share.
Record counts reach across sheets: `=COUNTA('Open alerts'!A7:A11)`. Edit a row
and the summary follows.

Verified by recalculation, not by inspection: the workbook is opened in
LibreOffice, recalculated, and the cached values read back. The severity profile
returned 1 critical / 2 high / 5 medium / 1 low = 9 total with correct
percentage shares, and the maintenance sheet's `SUM` returned 76 hours.

**Typed cells.** Numbers are written as numbers with a number format, so they
sort and chart correctly. A test reads back the Hours column and fails if the
values arrive as text.

## 4. Word

Cover with the recommendation, executive summary, key-numbers table, findings
(severity-coloured), every evidence table, recommendations, financials, risks,
the plan, and sources. Proper heading levels and a `Table Grid` style
throughout, not a wall of paragraphs.

Documents that request approval get an **unsigned** signature block and the line
*"This document is not approved until a competent person signs it. ZEUS does
not approve findings, permits or isolations."* A test asserts the approver and
signature cells are empty.

## 5. Spreadsheet in, executive story out

`analyze_spreadsheet` / `spreadsheet_to_presentation` / `spreadsheet_to_report`
do not paste rows onto slides:

1. Read `.xlsx` / `.xlsm` / `.csv`, **finding the header row** - real sheets
   start with a title and a blank line, so the header is located by scoring
   rows rather than assuming A1.
2. Profile every column: numeric (total, average, min, max, count) or
   categorical (distinct values, top values).
3. Derive KPIs from the largest numeric totals.
4. Find what is worth saying: **concentration** (one category holding 40 %+ of
   the rows) and **outliers** (a maximum more than 2.5x its column average).
5. Render the resulting analysis as a deck, a report, or both.

Tested against the real multi-sheet reference workbook: it located the header
under each title block across 3 sheets, totalled 1,060,000 passengers across 5
values, and flagged both a 50 % category concentration and two 2.8-2.9x
outliers.

## 6. Verification - no file is claimed unseen

`core/office.py::verify_office_file` checks, in order: the file exists, is a
file, is non-empty, has the expected extension, and **can be reopened and
parsed by the library that wrote it**. `finish()` returns an error result when
any check fails - it cannot return a `saved` key for a bad file.

Three tests attack it directly: a missing file, an empty file, and a file with
the right name but corrupt bytes. All three must produce an error, not a
success.

## 7. Commands

| Say | Get |
|---|---|
| create an executive presentation | full deck of the plant |
| create a management presentation for P-204 | deck for one asset |
| create an analysis workbook | multi-sheet XLSX |
| create an executive report for P-204 | Word report |
| **create a briefing pack** | all three, from one analysis |
| analyse the spreadsheet `<path>` | profile, KPIs, findings |
| read this excel and create a presentation | spreadsheet → analysis → deck |
| generate a maintenance report | data-driven DOCX |
| export the maintenance history to Excel | filtered XLSX with provenance |

Routing note: these are matched **before** the generic "create a presentation
about anything" route, so a specific request gets real records rather than
invented prose. `read this excel and make a deck` is matched before the generic
`read <file>` route for the same reason.

## 8. Quality references

The reference deck and workbook were inspected for **structure only**: the
diagnosis → strategy → impact → roadmap → ask storyline, KPI-card treatment,
question-shaped slide titles, and the multi-audience sheet split (detail →
analysis → simplified view). No design, palette, branding, wording or business
content was reproduced. ZEUS's decks use the dashboard's own dark HUD palette;
the workbooks use a light print-friendly scheme.

One deliberate improvement on the reference workbook: it hard-codes its
calculated values. These workbooks use formulas, so the numbers stay honest
when someone edits a row.

## 9. Remaining issues

1. **Ollama was down in the build environment**, so the optional narrative path
   was exercised only through its failure branch. The figures, tables, charts
   and formulas are all model-independent and were verified fully.
2. **Chart types are limited** to column, bar and line. A pie chart is rarely
   the right answer, and nothing here needed a scatter yet.
3. **The workbook analyser reads values, not formulas**, from a supplied
   spreadsheet (`data_only=True`). A file saved by a tool that never calculated
   its formulas will present empty cells; the analyser reports the columns as
   non-numeric rather than guessing.
4. **Financial history, employees, contacts and the confidential-data
   categories** from the wider brief are not built yet - the analysis layer has
   a `financials` slot ready for them.
