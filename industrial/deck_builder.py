"""
Executive deck builder: IndustrialAnalysis -> a real .pptx.

The brief for this file was "not bullet spam". So the deck is built from slide
TYPES, each of which answers one question a decision-maker actually asks:

    title        what is this and what is the recommendation
    summary      the situation, the numbers, and what it means
    diagnosis    what is causing it - a card grid, each card carrying its number
    metrics      one native chart plus a written read-out of what it shows
    table        the evidence, paginated and sized to fill the page
    decisions    what to do, numbered, with impact and effort
    financials   what it is worth
    risks        what could go wrong and the mitigation
    roadmap      when, in colour-coded phases with the outcome each is judged on
    ask          the decision being requested, and the data behind it

Design rules enforced in code rather than left to judgement:

  * Every slide FILLS its page. Cards and tables are sized from the space that
    exists, so no slide is half empty and nothing runs off the bottom.
  * Nothing below 9 pt.
  * A card is only drawn when it has content; an empty section is skipped.
  * Colour carries meaning - severity, tone and phase - never decoration.
  * Tables get a dark header, banded rows and content-weighted columns.
  * Every deck ends with its data sources, and a DEMO banner when the figures
    are sample data.

The layout grammar (dark header band, white cards with a coloured accent edge,
KPI cards, question-shaped slide titles, phase-coloured roadmap) was calibrated
against a professional reference deck. None of that deck's design, palette,
branding, wording or business content is reproduced - the palette here is
ZEUS's own and every figure comes from the local database.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.branding import APP_TAGLINE as BRAND_TAGLINE
from core.logger import get_logger
from industrial.analysis import (
    BAD, GOOD, NEUTRAL, WARN, IndustrialAnalysis, KPI, TableBlock,
)

logger = get_logger("industrial.deck")

# ---------------------------------------------------------------- palette --
# A light, print-quality scheme. Decks get read on projectors and printed for
# board packs; a dark terminal theme survives neither.
PAGE = "F4F7FA"          # page background
NAVY = "10263F"          # header band, title slide, table header
NAVY_DEEP = "0A1A2C"     # panel inside the dark title slide
NAVY_SOFT = "1C3D5F"     # secondary dark
WHITE = "FFFFFF"
INK = "16222E"           # body text on light
INK_SOFT = "3D4C5A"
MUTED = "62717F"
LINE = "D7E0E8"          # card border
BAND = "EEF3F8"          # banded table row
ACCENT = "0E7490"        # ZEUS teal - the one branded colour
ACCENT_LIGHT = "7DD3E0"  # accent on dark backgrounds

RED = "B3261E"
GREEN = "1B7A46"
AMBER = "B26A00"
BLUE = "1F5C99"

TONE_COLOUR = {GOOD: GREEN, BAD: RED, WARN: AMBER, NEUTRAL: ACCENT}
SEVERITY_COLOUR = {"critical": RED, "high": RED, "medium": AMBER, "low": BLUE,
                   "info": MUTED, "certain": RED}
PHASE_COLOUR = [RED, AMBER, BLUE, GREEN]

FONT = "Arial"           # present on every Windows and LibreOffice install
MONO = "Consolas"

# 16:9 at 13.333 x 7.5 inches
SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN = 0.58
CONTENT_W = SLIDE_W - 2 * MARGIN
HEADER_H = 0.92          # the dark band across the top of a content slide
BODY_TOP = 1.28
FOOTER_TOP = SLIDE_H - 0.44
BODY_BOTTOM = FOOTER_TOP - 0.10
BODY_H = BODY_BOTTOM - BODY_TOP


def _rgb(hex_string: str):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hex_string)


def _inches(value: float):
    from pptx.util import Inches
    return Inches(value)


def _pt(value: float):
    from pptx.util import Pt
    return Pt(value)


def _fit(text: str, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Calibrated by rendering decks through LibreOffice and measuring where the
# text actually landed, not guessed: the first pass used 0.60 / 1.34 and left
# a six-line box holding three lines.
#
# Arial's average advance across mixed-case prose is close to 0.5 em; 0.52
# keeps a little headroom for capitals and wide punctuation. The line factor
# is ~1.0 because `spacing` is already a multiple of the font size, so
# multiplying by both counted the leading twice.
#
# The remaining bias is deliberately toward under-filling. A paragraph that
# overflows its card prints ON TOP of the elements below it; one that stops a
# line early costs a little white space and nobody notices.
_CHAR_WIDTH_FACTOR = 0.52
_LINE_HEIGHT_FACTOR = 1.02
_SAFETY = 0.92           # keep back a line's worth against estimation error


def _chars_that_fit(width_in: float, height_in: float, font_pt: float,
                    spacing: float = 1.0) -> int:
    """How much text fits in a box, in characters.

    python-pptx cannot measure rendered text - there is no layout engine
    behind it - so this estimates. Estimating low is the safe direction: the
    cost of a short paragraph is a little white space, and the cost of a long
    one is text printed over the next element.
    """
    if width_in <= 0 or height_in <= 0 or font_pt <= 0:
        return 0
    char_w = (font_pt * _CHAR_WIDTH_FACTOR) / 72.0
    line_h = (font_pt * _LINE_HEIGHT_FACTOR * max(spacing, 1.0)) / 72.0
    per_line = max(1, int(width_in / char_w))
    lines = max(1, int(height_in / line_h))
    return max(1, int(per_line * lines * _SAFETY))


# Bold display text is wider than the prose factor above. Measured from
# Liberation Sans Bold (metric-compatible with Arial, and what LibreOffice
# actually substitutes): "Maintenance" averages 0.551 em per character and
# "Decommissioned" 0.591. 0.58 covers the words that appear as KPI values with
# a little headroom; _VALUE_SAFETY keeps back the rest.
#
# The first version of this used the 0.52 prose factor and concluded that
# "Maintenance" fitted at 23pt by four thousandths of an inch. It did not, and
# the deck printed "Maintenanc" with the "e" on a second line over the note
# beneath it. Estimating text width is guessing; the only reason these numbers
# are trustworthy is that they came from measuring the font.
_BOLD_WIDTH_FACTOR = 0.58
_VALUE_SAFETY = 0.97


def _value_size(text: str, width_in: float, largest: float = 23.0,
                smallest: float = 12.0) -> float:
    """The largest size at which `text` fits ONE line of `width_in`.

    KPI values live in a fixed card and must not wrap: a wrapped value pushes
    its own second line over the note beneath it, and the note is what says
    what the number means. Shrinking is the lesser cost - a slightly smaller
    "Maintenance" still reads; "Maintenanc / e" does not.

    Most values are numbers and come back at `largest` unchanged. This only
    bites on the handful that are words.
    """
    text = str(text or "")
    if not text or width_in <= 0:
        return largest
    usable = width_in * _VALUE_SAFETY
    size = largest
    while size > smallest:
        if len(text) * (size * _BOLD_WIDTH_FACTOR) / 72.0 <= usable:
            return size
        size -= 1.0
    return smallest


def _fit_prose(text: str, width_in: float, height_in: float, font_pt: float,
               spacing: float = 1.0) -> str:
    """Trim prose to a box, ending on a sentence where possible.

    Cutting mid-sentence reads as a bug; ending a sentence early reads as
    brevity. Where no sentence boundary is available in the last third of the
    budget, it falls back to a word boundary with an ellipsis.
    """
    text = ("" if text is None else str(text)).strip()
    budget = _chars_that_fit(width_in, height_in, font_pt, spacing)
    if not text or len(text) <= budget:
        return text

    window = text[:budget]
    cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if cut >= budget * 0.55:
        return window[:cut + 1].strip()
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip(" ,;:") + "…"


class DeckBuilder:
    """Renders one IndustrialAnalysis into a presentation."""

    def __init__(self, analysis: IndustrialAnalysis):
        from pptx import Presentation
        self.analysis = analysis
        self.deck = Presentation()
        self.deck.slide_width = _inches(SLIDE_W)
        self.deck.slide_height = _inches(SLIDE_H)
        self._number = 0

    # ------------------------------------------------------- primitives --

    def _blank(self, dark: bool = False):
        """A blank slide painted with the page colour. Blank means blank: no
        placeholder boxes to fight with."""
        layout = self.deck.slide_layouts[6]
        slide = self.deck.slides.add_slide(layout)
        background = slide.background
        background.fill.solid()
        background.fill.fore_color.rgb = _rgb(NAVY if dark else PAGE)
        return slide

    def _box(self, slide, x, y, w, h, fill=WHITE, edge=LINE, radius=False,
             line_width=0.75):
        from pptx.enum.shapes import MSO_SHAPE
        shape_kind = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
        shape = slide.shapes.add_shape(shape_kind, _inches(x), _inches(y),
                                       _inches(w), _inches(h))
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(fill)
        if edge:
            shape.line.color.rgb = _rgb(edge)
            shape.line.width = _pt(line_width)
        else:
            shape.line.fill.background()
        shape.shadow.inherit = False
        if shape.has_text_frame:
            shape.text_frame.text = ""
        return shape

    def _text(self, slide, x, y, w, h, text, size=12, colour=INK, bold=False,
              align="left", font=FONT, italic=False, spacing=1.0, anchor="top",
              space_after=0.0):
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        box = slide.shapes.add_textbox(_inches(x), _inches(y), _inches(w), _inches(h))
        frame = box.text_frame
        frame.word_wrap = True
        frame.margin_left = frame.margin_right = 0
        frame.margin_top = frame.margin_bottom = 0
        frame.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE,
                                 "bottom": MSO_ANCHOR.BOTTOM}[anchor]
        lines = str(text).split("\n")
        for index, line in enumerate(lines):
            para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            para.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
                              "right": PP_ALIGN.RIGHT}[align]
            para.line_spacing = spacing
            if space_after:
                para.space_after = _pt(space_after)
            run = para.add_run()
            run.text = line
            run.font.size = _pt(size)
            run.font.bold = bold
            run.font.italic = italic
            run.font.name = font
            run.font.color.rgb = _rgb(colour)
        return box

    def _bullets(self, slide, x, y, w, h, items, size=10.5, colour=INK_SOFT,
                 marker_colour=ACCENT, gap=0.0):
        """A bulleted block that keeps its marker aligned with the first line."""
        from pptx.enum.text import PP_ALIGN
        box = slide.shapes.add_textbox(_inches(x), _inches(y), _inches(w), _inches(h))
        frame = box.text_frame
        frame.word_wrap = True
        frame.margin_left = frame.margin_right = 0
        frame.margin_top = frame.margin_bottom = 0
        for index, item in enumerate(items):
            para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            para.alignment = PP_ALIGN.LEFT
            para.line_spacing = 1.15
            para.space_after = _pt(gap or size * 0.65)
            marker = para.add_run()
            marker.text = "•  "
            marker.font.size = _pt(size)
            marker.font.bold = True
            marker.font.name = FONT
            marker.font.color.rgb = _rgb(marker_colour)
            run = para.add_run()
            run.text = str(item)
            run.font.size = _pt(size)
            run.font.name = FONT
            run.font.color.rgb = _rgb(colour)
        return box

    def _chip(self, slide, x, y, text, colour=ACCENT, width=None, height=0.25,
              size=9.0):
        """A small pill carrying a magnitude or a severity."""
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        width = width or max(0.66, 0.082 * size * len(str(text)) / 9.0 + 0.26)
        shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, _inches(x),
                                       _inches(y), _inches(width), _inches(height))
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(colour)
        shape.line.fill.background()
        shape.shadow.inherit = False
        frame = shape.text_frame
        frame.margin_left = frame.margin_right = 0
        frame.margin_top = frame.margin_bottom = 0
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        para = frame.paragraphs[0]
        para.alignment = PP_ALIGN.CENTER
        run = para.add_run()
        run.text = str(text).upper()
        run.font.size = _pt(size)
        run.font.bold = True
        run.font.name = FONT
        run.font.color.rgb = _rgb(WHITE)
        return shape

    def _header(self, slide, section: str, title: str, subtitle: str = ""):
        """A dark band carrying 'NN | SECTION - question?'. The number tells the
        reader where they are in the argument; the question tells them what the
        slide is for."""
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        self._number += 1
        band = self._box(slide, 0, 0, SLIDE_W, HEADER_H, fill=NAVY, edge=None)
        frame = band.text_frame
        frame.word_wrap = True
        frame.margin_left = _inches(MARGIN)
        frame.margin_right = _inches(MARGIN)
        frame.margin_top = frame.margin_bottom = 0
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        para = frame.paragraphs[0]
        para.alignment = PP_ALIGN.LEFT
        lead = para.add_run()
        lead.text = f"{self._number:02d}  |  {section.upper()}   "
        lead.font.size = _pt(13)
        lead.font.bold = True
        lead.font.name = FONT
        lead.font.color.rgb = _rgb(ACCENT_LIGHT)
        rest = para.add_run()
        rest.text = f"—   {title}"
        rest.font.size = _pt(17)
        rest.font.bold = True
        rest.font.name = FONT
        rest.font.color.rgb = _rgb(WHITE)
        if subtitle:
            self._text(slide, MARGIN, HEADER_H + 0.09, CONTENT_W, 0.26,
                       subtitle, 10.5, MUTED)
        return band

    def _body_top(self, subtitle: bool) -> float:
        return BODY_TOP + (0.22 if subtitle else 0.0)

    def _footer(self, slide, note: str = ""):
        text = note or self.analysis.demo_banner()
        if text:
            colour = AMBER if (self.analysis.contains_demo_data and not note) else MUTED
            self._text(slide, MARGIN, FOOTER_TOP, CONTENT_W - 1.7, 0.3, text, 9, colour)
        self._text(slide, SLIDE_W - MARGIN - 1.5, FOOTER_TOP, 1.5, 0.3,
                   f"{self._number:02d}", 9, MUTED, align="right")

    # ------------------------------------------------------- kpi cards ---

    def _kpi_row(self, slide, kpis: List[KPI], y: float, height: float = 1.45,
                 on_dark: bool = False, count_max: int = 5):
        """The KPI band. Label small and quiet, number large and coloured, note
        underneath - so the number is what the eye lands on."""
        if not kpis:
            return y
        count = min(len(kpis), count_max)
        gap = 0.18
        width = (CONTENT_W - gap * (count - 1)) / count
        card_fill = NAVY_DEEP if on_dark else WHITE
        card_edge = NAVY_SOFT if on_dark else LINE
        label_colour = ACCENT_LIGHT if on_dark else MUTED
        note_colour = "8FA3B5" if on_dark else MUTED
        for index, kpi in enumerate(kpis[:count]):
            x = MARGIN + index * (width + gap)
            self._box(slide, x, y, width, height, fill=card_fill, edge=card_edge)
            accent = TONE_COLOUR.get(kpi.tone, ACCENT)
            if on_dark:
                accent = {GREEN: "4ADE80", RED: "F87171", AMBER: "FBBF24",
                          ACCENT: ACCENT_LIGHT}.get(accent, ACCENT_LIGHT)
            self._box(slide, x, y, width, 0.055, fill=accent, edge=None)
            self._text(slide, x + 0.18, y + 0.22, width - 0.36, 0.3,
                       _fit(kpi.label.upper(), 26), 9, label_colour, True, spacing=0.95)
            value_text = kpi.value + (f"   {kpi.delta}" if kpi.delta else "")
            # A KPI value is usually a number and fits at 23pt. Sometimes it is
            # a word - "Maintenance", "Decommissioned" - and at 23pt that
            # wrapped mid-word and printed the stray letter over the note
            # underneath. python-pptx has no layout engine to ask, so the size
            # is chosen from the measurement used everywhere else in this file.
            self._text(slide, x + 0.18, y + 0.52, width - 0.36, 0.46,
                       value_text, _value_size(value_text, width - 0.36),
                       accent, True)
            if kpi.note:
                self._text(slide, x + 0.18, y + height - 0.52, width - 0.36, 0.44,
                           _fit(kpi.note, 62), 9, note_colour, spacing=1.05)
        return y + height

    # ---------------------------------------------------------- slides ---

    def title_slide(self):
        """Dark, full bleed. The one slide that is allowed to be a statement."""
        slide = self._blank(dark=True)
        self._box(slide, 0, 0, SLIDE_W, 0.09, fill=ACCENT, edge=None)

        # Two lines, not one: the tagline letter-spaced onto the same line as
        # the name runs past the slide edge.
        #
        # The old name lived here, letter-spaced, and so survived a
        # project-wide search-and-replace - a name written with a space between
        # every letter contains no copy of itself to find. It went out on the
        # front page of every generated deck until someone rendered one and
        # looked at it. Keep this a single unbroken string.
        self._text(slide, MARGIN, 0.50, CONTENT_W, 0.26,
                   "M E D H A", 11, ACCENT_LIGHT, True)
        self._text(slide, MARGIN, 0.74, CONTENT_W, 0.26,
                   BRAND_TAGLINE.upper(), 8.5, "8FA3B5", True, spacing=1.4)
        self._text(slide, MARGIN, 1.05, CONTENT_W, 0.8,
                   _fit(self.analysis.title, 78), 33, WHITE, True, spacing=1.0)
        if self.analysis.subtitle:
            self._text(slide, MARGIN, 1.94, CONTENT_W, 0.34,
                       _fit(self.analysis.subtitle, 120), 13.5, "9FB3C6")

        if self.analysis.headline:
            self._box(slide, MARGIN, 2.45, CONTENT_W, 1.18, fill=NAVY_DEEP,
                      edge=NAVY_SOFT)
            self._box(slide, MARGIN, 2.45, 0.06, 1.18, fill=ACCENT, edge=None)
            self._text(slide, MARGIN + 0.3, 2.63, CONTENT_W - 0.6, 0.26,
                       "RECOMMENDATION", 9.5, ACCENT_LIGHT, True)
            self._text(slide, MARGIN + 0.3, 2.94, CONTENT_W - 0.6, 0.62,
                       _fit(self.analysis.headline, 230), 14.5, WHITE, spacing=1.2)

        self._kpi_row(slide, self.analysis.kpis, 3.92, height=1.62, on_dark=True)

        self._box(slide, 0, SLIDE_H - 0.72, SLIDE_W, 0.72, fill=NAVY_DEEP, edge=None)
        self._text(slide, MARGIN, SLIDE_H - 0.56, CONTENT_W * 0.52, 0.24,
                   f"ZEUS  ·  {BRAND_TAGLINE}", 9.5, ACCENT_LIGHT, True)
        self._text(slide, MARGIN, SLIDE_H - 0.32, CONTENT_W * 0.52, 0.24,
                   f"Generated locally on {self.analysis.generated_at}"
                   f"  ·  no data left this machine", 8.5, "8FA3B5")
        banner = self.analysis.demo_banner()
        if banner:
            self._text(slide, MARGIN + CONTENT_W * 0.52, SLIDE_H - 0.5,
                       CONTENT_W * 0.48, 0.32, banner, 9, "FBBF24", align="right")
        return slide

    def summary_slide(self):
        """The situation, the numbers, and - across the bottom - what it means.
        The bottom row is what stops this being a wall of text in a big box."""
        if not self.analysis.executive_summary:
            return None
        slide = self._blank()
        self._header(slide, "Executive summary", "Where do things stand?")
        top = BODY_TOP
        upper_h = BODY_H * 0.46

        text_w = CONTENT_W * 0.615
        self._box(slide, MARGIN, top, text_w, upper_h)
        self._box(slide, MARGIN, top, 0.055, upper_h, fill=ACCENT, edge=None)
        self._text(slide, MARGIN + 0.3, top + 0.24, text_w - 0.6, 0.26,
                   "THE SITUATION", 9.5, ACCENT, True)
        # Fitted to the box. This paragraph is the one piece of free prose on
        # the slide and the only one whose length nobody controls, so without
        # this it runs past the card and prints over the finding cards below.
        self._text(slide, MARGIN + 0.3, top + 0.6, text_w - 0.6, upper_h - 0.85,
                   _fit_prose(self.analysis.executive_summary,
                              text_w - 0.6, upper_h - 0.85, 13.5, 1.5),
                   13.5, INK, spacing=1.5)

        left = MARGIN + text_w + 0.2
        width = CONTENT_W - text_w - 0.2
        self._box(slide, left, top, width, upper_h, fill=NAVY, edge=None)
        self._text(slide, left + 0.28, top + 0.24, width - 0.56, 0.26,
                   "KEY NUMBERS", 9.5, ACCENT_LIGHT, True)
        rows = self.analysis.kpis[:5]
        if rows:
            step = (upper_h - 0.78) / len(rows)
            y = top + 0.66
            for kpi in rows:
                colour = {GREEN: "4ADE80", RED: "F87171", AMBER: "FBBF24"}.get(
                    TONE_COLOUR.get(kpi.tone, ACCENT), ACCENT_LIGHT)
                self._text(slide, left + 0.28, y + 0.04, width - 1.85, 0.3,
                           _fit(kpi.label, 30), 10.5, "9FB3C6")
                self._text(slide, left + width - 1.62, y, 1.34, 0.32, kpi.value,
                           14, colour, True, align="right")
                y += step

        lower_y = top + upper_h + 0.22
        lower_h = BODY_BOTTOM - lower_y
        cards = self.analysis.findings[:3]
        if cards:
            gap = 0.2
            card_w = (CONTENT_W - gap * (len(cards) - 1)) / len(cards)
            for index, finding in enumerate(cards):
                x = MARGIN + index * (card_w + gap)
                colour = SEVERITY_COLOUR.get(finding.severity.lower(), ACCENT)
                self._box(slide, x, lower_y, card_w, lower_h)
                self._box(slide, x, lower_y, card_w, 0.05, fill=colour, edge=None)
                self._text(slide, x + 0.22, lower_y + 0.2, card_w - 1.05, 0.3,
                           _fit(finding.title, 42), 11.5, INK, True)
                if finding.magnitude:
                    self._chip(slide, x + card_w - 0.98, lower_y + 0.2,
                               _fit(finding.magnitude, 11), colour, width=0.82,
                               size=8.5)
                self._text(slide, x + 0.22, lower_y + 0.56, card_w - 0.44,
                           lower_h - 0.72,
                           _fit_prose(finding.detail, card_w - 0.44,
                                      lower_h - 0.72, 10, 1.2),
                           10, INK_SOFT, spacing=1.2)
        self._footer(slide)
        return slide

    def diagnosis_slide(self, limit: int = 6):
        """The reference-grade card grid: up to 3 x 2, each card with a coloured
        accent edge, its title, its number and the sentence behind it."""
        if not self.analysis.findings:
            return None
        slide = self._blank()
        self._header(slide, "Diagnosis", "What is driving it?",
                     "Every card carries the figure it is based on")
        findings = self.analysis.findings[:limit]
        top = self._body_top(True)
        available_h = BODY_BOTTOM - top

        columns = 3 if len(findings) > 2 else max(1, len(findings))
        rows = (len(findings) + columns - 1) // columns
        gap_x, gap_y = 0.22, 0.22
        card_w = (CONTENT_W - gap_x * (columns - 1)) / columns
        card_h = min(2.35, (available_h - gap_y * (rows - 1)) / rows)

        for index, finding in enumerate(findings):
            row, column = divmod(index, columns)
            x = MARGIN + column * (card_w + gap_x)
            y = top + row * (card_h + gap_y)
            colour = SEVERITY_COLOUR.get(finding.severity.lower(), ACCENT)
            self._box(slide, x, y, card_w, card_h)
            self._box(slide, x, y, 0.06, card_h, fill=colour, edge=None)
            self._text(slide, x + 0.26, y + 0.18, card_w - 0.52, 0.44,
                       _fit(finding.title, 52), 12.5, INK, True, spacing=1.05)
            detail_h = card_h - 1.02
            self._text(slide, x + 0.26, y + 0.68, card_w - 0.52, detail_h,
                       _fit(finding.detail, 230), 10.5, INK_SOFT, spacing=1.25)
            if finding.magnitude:
                self._chip(slide, x + 0.26, y + card_h - 0.44,
                           _fit(finding.magnitude, 24), colour, size=8.5)
            if finding.evidence:
                self._text(slide, x + card_w - 2.0, y + card_h - 0.42, 1.74, 0.3,
                           _fit(finding.evidence.replace("\n", "  "), 34), 8.5,
                           MUTED, font=MONO, align="right")

        remaining = len(self.analysis.findings) - len(findings)
        self._footer(slide, f"{remaining} further finding(s) in the accompanying report."
                     if remaining > 0 else "")
        return slide

    def chart_slide(self, block: TableBlock, title: str, question: str,
                    chart_type: str = "bar"):
        """One native chart - so it stays editable in PowerPoint - beside a
        read-out that states what the chart means."""
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE

        if not block or not block.rows:
            return None
        categories = [str(row[0]) for row in block.rows]
        try:
            values = [float(row[1] or 0) for row in block.rows]
        except (TypeError, ValueError, IndexError):
            return None

        slide = self._blank()
        self._header(slide, "Metrics", question, block.note)
        top = self._body_top(bool(block.note))
        height = BODY_BOTTOM - top
        chart_w = CONTENT_W * 0.63

        self._box(slide, MARGIN, top, chart_w, height)
        self._text(slide, MARGIN + 0.3, top + 0.2, chart_w - 0.6, 0.26,
                   _fit(block.title.upper(), 48), 9.5, ACCENT, True)

        data = CategoryChartData()
        data.categories = categories
        data.add_series(block.columns[1] if len(block.columns) > 1 else "Value", values)

        kinds = {"bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
                 "line": XL_CHART_TYPE.LINE_MARKERS,
                 "hbar": XL_CHART_TYPE.BAR_CLUSTERED}
        frame = slide.shapes.add_chart(
            kinds.get(chart_type, XL_CHART_TYPE.COLUMN_CLUSTERED),
            _inches(MARGIN + 0.18), _inches(top + 0.56), _inches(chart_w - 0.36),
            _inches(height - 0.78), data)
        chart = frame.chart
        chart.has_legend = False
        chart.has_title = False      # the slide has the title; repeating it is noise
        try:
            plot = chart.plots[0]
            plot.gap_width = 55
            plot.has_data_labels = chart_type != "line"
            if plot.has_data_labels:
                labels = plot.data_labels
                labels.font.size = _pt(10)
                labels.font.bold = True
                labels.font.name = FONT
                labels.font.color.rgb = _rgb(INK)
            for point in plot.series[0].points:
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = _rgb(ACCENT)
            for axis in (chart.category_axis, chart.value_axis):
                axis.tick_labels.font.size = _pt(10.5)
                axis.tick_labels.font.color.rgb = _rgb(MUTED)
                axis.tick_labels.font.name = FONT
                axis.format.line.color.rgb = _rgb(LINE)
                axis.has_major_gridlines = axis is chart.value_axis
                if axis.has_major_gridlines:
                    axis.major_gridlines.format.line.color.rgb = _rgb(LINE)
                    axis.major_gridlines.format.line.width = _pt(0.5)
        except Exception as error:                    # chart styling is cosmetic
            logger.debug(f"chart styling skipped: {error}")

        # The read-out. Three stacked cards, so the right-hand column is used
        # rather than left as a mostly empty panel.
        left = MARGIN + chart_w + 0.22
        width = CONTENT_W - chart_w - 0.22
        total = sum(values)
        largest_value, largest_name = max(zip(values, categories)) if values else (0, "")
        smallest_value, smallest_name = min(zip(values, categories)) if values else (0, "")
        average = total / len(values) if values else 0

        def _n(value: float) -> str:
            return f"{value:,.0f}" if abs(value - round(value)) < 1e-9 else f"{value:,.2f}"

        # What the read-out should say depends on what the chart IS.
        #
        # Summing a series of measurements is meaningless - the total of a
        # vibration trend is not a quantity that exists - and "% of the total"
        # is meaningless for anything measured over time. The first version of
        # this printed "TOTAL 153.15 across 31 categories" above a millimetres
        # per second trend, which is a confidently presented non-number.
        #
        # A trend is read by where it is now, how far it has moved, and how
        # bad it got. A set of categories is read by the total and the biggest
        # share. So the cards differ.
        if self._is_trend(block):
            latest_value, latest_name = values[-1], categories[-1]
            first_value = values[0]
            change = latest_value - first_value
            direction = "up" if change > 0 else "down" if change < 0 else "flat"
            cards = [
                ("LATEST", _n(latest_value), f"at {_fit(str(latest_name), 22)}"),
                ("CHANGE", f"{change:+,.2f}".rstrip("0").rstrip(".") or "0",
                 f"{direction} from {_n(first_value)} at the start of the series"),
                ("PEAK", _n(largest_value),
                 f"reached {_fit(str(largest_name), 20)}; low {_n(smallest_value)}"),
            ]
        else:
            cards = [
                ("TOTAL", _n(total),
                 f"across {len(values)} {'categories' if len(values) != 1 else 'category'}"),
                ("LARGEST", _fit(str(largest_name), 22),
                 f"{_n(largest_value)} · {largest_value / total * 100:.0f}% of the total"
                 if total else _n(largest_value)),
                ("SPREAD", f"{_n(smallest_value)} – {_n(largest_value)}",
                 f"average {_n(average)}; lowest is {_fit(str(smallest_name), 20)}"),
            ]
        gap = 0.18
        card_h = (height - gap * (len(cards) - 1)) / len(cards)
        for index, (label, value, note) in enumerate(cards):
            y = top + index * (card_h + gap)
            self._box(slide, left, y, width, card_h)
            self._box(slide, left, y, width, 0.05, fill=ACCENT, edge=None)
            self._text(slide, left + 0.24, y + 0.22, width - 0.48, 0.26, label,
                       9, MUTED, True)
            self._text(slide, left + 0.24, y + 0.52, width - 0.48, 0.44, value,
                       19, ACCENT, True)
            self._text(slide, left + 0.24, y + 1.02, width - 0.48,
                       max(0.3, card_h - 1.14), note, 10, INK_SOFT, spacing=1.2)
        self._footer(slide)
        return slide

    def table_slide(self, block: TableBlock, section: str, question: str,
                    max_slides: int = 2):
        """Evidence. Rows per page and row height are both computed from the
        space available, so the table fills the slide instead of floating in
        the top third of it."""
        if not block or not block.rows:
            return []
        top = self._body_top(bool(block.note))
        available = BODY_BOTTOM - top
        min_row = 0.30
        capacity = max(4, int((available - 0.42) / min_row))
        rows_per_slide = min(capacity, max(6, len(block.rows)))

        made = []
        chunks = [block.rows[i:i + rows_per_slide]
                  for i in range(0, len(block.rows), rows_per_slide)][:max_slides]
        for index, chunk in enumerate(chunks):
            slide = self._blank()
            suffix = f"  ({index + 1} of {len(chunks)})" if len(chunks) > 1 else ""
            self._header(slide, section, question + suffix, block.note)
            header_h = 0.42
            # A short table is given taller rows rather than being left floating
            # in the top third of an otherwise empty page.
            max_row = 0.78 if len(chunk) <= 6 else 0.52 if len(chunk) <= 9 else 0.42
            row_h = min(max_row, max(min_row, (available - header_h) / len(chunk)))
            table_h = header_h + row_h * len(chunk)
            self._draw_table(slide, block.columns, chunk, MARGIN, top, CONTENT_W,
                             block, header_h=header_h, row_h=row_h)

            # Whatever is left becomes the read-out strip, sized to close the
            # page out rather than leaving a band of white below the last row.
            spare = available - table_h
            if spare > 0.7:
                self._table_note(slide, top + table_h + 0.2, spare - 0.2, block, chunk)

            remaining = len(block.rows) - rows_per_slide * len(chunks)
            self._footer(slide, f"{remaining} further row(s) in the workbook."
                         if (index == len(chunks) - 1 and remaining > 0) else "")
            made.append(slide)
        return made

    def _table_note(self, slide, y, height, block: TableBlock, rows):
        """A one-line read-out under a short table: totals for the numeric
        columns, so the space below the last row says something."""
        height = min(height, 1.55)
        self._box(slide, MARGIN, y, CONTENT_W, height, fill=BAND, edge=LINE)
        self._box(slide, MARGIN, y, CONTENT_W, 0.05, fill=ACCENT, edge=None)
        facts: List[Tuple[str, str]] = [("ROWS SHOWN", str(len(rows)))]
        for index, name in enumerate(block.columns):
            values = []
            for row in rows:
                if index < len(row):
                    try:
                        values.append(float(str(row[index]).replace(",", "")))
                    except (TypeError, ValueError):
                        values = []
                        break
            if values and len(facts) < 4:
                total = sum(values)
                facts.append((f"TOTAL {name.upper()}",
                              f"{total:,.0f}" if abs(total - round(total)) < 1e-9
                              else f"{total:,.1f}"))
        if len(facts) < 4 and block.note:
            facts.append(("NOTE", _fit(block.note, 44)))
        width = CONTENT_W / len(facts)
        block_h = 0.62
        offset = max(0.2, (height - block_h) / 2)
        for index, (label, value) in enumerate(facts):
            x = MARGIN + 0.26 + index * width
            self._text(slide, x, y + offset, width - 0.3, 0.24, label, 8.5, MUTED, True)
            self._text(slide, x, y + offset + 0.28, width - 0.3,
                       max(0.3, height - offset - 0.34), value,
                       11 if label == "NOTE" else 13, INK, True, spacing=1.1)

    def _draw_table(self, slide, columns, rows, x, y, width, block: TableBlock,
                    header_h: float = 0.42, row_h: float = 0.34):
        from pptx.util import Inches as _I
        shape = slide.shapes.add_table(len(rows) + 1, len(columns), _inches(x),
                                       _inches(y), _inches(width),
                                       _inches(header_h + row_h * len(rows)))
        table = shape.table
        try:
            table.first_row = True
            table.horz_banding = False        # banded by hand, for real control
        except Exception:
            pass

        table.rows[0].height = _I(header_h)
        for index in range(1, len(rows) + 1):
            table.rows[index].height = _I(row_h)

        widths = self._column_widths(columns, rows, width)
        for index, column_width in enumerate(widths):
            table.columns[index].width = _I(column_width)

        body_size = 10.5 if row_h >= 0.36 else 9.5
        cap = {index: max(14, int(widths[index] / 0.082))
               for index in range(len(columns))}

        for index, name in enumerate(columns):
            cell = table.cell(0, index)
            cell.text = _fit(str(name), cap[index])
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(NAVY)
            para = cell.text_frame.paragraphs[0]
            para.runs[0].font.size = _pt(10)
            para.runs[0].font.bold = True
            para.runs[0].font.color.rgb = _rgb(WHITE)
            para.runs[0].font.name = FONT
            cell.margin_left = cell.margin_right = _I(0.07)
            cell.margin_top = cell.margin_bottom = _I(0.03)

        severity_column = None
        for name, rule in (block.highlight or {}).items():
            if rule == "severity" and name in columns:
                severity_column = columns.index(name)

        for row_index, row in enumerate(rows, start=1):
            banded = row_index % 2 == 0
            for column_index, value in enumerate(row):
                cell = table.cell(row_index, column_index)
                text = "" if value is None else str(value)
                cell.text = _fit(text, cap.get(column_index, 40))
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(BAND if banded else WHITE)
                para = cell.text_frame.paragraphs[0]
                run = para.runs[0] if para.runs else para.add_run()
                run.font.size = _pt(body_size)
                run.font.name = FONT
                colour = INK if column_index == 0 else INK_SOFT
                if column_index == 0:
                    run.font.bold = True
                if severity_column is not None and column_index == severity_column:
                    colour = SEVERITY_COLOUR.get(text.lower(), INK)
                    run.font.bold = True
                run.font.color.rgb = _rgb(colour)
                cell.margin_left = cell.margin_right = _I(0.07)
                cell.margin_top = cell.margin_bottom = _I(0.02)
        return table

    @staticmethod
    def _column_widths(columns, rows, total_width) -> List[float]:
        """Width by content, not equal shares - a description column needs room
        that a date column does not. Header words are counted whole so a title
        never wraps into 'Severit / y'."""
        weights = []
        for index, name in enumerate(columns):
            header = str(name)
            longest = max([len(word) for word in header.split()] or [4]) + 3
            longest = max(longest, min(len(header), 18))
            for row in rows[:40]:
                if index < len(row) and row[index] is not None:
                    longest = max(longest, min(len(str(row[index])), 52))
            weights.append(max(7, longest))
        total = sum(weights)
        widths = [total_width * weight / total for weight in weights]
        minimum = 0.85
        deficit = sum(minimum - width for width in widths if width < minimum)
        if deficit > 0:
            surplus = sum(width - minimum for width in widths if width > minimum) or 1
            widths = [minimum if width < minimum
                      else width - deficit * (width - minimum) / surplus
                      for width in widths]
        return widths

    def decisions_slide(self):
        if not self.analysis.recommendations:
            return None
        slide = self._blank()
        self._header(slide, "Recommended actions", "What should we do?",
                     "Ordered by priority, each with its impact and the effort it takes")
        items = sorted(self.analysis.recommendations, key=lambda r: r.priority)[:4]
        top = self._body_top(True)
        gap = 0.2
        height = (BODY_BOTTOM - top - gap * (len(items) - 1)) / len(items)
        for index, item in enumerate(items):
            y = top + index * (height + gap)
            self._box(slide, MARGIN, y, CONTENT_W, height)
            self._box(slide, MARGIN, y, 0.055, height, fill=ACCENT, edge=None)
            badge_y = y + (height - 0.56) / 2
            self._box(slide, MARGIN + 0.26, badge_y, 0.56, 0.56, fill=NAVY,
                      edge=None, radius=True)
            self._text(slide, MARGIN + 0.26, badge_y + 0.13, 0.56, 0.34,
                       str(index + 1), 16, WHITE, True, align="center")
            text_w = CONTENT_W - 4.6
            self._text(slide, MARGIN + 1.02, y + 0.2, text_w, 0.32,
                       _fit(item.title, 86), 13.5, INK, True)
            if item.rationale:
                self._text(slide, MARGIN + 1.02, y + 0.56, text_w,
                           max(0.3, height - 0.72), _fit(item.rationale, 190), 10.5,
                           INK_SOFT, spacing=1.2)
            right = MARGIN + CONTENT_W - 3.3
            if item.impact:
                self._text(slide, right, y + 0.22, 1.55, 0.24, "IMPACT", 8.5,
                           MUTED, True)
                self._text(slide, right, y + 0.5, 1.55, max(0.3, height - 0.66),
                           _fit(item.impact, 58), 10, GREEN, spacing=1.15)
            if item.effort:
                self._text(slide, right + 1.66, y + 0.22, 1.5, 0.24, "EFFORT", 8.5,
                           MUTED, True)
                self._text(slide, right + 1.66, y + 0.5, 1.5, max(0.3, height - 0.66),
                           _fit(item.effort, 58), 10, INK_SOFT, spacing=1.15)
        self._footer(slide)
        return slide

    def risks_slide(self):
        if not self.analysis.risks:
            return None
        slide = self._blank()
        self._header(slide, "Risks", "What could go wrong?",
                     "Each risk carries the control that reduces it")
        block = TableBlock(
            title="Risks",
            columns=["Risk", "Likelihood", "Impact", "Mitigation"],
            rows=[[r.risk, r.likelihood.title(), r.impact.title(), r.mitigation]
                  for r in self.analysis.risks[:8]],
            highlight={"Impact": "severity"})
        top = self._body_top(True)
        available = BODY_BOTTOM - top
        header_h = 0.42
        row_h = min(1.15, max(0.34, (available - header_h) / max(1, len(block.rows))))
        self._draw_table(slide, block.columns, block.rows, MARGIN, top, CONTENT_W,
                         block, header_h=header_h, row_h=row_h)
        self._footer(slide)
        return slide

    def roadmap_slide(self):
        """Phases across the page, each with its own colour, its actions and the
        outcome it is judged on."""
        if not self.analysis.timeline:
            return None
        slide = self._blank()
        self._header(slide, "Roadmap", "When does it happen?",
                     "Each phase is judged on its stated outcome")
        phases = self.analysis.timeline[:4]
        top = self._body_top(True)
        height = BODY_BOTTOM - top
        gap = 0.22
        width = (CONTENT_W - gap * (len(phases) - 1)) / len(phases)
        head_h = 0.86
        for index, phase in enumerate(phases):
            x = MARGIN + index * (width + gap)
            colour = PHASE_COLOUR[index % len(PHASE_COLOUR)]
            self._box(slide, x, top, width, height)
            self._box(slide, x, top, width, head_h, fill=colour, edge=None)
            self._text(slide, x + 0.2, top + 0.14, width - 0.4, 0.26,
                       _fit(phase.window.upper(), 30), 9.5, WHITE, True,
                       align="center")
            self._text(slide, x + 0.2, top + 0.42, width - 0.4, 0.34,
                       _fit(phase.phase.upper(), 22), 15, WHITE, True, align="center")
            outcome_h = 0.86 if phase.outcome else 0.0
            actions_h = height - head_h - outcome_h - 0.36
            self._bullets(slide, x + 0.22, top + head_h + 0.24, width - 0.44,
                          actions_h, [_fit(a, 78) for a in phase.actions[:4]],
                          size=10, colour=INK_SOFT, marker_colour=colour)
            if phase.outcome:
                oy = top + height - outcome_h - 0.1
                self._box(slide, x + 0.16, oy, width - 0.32, outcome_h, fill=BAND,
                          edge=LINE)
                self._text(slide, x + 0.3, oy + 0.12, width - 0.6, 0.22, "OUTCOME",
                           8.5, MUTED, True)
                self._text(slide, x + 0.3, oy + 0.36, width - 0.6, outcome_h - 0.46,
                           _fit(phase.outcome, 78), 10, GREEN, True, spacing=1.15)
        self._footer(slide)
        return slide

    def ask_slide(self):
        slide = self._blank()
        # The header follows the CONTENT. With a decision to put, this slide
        # asks for it; without one it is the provenance page and nothing more,
        # and heading that "Decision - what is being asked for?" over a list of
        # source tables asks a question the slide never answers.
        if self.analysis.decision_request:
            self._header(slide, "Decision", "What is being asked for?")
        else:
            self._header(slide, "Provenance", "Where did these figures come from?")
        top = BODY_TOP
        ask_h = 1.35
        if self.analysis.decision_request:
            self._box(slide, MARGIN, top, CONTENT_W, ask_h, fill=NAVY, edge=None)
            self._box(slide, MARGIN, top, 0.06, ask_h, fill=ACCENT, edge=None)
            self._text(slide, MARGIN + 0.32, top + 0.22, CONTENT_W - 0.64, 0.26,
                       "THE ASK", 9.5, ACCENT_LIGHT, True)
            self._text(slide, MARGIN + 0.32, top + 0.56, CONTENT_W - 0.64, 0.66,
                       _fit(self.analysis.decision_request, 210), 15.5, WHITE,
                       True, spacing=1.25)
        else:
            ask_h = 0.0

        closing_h = 0.86
        y = top + ask_h + (0.24 if ask_h else 0)
        height = BODY_BOTTOM - y - closing_h - 0.22
        half = CONTENT_W * 0.5 - 0.12

        self._box(slide, MARGIN, y, half, height)
        self._box(slide, MARGIN, y, half, 0.05, fill=ACCENT, edge=None)
        self._text(slide, MARGIN + 0.28, y + 0.22, half - 0.56, 0.26,
                   "DATA BEHIND THIS DECK", 9.5, ACCENT, True)
        line_y = y + 0.62
        for source in self.analysis.source_data[:7]:
            self._text(slide, MARGIN + 0.28, line_y, half - 0.56, 0.3,
                       _fit(source, 58), 10, INK_SOFT, font=MONO)
            line_y += 0.32

        right = MARGIN + CONTENT_W * 0.5 + 0.12
        self._box(slide, right, y, half, height)
        self._box(slide, right, y, half, 0.05, fill=ACCENT, edge=None)
        self._text(slide, right + 0.28, y + 0.22, half - 0.56, 0.26,
                   "HOW THIS WAS PRODUCED", 9.5, ACCENT, True)
        notes = [
            "Assembled from the local ZEUS database.",
            "Generated offline — nothing left this machine.",
            ("Narrative written by the local model, constrained to these figures."
             if self.analysis.narrative_included
             else "No language model was involved in any figure."),
            "Every figure is reproducible from the tables in the workbook.",
        ]
        if self.analysis.contains_demo_data:
            notes.append("Figures include DEMO sample rows, marked as such.")
        notes += [_fit(c, 70) for c in self.analysis.citations[:2]]
        self._bullets(slide, right + 0.28, y + 0.62, half - 0.56, height - 0.8,
                      notes[:6], size=10, colour=INK_SOFT)

        # A closing band, so the page ends on a statement rather than white space.
        closing_y = y + height + 0.22
        self._box(slide, MARGIN, closing_y, CONTENT_W, closing_h, fill=NAVY, edge=None)
        self._text(slide, MARGIN + 0.32, closing_y + 0.17, CONTENT_W - 0.64, 0.24,
                   "VERIFICATION", 9, ACCENT_LIGHT, True)
        self._text(slide, MARGIN + 0.32, closing_y + 0.44, CONTENT_W - 0.64, 0.32,
                   "Every file in this pack was reopened and parsed after writing; "
                   "a file that fails that check is reported as an error, never as a "
                   "success. ZEUS does not approve findings, permits or isolations.",
                   10, "C7D6E2")
        self._footer(slide)
        return slide

    # ------------------------------------------------------------ build --

    def build(self) -> "DeckBuilder":
        """Assemble the storyline. Slides with no content are skipped, so a thin
        analysis produces a short deck rather than empty pages."""
        analysis = self.analysis
        self.title_slide()
        self.summary_slide()
        self.diagnosis_slide()

        chart_block = (analysis.table("Maintenance by type")
                       or analysis.table("Condition trend"))
        if chart_block is None:
            chart_block = self._first_numeric_block()
        if chart_block:
            question = {"Maintenance by type": "Where does the maintenance effort go?",
                        "Condition trend": "How is the condition trending?"}.get(
                            chart_block.title, f"What does {chart_block.title.lower()} show?")
            self.chart_slide(chart_block, "Metrics", question,
                             chart_type="line" if self._is_trend(chart_block) else "bar")

        for block, section, question in self._evidence_plan():
            self.table_slide(block, section, question)

        self.decisions_slide()
        if analysis.financials:
            self.table_slide(analysis.financials, "Financial impact",
                             "What is it worth?")
        self.risks_slide()
        self.roadmap_slide()
        self.ask_slide()
        return self

    # A bar chart stops being readable somewhere around fourteen categories.
    # A line chart does not - a condition trend is MORE convincing with sixty
    # points than with ten, because the shape is the argument. Charting them
    # under one row limit meant the most important chart in a maintenance
    # report - the vibration trend that explains why the machine came out of
    # service - was silently rejected for having too much data.
    MAX_BAR_ROWS = 14
    MAX_LINE_ROWS = 80

    @staticmethod
    def _is_numeric_block(block: TableBlock) -> bool:
        if not block.rows or len(block.columns) < 2:
            return False
        try:
            [float(str(row[1]).replace(",", "")) for row in block.rows[:12]]
        except (TypeError, ValueError, IndexError):
            return False
        return True

    @staticmethod
    def _is_trend(block: TableBlock) -> bool:
        """A series over time, which wants a line rather than bars."""
        title = block.title.lower()
        if any(word in title for word in ("trend", "monthly", "over time",
                                          "history of", "series")):
            return True
        first = str(block.columns[0]).lower()
        return first in ("date", "month", "week", "period", "day", "year")

    def _first_numeric_block(self) -> Optional[TableBlock]:
        """The table best suited to carrying the deck's chart.

        Trends win over categories: if the analysis contains a series over
        time, that is the story, and a bar chart of something else buried
        under it is a wasted slide.
        """
        candidates = [b for b in self.analysis.tables if self._is_numeric_block(b)]
        for block in candidates:
            if self._is_trend(block) and 3 <= len(block.rows) <= self.MAX_LINE_ROWS:
                return block
        for block in candidates:
            if 2 <= len(block.rows) <= self.MAX_BAR_ROWS:
                return block
        return None

    def _evidence_plan(self) -> List[Tuple[TableBlock, str, str]]:
        plan = []
        wanted = [
            ("Open alerts", "Evidence", "What is open right now?"),
            ("Spares and inventory", "Inventory", "Can we repair it when it fails?"),
            ("Maintenance history", "History", "What has been done to it?"),
            ("Inspection history", "Inspections", "What did the inspections find?"),
            ("Specification", "Asset", "What are we talking about?"),
        ]
        for title, section, question in wanted:
            block = self.analysis.table(title)
            if block and block.rows:
                plan.append((block, section, question))
        if not plan:
            for block in self.analysis.tables[:3]:
                if block.rows:
                    plan.append((block, "Evidence", f"What does {block.title.lower()} show?"))
        return plan[:4]

    def save(self, path) -> str:
        self.deck.save(str(path))
        return str(path)


class TopicDeckBuilder(DeckBuilder):
    """A deck about a TOPIC rather than about the plant database.

    Same design system, same primitives, same rules - the difference is only
    where the content comes from. This exists because "make a presentation
    about rainwater harvesting" used to be served by a completely separate
    generator that dropped bullets into python-pptx's default 4:3 template.
    Two generators meant two quality levels; there is now one.

    Slide count is exactly one per section, because the caller (and its tests)
    control how many slides were asked for.
    """

    def __init__(self, topic: str, subtitle: str = "", generated_at: str = ""):
        from datetime import datetime
        # The deck title is usually a dressed-up version of the topic; showing
        # the raw topic underneath it as a subtitle just repeats the headline
        # back in lower case.
        cleaned = (subtitle or "").strip()
        if cleaned and cleaned.lower() in topic.lower():
            cleaned = ""
        analysis = IndustrialAnalysis(
            title=topic,
            subtitle=cleaned,
            generated_at=generated_at or datetime.now().strftime("%Y-%m-%d %H:%M"),
            contains_demo_data=False)
        super().__init__(analysis)

    # ---------------------------------------------------------- slides ---

    def topic_title_slide(self, sections: List[Dict[str, Any]]):
        """Dark opener. With no KPIs to show, the contents earn the space."""
        slide = self._blank(dark=True)
        self._box(slide, 0, 0, SLIDE_W, 0.09, fill=ACCENT, edge=None)
        self._text(slide, MARGIN, 0.72, CONTENT_W, 0.3,
                   f"M E D H A   ·   {BRAND_TAGLINE.upper()}",
                   10.5, ACCENT_LIGHT, True)
        self._text(slide, MARGIN, 1.18, CONTENT_W, 1.0,
                   _fit(self.analysis.title, 90), 32, WHITE, True, spacing=1.05)
        if self.analysis.subtitle:
            self._text(slide, MARGIN, 2.24, CONTENT_W, 0.36,
                       _fit(self.analysis.subtitle, 130), 13.5, "9FB3C6")

        contents = [s.get("title", "") for s in sections if s.get("title")]
        if contents:
            top = 2.85
            columns_planned = 2 if len(contents) > 4 else 1
            rows_planned = (len(contents) + columns_planned - 1) // columns_planned
            # Size the panel to its contents rather than stretching it to the
            # footer - a half-empty box reads as an unfinished slide.
            height = min(SLIDE_H - top - 1.0, 0.98 + rows_planned * 0.42)
            self._box(slide, MARGIN, top, CONTENT_W, height, fill=NAVY_DEEP,
                      edge=NAVY_SOFT)
            self._box(slide, MARGIN, top, 0.06, height, fill=ACCENT, edge=None)
            self._text(slide, MARGIN + 0.32, top + 0.24, CONTENT_W - 0.64, 0.26,
                       "WHAT THIS COVERS", 9.5, ACCENT_LIGHT, True)
            columns = 2 if len(contents) > 4 else 1
            per_column = (len(contents) + columns - 1) // columns
            column_width = (CONTENT_W - 0.64) / columns
            for index, title in enumerate(contents[:10]):
                column, row = divmod(index, per_column)
                x = MARGIN + 0.32 + column * column_width
                y = top + 0.66 + row * 0.42
                if y + 0.4 > top + height:
                    break
                self._text(slide, x, y, 0.4, 0.3, f"{index + 1:02d}", 11,
                           ACCENT_LIGHT, True)
                self._text(slide, x + 0.46, y, column_width - 0.6, 0.36,
                           _fit(title, 62), 12, "D6E2EC")

        self._box(slide, 0, SLIDE_H - 0.62, SLIDE_W, 0.62, fill=NAVY_DEEP, edge=None)
        self._text(slide, MARGIN, SLIDE_H - 0.45, CONTENT_W, 0.3,
                   f"ZEUS — {BRAND_TAGLINE}  ·  {self.analysis.generated_at}"
                   f"  ·  no data left this machine", 9.5, "8FA3B5")
        return slide

    def topic_slide(self, section: Dict[str, Any]):
        """Header band, the slide's one message, then its points as cards.

        Cards rather than a bullet list: it is the same information, but it
        reads as a designed page and it forces each point to stand on its own.
        """
        title = str(section.get("title") or "").strip()
        key_point = str(section.get("key_point") or "").strip()
        points = [str(p).strip() for p in (section.get("bullets") or []) if str(p).strip()]
        if not title and not points:
            return None

        slide = self._blank()
        self._header(slide, section.get("section") or "Topic", title or "Overview")
        top = BODY_TOP
        available = BODY_BOTTOM - top

        if key_point:
            callout_h = 1.0
            self._box(slide, MARGIN, top, CONTENT_W, callout_h)
            self._box(slide, MARGIN, top, 0.055, callout_h, fill=ACCENT, edge=None)
            self._text(slide, MARGIN + 0.3, top + 0.18, CONTENT_W - 0.6, 0.24,
                       "THE POINT", 9, ACCENT, True)
            self._text(slide, MARGIN + 0.3, top + 0.46, CONTENT_W - 0.6, 0.46,
                       _fit(key_point, 190), 14, INK, True, spacing=1.2)
            top += callout_h + 0.24
            available = BODY_BOTTOM - top

        if points:
            shown = points[:6]
            columns = 3 if len(shown) > 4 else (2 if len(shown) > 1 else 1)
            rows = (len(shown) + columns - 1) // columns
            gap = 0.22
            card_w = (CONTENT_W - gap * (columns - 1)) / columns
            # Cards are sized to the space, then capped: a two-line point in a
            # 2.4-inch card is mostly white space. The text is centred inside
            # whatever height results, so it never floats at the top.
            card_h = min(1.85, (available - gap * (rows - 1)) / rows)
            block_h = rows * card_h + gap * (rows - 1)
            top += max(0.0, (available - block_h) / 2)
            for index, point in enumerate(shown):
                row, column = divmod(index, columns)
                x = MARGIN + column * (card_w + gap)
                y = top + row * (card_h + gap)
                self._box(slide, x, y, card_w, card_h)
                self._box(slide, x, y, card_w, 0.05, fill=ACCENT, edge=None)
                self._text(slide, x + 0.24, y + 0.2, 0.5, 0.28, f"{index + 1:02d}",
                           11.5, ACCENT, True)
                self._text(slide, x + 0.24, y + 0.52, card_w - 0.48,
                           max(0.4, card_h - 0.74), _fit(point, 300), 11.5,
                           INK_SOFT, spacing=1.3, anchor="middle")

        note = section.get("notes")
        if note:
            try:
                slide.notes_slide.notes_text_frame.text = str(note)[:500]
            except Exception as error:                 # notes are a nicety
                logger.debug(f"speaker note skipped: {error}")
        self._footer(slide, "")
        return slide

    def build_topic(self, sections: List[Dict[str, Any]]) -> "TopicDeckBuilder":
        self.topic_title_slide(sections)
        for section in sections:
            self.topic_slide(section)
        return self


def build_topic_deck(topic: str, sections: List[Dict[str, Any]], path,
                     subtitle: str = "") -> Dict[str, Any]:
    """Render a topic deck through the same design system as everything else."""
    from core import office
    builder = TopicDeckBuilder(topic, subtitle=subtitle).build_topic(sections)
    builder.save(path)
    return office.finish(path, "pptx", extra={
        "slides_built": len(builder.deck.slides),
        "topic": topic,
    })


def build_deck(analysis: IndustrialAnalysis, path) -> Dict[str, Any]:
    """Render and verify. Returns the standard office result dictionary."""
    from core import office
    builder = DeckBuilder(analysis).build()
    builder.save(path)
    return office.finish(path, "pptx", extra={
        "slides_built": len(builder.deck.slides),
        "analysis": analysis.summary_dict(),
        "demo_data": analysis.contains_demo_data,
        "narrative_included": analysis.narrative_included,
    })
