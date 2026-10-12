"""
Report builder: IndustrialAnalysis -> a real .docx.

The same analysis that produces the deck and the workbook, written out as a
document a person can sign: a cover page with the recommendation and the key
numbers, then the executive summary, what the data shows, the evidence tables,
recommendations, risks, the plan, and where every figure came from.

Formatting is done properly rather than by dumping paragraphs:

  * a cover page - navy title band, recommendation callout, KPI band - then a
    page break, so the document opens on a statement rather than a wall of text
  * a running header carrying the document title, and a footer with page
    numbers as real Word fields
  * heading styles set once, with a rule under each section heading
  * tables with a shaded header row that REPEATS across page breaks, banded
    rows, sized columns and no orphaned single rows
  * severity carried in colour, consistently with the deck and the workbook
  * an UNSIGNED approval block - ZEUS never marks anything approved

The layout grammar was calibrated against a professional reference document;
none of its content, wording or branding is reproduced.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.branding import APP_TAGLINE as BRAND_TAGLINE
from core.logger import get_logger
from industrial.analysis import BAD, GOOD, IndustrialAnalysis, TableBlock, WARN

logger = get_logger("industrial.report")

# Usable text width of a Letter page with one-inch margins, in inches. The KPI
# cards divide it between them.
_TEXT_WIDTH_IN = 6.5

# Bold Arial averages ~0.55 em per character across mixed-case words and up to
# 0.59 for the longest ones. Measured from Liberation Sans Bold, which is what
# Word and LibreOffice substitute; 0.58 covers it with headroom, and the 0.90
# keeps back the cell padding python-docx does not expose.
_BOLD_WIDTH_FACTOR = 0.58
_CELL_USABLE = 0.90


def _value_points(text: str, cards: int, largest: float = 18.0,
                  smallest: float = 9.0) -> float:
    """The largest point size at which a KPI value fits its card on one line."""
    text = str(text or "")
    if not text or cards <= 0:
        return largest
    usable = (_TEXT_WIDTH_IN / cards) * _CELL_USABLE
    size = largest
    while size > smallest:
        if len(text) * (size * _BOLD_WIDTH_FACTOR) / 72.0 <= usable:
            return size
        size -= 0.5
    return smallest

# Kept in step with deck_builder / workbook_builder so one analysis looks like
# one family of documents.
INK = (0x1F, 0x29, 0x33)
NAVY = (0x10, 0x26, 0x3F)
ACCENT = (0x0E, 0x74, 0x90)
MUTED = (0x5A, 0x6B, 0x7B)
RED = (0xB4, 0x23, 0x18)
AMBER = (0x8A, 0x5A, 0x00)
GREEN = (0x1B, 0x7A, 0x3D)
WHITE = (0xFF, 0xFF, 0xFF)

NAVY_HEX = "10263F"
BAND_HEX = "F2F6FA"
HEAD_HEX = "10263F"
NOTE_HEX = "EAF2FB"
TOTAL_HEX = "D8F0E2"
LINE_HEX = "C9D6E2"

TONE_RGB = {GOOD: GREEN, BAD: RED, WARN: AMBER}
SEVERITY_RGB = {"critical": RED, "high": RED, "medium": AMBER, "certain": RED}


def _qn(tag: str):
    from docx.oxml.ns import qn
    return qn(tag)


def _element(tag: str):
    from docx.oxml import OxmlElement
    return OxmlElement(tag)


def _shade(cell, hex_colour: str):
    """python-docx has no cell shading API; this is the supported XML route."""
    shading = _element("w:shd")
    shading.set(_qn("w:val"), "clear")
    shading.set(_qn("w:color"), "auto")
    shading.set(_qn("w:fill"), hex_colour)
    cell._tc.get_or_add_tcPr().append(shading)


def _repeat_header(row):
    """Make a header row repeat at the top of every page the table spans."""
    properties = row._tr.get_or_add_trPr()
    header = _element("w:tblHeader")
    header.set(_qn("w:val"), "true")
    properties.append(header)


def _no_split(row):
    properties = row._tr.get_or_add_trPr()
    properties.append(_element("w:cantSplit"))


def _rule(paragraph, colour: str = LINE_HEX, size: int = 8, space: int = 2):
    """A horizontal rule under a paragraph, drawn as a real bottom border."""
    borders = _element("w:pBdr")
    bottom = _element("w:bottom")
    bottom.set(_qn("w:val"), "single")
    bottom.set(_qn("w:sz"), str(size))
    bottom.set(_qn("w:space"), str(space))
    bottom.set(_qn("w:color"), colour)
    borders.append(bottom)
    paragraph._p.get_or_add_pPr().append(borders)


def _field(paragraph, instruction: str):
    """Insert a Word field (used for PAGE / NUMPAGES) so the footer is live."""
    run = paragraph.add_run()
    begin = _element("w:fldChar")
    begin.set(_qn("w:fldCharType"), "begin")
    text = _element("w:instrText")
    text.set(_qn("xml:space"), "preserve")
    text.text = f" {instruction} "
    end = _element("w:fldChar")
    end.set(_qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(text)
    run._r.append(end)
    return run


class ReportBuilder:
    def __init__(self, analysis: IndustrialAnalysis):
        from docx import Document
        self.analysis = analysis
        self.document = Document()
        self._configure_styles()
        self._configure_page()

    # ---------------------------------------------------------- setup ----

    def _configure_styles(self):
        from docx.shared import Pt, RGBColor
        try:
            normal = self.document.styles["Normal"]
            normal.font.name = "Calibri"
            normal.font.size = Pt(10.5)
            normal.paragraph_format.space_after = Pt(6)
            normal.paragraph_format.line_spacing = 1.12
        except Exception as error:
            logger.debug(f"normal style setup skipped: {error}")

        for name, size, colour, space_before in (("Heading 1", 15, NAVY, 16),
                                                 ("Heading 2", 12.5, ACCENT, 10),
                                                 ("Title", 26, NAVY, 0)):
            try:
                style = self.document.styles[name]
                style.font.name = "Calibri"
                style.font.size = Pt(size)
                style.font.bold = True
                style.font.color.rgb = RGBColor(*colour)
                style.paragraph_format.space_before = Pt(space_before)
                style.paragraph_format.space_after = Pt(6)
                style.paragraph_format.keep_with_next = True
            except Exception as error:
                logger.debug(f"{name} style setup skipped: {error}")

    def _configure_page(self):
        """Margins that leave room for wide evidence tables, plus a running
        header and a page-number footer."""
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt, RGBColor, Inches
        try:
            for section in self.document.sections:
                section.left_margin = Inches(0.75)
                section.right_margin = Inches(0.75)
                section.top_margin = Inches(0.7)
                section.bottom_margin = Inches(0.7)
                section.header_distance = Inches(0.35)
                section.footer_distance = Inches(0.35)

                header = section.header.paragraphs[0]
                header.text = ""
                run = header.add_run(self.analysis.title)
                run.font.size = Pt(8.5)
                run.font.color.rgb = RGBColor(*MUTED)
                header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                _rule(header)

                footer = section.footer.paragraphs[0]
                footer.text = ""
                footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
                lead = footer.add_run(f"ZEUS — {BRAND_TAGLINE}  ·  page ")
                lead.font.size = Pt(8)
                lead.font.color.rgb = RGBColor(*MUTED)
                page = _field(footer, "PAGE")
                page.font.size = Pt(8)
                page.font.color.rgb = RGBColor(*MUTED)
                middle = footer.add_run(" of ")
                middle.font.size = Pt(8)
                middle.font.color.rgb = RGBColor(*MUTED)
                total = _field(footer, "NUMPAGES")
                total.font.size = Pt(8)
                total.font.color.rgb = RGBColor(*MUTED)
        except Exception as error:
            logger.debug(f"page setup skipped: {error}")

    # ------------------------------------------------------- primitives --

    def _para(self, text: str, size: float = 10.5, colour=INK, bold: bool = False,
              italic: bool = False, space_after: float = 6, align: str = "left"):
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt, RGBColor
        paragraph = self.document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(space_after)
        paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT,
                               "center": WD_ALIGN_PARAGRAPH.CENTER,
                               "right": WD_ALIGN_PARAGRAPH.RIGHT}[align]
        run = paragraph.add_run(str(text))
        run.font.size = Pt(size)
        run.bold = bold
        run.italic = italic
        run.font.color.rgb = RGBColor(*colour)
        return paragraph

    def _label(self, text: str):
        return self._para(text.upper(), size=8.5, colour=ACCENT, bold=True,
                          space_after=2)

    def _heading(self, text: str, level: int = 1):
        heading = self.document.add_heading(text, level=level)
        if level == 1:
            _rule(heading, LINE_HEX, size=6, space=4)
        return heading

    def _band(self, text: str, fill: str = NAVY_HEX, colour=WHITE, size: float = 22,
              bold: bool = True, subtitle: str = "", height_pad: int = 8):
        """A full-width shaded band, built as a one-cell table - the reliable
        way to get a coloured block across the page in Word."""
        from docx.shared import Pt, RGBColor
        table = self.document.add_table(rows=1, cols=1)
        table.autofit = True
        cell = table.rows[0].cells[0]
        _shade(cell, fill)
        cell.text = ""
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_before = Pt(height_pad)
        paragraph.paragraph_format.space_after = Pt(4 if subtitle else height_pad)
        run = paragraph.add_run(text)
        run.font.size = Pt(size)
        run.bold = bold
        run.font.color.rgb = RGBColor(*colour)
        if subtitle:
            second = cell.add_paragraph()
            second.paragraph_format.space_after = Pt(height_pad)
            run = second.add_run(subtitle)
            run.font.size = Pt(10.5)
            run.font.color.rgb = RGBColor(0xC7, 0xD6, 0xE2)
        self._clear_borders(table)
        return table

    @staticmethod
    def _clear_borders(table):
        borders = _element("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            element = _element(f"w:{edge}")
            element.set(_qn("w:val"), "none")
            element.set(_qn("w:sz"), "0")
            borders.append(element)
        table._tbl.tblPr.append(borders)

    def _kpi_band(self):
        """The key numbers as a row of cards, the same shape as the deck's KPI
        row - so the cover of the report and the first slide agree by eye as
        well as by figure."""
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt, RGBColor
        kpis = self.analysis.kpis[:5]
        if not kpis:
            return None
        table = self.document.add_table(rows=1, cols=len(kpis))
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for cell, kpi in zip(table.rows[0].cells, kpis):
            _shade(cell, BAND_HEX)
            cell.text = ""
            label = cell.paragraphs[0]
            label.alignment = WD_ALIGN_PARAGRAPH.CENTER
            label.paragraph_format.space_before = Pt(6)
            label.paragraph_format.space_after = Pt(0)
            run = label.add_run(kpi.label.upper())
            run.font.size = Pt(7.5)
            run.bold = True
            run.font.color.rgb = RGBColor(*MUTED)

            value = cell.add_paragraph()
            value.alignment = WD_ALIGN_PARAGRAPH.CENTER
            value.paragraph_format.space_after = Pt(0)
            run = value.add_run(kpi.value)
            # Five cards across the text width give each about 1.25 inches.
            # Most KPI values are numbers and fit at 18pt; a word like
            # "Maintenance" does not, and Word wraps it mid-word, dropping the
            # stray letter onto the line where the note belongs. Word has no
            # shrink-to-fit for a run, so the size is chosen here - by the same
            # measurement the deck uses, so the two documents agree by eye.
            run.font.size = Pt(_value_points(kpi.value, len(kpis)))
            run.bold = True
            run.font.color.rgb = RGBColor(*TONE_RGB.get(kpi.tone, ACCENT))

            if kpi.note:
                note = cell.add_paragraph()
                note.alignment = WD_ALIGN_PARAGRAPH.CENTER
                note.paragraph_format.space_after = Pt(6)
                run = note.add_run(kpi.note)
                run.font.size = Pt(7.5)
                run.font.color.rgb = RGBColor(*MUTED)
        self._clear_borders(table)
        return table

    def _callout(self, label: str, text: str, fill: str = NOTE_HEX,
                 colour=NAVY, size: float = 12):
        from docx.shared import Pt, RGBColor
        table = self.document.add_table(rows=1, cols=1)
        cell = table.rows[0].cells[0]
        _shade(cell, fill)
        cell.text = ""
        first = cell.paragraphs[0]
        first.paragraph_format.space_before = Pt(6)
        first.paragraph_format.space_after = Pt(2)
        run = first.add_run(label.upper())
        run.font.size = Pt(8)
        run.bold = True
        run.font.color.rgb = RGBColor(*ACCENT)
        body = cell.add_paragraph()
        body.paragraph_format.space_after = Pt(6)
        run = body.add_run(text)
        run.font.size = Pt(size)
        run.bold = True
        run.font.color.rgb = RGBColor(*colour)
        self._clear_borders(table)
        return table

    def _table(self, columns: List[str], rows: List[List[Any]],
               severity_column: Optional[str] = None,
               total_row: bool = False, font_size: float = 9):
        """A real table: shaded repeating header, banded rows, severity colour,
        and no row allowed to split across a page."""
        from docx.shared import Pt, RGBColor
        if not rows:
            self._para("No records.", italic=True, colour=MUTED)
            return None
        table = self.document.add_table(rows=1, cols=len(columns))
        table.style = "Table Grid"
        widths = self._column_widths(columns, rows)
        header = table.rows[0]
        _repeat_header(header)
        _no_split(header)
        for cell, name in zip(header.cells, columns):
            _shade(cell, HEAD_HEX)
            cell.text = ""
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_before = Pt(2)
            paragraph.paragraph_format.space_after = Pt(2)
            run = paragraph.add_run(str(name))
            run.bold = True
            run.font.size = Pt(font_size)
            run.font.color.rgb = RGBColor(*WHITE)

        severity_index = (columns.index(severity_column)
                          if severity_column in columns else None)
        for position, row_values in enumerate(rows):
            row = table.add_row()
            _no_split(row)
            cells = row.cells
            banded = position % 2 == 1
            last = total_row and position == len(rows) - 1
            for index, value in enumerate(row_values):
                if index >= len(cells):
                    break
                if last:
                    _shade(cells[index], TOTAL_HEX)
                elif banded:
                    _shade(cells[index], BAND_HEX)
                cells[index].text = ""
                paragraph = cells[index].paragraphs[0]
                paragraph.paragraph_format.space_before = Pt(1)
                paragraph.paragraph_format.space_after = Pt(1)
                text = "" if value is None else str(value)
                run = paragraph.add_run(text)
                run.font.size = Pt(font_size)
                if index == 0 or last:
                    run.bold = True
                if last:
                    run.font.color.rgb = RGBColor(0x10, 0x60, 0x3A)
                elif severity_index is not None and index == severity_index:
                    colour = SEVERITY_RGB.get(text.lower())
                    if colour:
                        run.bold = True
                        run.font.color.rgb = RGBColor(*colour)
        self._apply_widths(table, widths)
        self.document.add_paragraph()
        return table

    def _usable_width(self) -> float:
        from docx.shared import Emu
        try:
            section = self.document.sections[0]
            return (Emu(section.page_width).inches - Emu(section.left_margin).inches
                    - Emu(section.right_margin).inches)
        except Exception:
            return 7.0

    def _column_widths(self, columns: List[str], rows: List[List[Any]]) -> List[float]:
        """Width by content, not equal shares. Word's autofit gives a date column
        the same room as a description, which is how 'Crude Preheat Exchanger'
        ends up stacked three words high."""
        total_width = self._usable_width()
        weights = []
        for index, name in enumerate(columns):
            header = str(name)
            longest = max(max([len(word) for word in header.split()] or [4]) + 2,
                          min(len(header), 16))
            for row in rows[:80]:
                if index < len(row) and row[index] is not None:
                    longest = max(longest, min(len(str(row[index])), 48))
            weights.append(max(6, longest))
        total = sum(weights)
        widths = [total_width * weight / total for weight in weights]
        minimum = 0.55
        deficit = sum(minimum - width for width in widths if width < minimum)
        if deficit > 0:
            surplus = sum(width - minimum for width in widths if width > minimum) or 1
            widths = [minimum if width < minimum
                      else width - deficit * (width - minimum) / surplus
                      for width in widths]
        return widths

    @staticmethod
    def _apply_widths(table, widths: List[float]):
        from docx.shared import Inches
        try:
            table.autofit = False
            for row in table.rows:
                for index, cell in enumerate(row.cells):
                    if index < len(widths):
                        cell.width = Inches(round(widths[index], 3))
        except Exception as error:
            logger.debug(f"column sizing skipped: {error}")

    # ---------------------------------------------------------- content --

    def cover(self):
        """A cover page: who made it, what it says, and the numbers that back
        it - then a page break so section one starts clean."""
        self._para("ZEUS", size=10, colour=ACCENT, bold=True, space_after=0)
        self._para(BRAND_TAGLINE.upper(), size=8, colour=ACCENT, bold=True,
                   space_after=4)
        self._band(self.analysis.title, subtitle=self.analysis.subtitle or "")
        self._para(f"Generated locally on {self.analysis.generated_at}. "
                   f"No data left this machine.", size=9, colour=MUTED,
                   space_after=4)
        if self.analysis.contains_demo_data:
            self._para(self.analysis.demo_banner(), size=9.5, colour=AMBER,
                       bold=True, space_after=8)

        if self.analysis.headline:
            self._callout("Recommendation", self.analysis.headline)
        if self.analysis.kpis:
            self._para("", size=6, space_after=2)
            self._kpi_band()
        if self.analysis.decision_request:
            self._para("", size=6, space_after=2)
            self._callout("Decision requested", self.analysis.decision_request,
                          fill=BAND_HEX, size=11)

        contents = [name for name, present in (
            ("Executive summary", bool(self.analysis.executive_summary)),
            ("Key numbers", bool(self.analysis.kpis)),
            ("What the data shows", bool(self.analysis.findings)),
            ("Evidence", bool(self.analysis.tables)),
            ("Recommended actions", bool(self.analysis.recommendations)),
            ("Risks", bool(self.analysis.risks)),
            ("Plan", bool(self.analysis.timeline)),
            ("Sources and assumptions", True)) if present]
        if contents:
            self._para("", size=6, space_after=2)
            self._label("Contents")
            self._para("   ·   ".join(contents), size=9.5, colour=MUTED)

        self.document.add_page_break()

    def summary(self):
        if self.analysis.executive_summary:
            self._heading("Executive summary", level=1)
            self._para(self.analysis.executive_summary, size=11)
        if self.analysis.kpis:
            self._heading("Key numbers", level=1)
            self._table(["Measure", "Value", "Change", "Context"],
                        [k.as_row() for k in self.analysis.kpis], font_size=9.5)

    def findings(self):
        if not self.analysis.findings:
            return
        self._heading("What the data shows", level=1)
        self._para("Each finding carries the figure it is based on and the record "
                   "it came from.", size=9.5, colour=MUTED, italic=True)
        self._table(["Severity", "Finding", "Detail", "Magnitude", "Evidence"],
                    [[f.severity.title(), f.title, f.detail, f.magnitude,
                      f.evidence.replace("\n", " ")] for f in self.analysis.findings],
                    severity_column="Severity")

    def evidence(self, max_rows: int = 60):
        if not any(block.rows for block in self.analysis.tables):
            return
        self._heading("Evidence", level=1)
        self._para("The records behind every figure above, exactly as held in the "
                   "local database.", size=9.5, colour=MUTED, italic=True)
        for block in self.analysis.tables:
            if not block.rows:
                continue
            self._heading(block.title, level=2)
            if block.note:
                self._para(block.note, size=9.5, colour=MUTED, italic=True)
            rows = block.rows[:max_rows]
            self._table(block.columns, rows,
                        severity_column="Severity" if "Severity" in block.columns
                        else None)
            if len(block.rows) > max_rows:
                self._para(f"{len(block.rows) - max_rows} further row(s) are in the "
                           f"accompanying workbook.", size=9, colour=MUTED,
                           italic=True)

    def recommendations(self):
        if not self.analysis.recommendations:
            return
        self._heading("Recommended actions", level=1)
        self._table(["#", "Action", "Why", "Impact", "Effort", "Owner"],
                    [[index, r.title, r.rationale, r.impact, r.effort, r.owner]
                     for index, r in enumerate(
                         sorted(self.analysis.recommendations,
                                key=lambda r: r.priority), start=1)])

    def risks(self):
        if not self.analysis.risks:
            return
        self._heading("Risks", level=1)
        self._table(["Risk", "Likelihood", "Impact", "Mitigation"],
                    [[r.risk, r.likelihood.title(), r.impact.title(), r.mitigation]
                     for r in self.analysis.risks],
                    severity_column="Impact")

    def roadmap(self):
        if not self.analysis.timeline:
            return
        self._heading("Plan", level=1)
        self._table(["Phase", "Window", "Actions", "Outcome"],
                    [[m.phase, m.window, "\n".join(f"• {a}" for a in m.actions),
                      m.outcome] for m in self.analysis.timeline])

    def sources(self):
        self._heading("Sources and assumptions", level=1)
        rows = [["Generated", self.analysis.generated_at],
                ["Produced by", f"ZEUS — {BRAND_TAGLINE} (local, offline)"],
                ["Narrative", "written by the local model, constrained to these figures"
                 if self.analysis.narrative_included
                 else "not used - text and figures are computed from records"]]
        rows += [["Data source", item] for item in self.analysis.source_data]
        rows += [["Citation", item] for item in self.analysis.citations]
        if self.analysis.contains_demo_data:
            rows.append(["Warning", self.analysis.demo_banner()])
        rows += [["Network", "none - nothing left this machine"],
                 ["Verification", "this file was reopened and parsed after writing; "
                                  "a file that fails that check is reported as an "
                                  "error, never as a success"]]
        self._table(["Item", "Detail"], rows, font_size=9.5)

    def approval_block(self, approver: str = ""):
        """An UNSIGNED approval block. ZEUS does not approve anything."""
        self._heading("Approval", level=1)
        if self.analysis.decision_request:
            self._para(self.analysis.decision_request, bold=True)
        self._table(["Role", "Name", "Signature", "Date"],
                    [["Prepared by", "ZEUS (generated)", "",
                      self.analysis.generated_at],
                     ["Reviewed by", approver, "", ""],
                     ["Approved by", "", "", ""]], font_size=9.5)
        self._para("This document is not approved until a competent person signs it. "
                   "ZEUS does not approve findings, permits or isolations.",
                   size=9.5, bold=True, colour=RED)

    # ------------------------------------------------------------ build --

    def build(self, include_approval: bool = False,
              approver: str = "") -> "ReportBuilder":
        self.cover()
        self.summary()
        self.findings()
        self.evidence()
        self.recommendations()
        if self.analysis.financials and self.analysis.financials.rows:
            self._heading("Financial impact", level=1)
            self._table(self.analysis.financials.columns,
                        self.analysis.financials.rows)
        self.risks()
        self.roadmap()
        self.sources()
        if include_approval:
            self.approval_block(approver)
        return self

    def save(self, path) -> str:
        self.document.save(str(path))
        return str(path)


def build_report(analysis: IndustrialAnalysis, path, include_approval: bool = False,
                 approver: str = "") -> Dict[str, Any]:
    from core import office
    builder = ReportBuilder(analysis).build(include_approval=include_approval,
                                            approver=approver)
    builder.save(path)
    return office.finish(path, "docx", extra={
        "analysis": analysis.summary_dict(),
        "demo_data": analysis.contains_demo_data,
        "narrative_included": analysis.narrative_included,
        "approval_block": include_approval,
    })
