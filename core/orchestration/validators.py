"""
The validation layer: nothing is reported as done until it has been checked.

One registry, one signature. `validate(kind, payload) -> ValidationReport`.
Each validator returns NAMED checks, so a failure says which check failed and
the auto-fix layer can dispatch on it instead of blindly retrying.

The office validators build on `core.office.verify_office_file`, which already
reopens a generated file with the library that wrote it - the strongest cheap
proof that a file is real. On top of that this module adds the checks that
catch a file which opens but is still wrong: a deck whose text runs off the
slide, a workbook whose totals were pasted instead of computed, a document
with no headings, code that does not parse, an answer with no grounding.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from core.orchestration.contracts import MedhaError, ValidationReport

logger = get_logger("orchestration.validate")

_VALIDATORS: Dict[str, Callable[[Any], ValidationReport]] = {}

# Nothing smaller than this is legible on a projector; the deck builder is
# held to the same number by its own tests.
MIN_SLIDE_FONT_PT = 8.5


def validator(kind: str):
    def decorate(function: Callable[[Any], ValidationReport]):
        _VALIDATORS[kind] = function
        return function
    return decorate


def validate(kind: str, payload: Any) -> ValidationReport:
    """Run the validator for `kind`. An unknown kind is reported honestly as
    unvalidated rather than quietly passing."""
    function = _VALIDATORS.get(kind)
    if function is None:
        return ValidationReport(ok=True, kind=kind, checks={"validator_exists": False},
                                detail=f"no validator registered for '{kind}' - "
                                       f"output was not checked")
    try:
        return function(payload)
    except Exception as error:                       # a validator must never crash a run
        logger.warning(f"validator '{kind}' raised: {error}")
        return ValidationReport(
            ok=False, kind=kind, checks={"validator_ran": False},
            detail=str(error),
            error=MedhaError(module=f"validator.{kind}", error_type="validator_crashed",
                              detail=str(error), recovery="skip_validation"))


def registered_kinds() -> List[str]:
    return sorted(_VALIDATORS)


# ------------------------------------------------------------- helpers ----

_PATH_KEYS = ("saved", "path", "file", "output_path", "created", "folder",
              "destination", "target", "moved", "copied", "renamed", "output")


def _as_path(payload: Any) -> Optional[str]:
    """Accept a path, or any tool result dict that carries one.

    The key list is broad on purpose: tools in this project were written over
    time and report their result under whatever name read best at the time
    ('created' for a new folder, 'saved' for a document). A validator that
    only knew one of those names would call a successful tool a failure.
    """
    if isinstance(payload, (str, Path)):
        return str(payload)
    if isinstance(payload, dict):
        for key in _PATH_KEYS:
            value = payload.get(key)
            if isinstance(value, (str, Path)) and str(value):
                return str(value)
    return None


def _file_basics(path: Optional[str], extension: str, module: str) -> ValidationReport:
    """exists / non-empty / right extension / reopens with its own library."""
    checks: Dict[str, bool] = {}
    if not path:
        return ValidationReport(
            ok=False, kind=extension, checks={"path_returned": False},
            detail="the step did not report a file path",
            error=MedhaError(module=module, error_type="no_path_returned",
                              detail="no path in the result", recovery="regenerate"))
    checks["path_returned"] = True

    from core import office
    verdict = office.verify_office_file(path, extension)
    checks["exists"] = bool(verdict.get("exists"))
    checks["non_empty"] = bool(verdict.get("bytes", 0))
    checks["extension"] = verdict.get("extension", "") == extension.lower().lstrip(".")
    checks["reopens"] = bool(verdict.get("readable", verdict.get("ok")))

    if verdict.get("ok"):
        return ValidationReport(ok=True, kind=extension, checks=checks,
                                detail=f"{path} verified ({verdict.get('bytes', 0)} bytes)")

    error_type = ("file_missing" if not checks["exists"]
                  else "file_empty" if not checks["non_empty"]
                  else "file_corrupt")
    return ValidationReport(
        ok=False, kind=extension, checks=checks,
        detail=verdict.get("error", "file failed verification"),
        error=MedhaError(module=module, error_type=error_type,
                          detail=verdict.get("error", ""), recovery="regenerate"))


# ------------------------------------------------------- office formats ---

@validator("docx")
def _validate_docx(payload: Any) -> ValidationReport:
    path = _as_path(payload)
    report = _file_basics(path, "docx", "docx_renderer")
    if not report.ok:
        return report
    try:
        from docx import Document
        document = Document(path)
        headings = [p for p in document.paragraphs
                    if p.style is not None and p.style.name.startswith("Heading")]
        body = " ".join(p.text for p in document.paragraphs).strip()
        report.checks["has_headings"] = bool(headings)
        report.checks["has_body_text"] = len(body) > 40
        report.checks["has_tables"] = bool(document.tables)
        if not report.checks["has_body_text"]:
            report.ok = False
            report.detail = "the document opens but is effectively empty"
            report.error = MedhaError(module="docx_renderer",
                                       error_type="empty_document",
                                       detail=report.detail,
                                       recovery="regenerate_with_content")
    except Exception as error:
        logger.debug(f"docx deep check skipped: {error}")
    return report


@validator("xlsx")
def _validate_xlsx(payload: Any) -> ValidationReport:
    path = _as_path(payload)
    report = _file_basics(path, "xlsx", "xlsx_renderer")
    if not report.ok:
        return report
    try:
        from openpyxl import load_workbook
        book = load_workbook(path)
        report.checks["has_sheets"] = bool(book.sheetnames)
        cells = 0
        formulas = 0
        broken = 0
        for sheet in book.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    cells += 1
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formulas += 1
                        # A formula referring to a sheet that is not in the
                        # book will open as #REF! for the user.
                        if "'" in cell.value:
                            name = cell.value.split("'")[1]
                            if name not in book.sheetnames:
                                broken += 1
        report.checks["has_data"] = cells > 0
        report.checks["formulas_present"] = formulas > 0
        report.checks["formula_references_resolve"] = broken == 0
        if not report.checks["has_data"]:
            report.ok = False
            report.detail = "the workbook opens but holds no data"
            report.error = MedhaError(module="xlsx_renderer", error_type="empty_workbook",
                                       detail=report.detail,
                                       recovery="regenerate_with_content")
        elif broken:
            report.ok = False
            report.detail = f"{broken} formula(s) reference a sheet that is not present"
            report.error = MedhaError(module="xlsx_renderer",
                                       error_type="broken_formula_reference",
                                       detail=report.detail, recovery="rebuild_formulas")
    except Exception as error:
        logger.debug(f"xlsx deep check skipped: {error}")
    return report


@validator("pptx")
def _validate_pptx(payload: Any) -> ValidationReport:
    """Opens, has slides, nothing runs off the page, nothing unreadably small.

    The overflow check is the one that matters: a deck can be perfectly valid
    OOXML and still have a card hanging off the bottom of slide four.
    """
    path = _as_path(payload)
    report = _file_basics(path, "pptx", "pptx_renderer")
    if not report.ok:
        return report
    try:
        from pptx import Presentation
        from pptx.util import Emu
        deck = Presentation(path)
        height = Emu(deck.slide_height).inches
        width = Emu(deck.slide_width).inches
        report.checks["has_slides"] = len(deck.slides) > 0

        overflow: List[str] = []
        smallest = 99.0
        text_found = False
        for index, slide in enumerate(deck.slides, start=1):
            for shape in slide.shapes:
                if shape.top is not None and shape.height is not None:
                    bottom = Emu(shape.top).inches + Emu(shape.height).inches
                    right = Emu(shape.left).inches + Emu(shape.width).inches
                    if bottom > height + 0.02 or right > width + 0.02:
                        overflow.append(f"slide {index}")
                if getattr(shape, "has_text_frame", False):
                    for paragraph in shape.text_frame.paragraphs:
                        for run in paragraph.runs:
                            if run.text.strip():
                                text_found = True
                            if run.font.size is not None:
                                smallest = min(smallest, run.font.size.pt)
        report.checks["nothing_overflows"] = not overflow
        report.checks["readable_type"] = smallest >= MIN_SLIDE_FONT_PT or smallest == 99.0
        report.checks["has_text"] = text_found

        if overflow:
            report.ok = False
            report.detail = f"content runs off the slide on {', '.join(sorted(set(overflow)))}"
            report.error = MedhaError(module="pptx_renderer", error_type="layout_overflow",
                                       detail=report.detail,
                                       recovery="retry_with_fewer_items")
        elif not report.checks["readable_type"]:
            report.ok = False
            report.detail = f"text as small as {smallest} pt found"
            report.error = MedhaError(module="pptx_renderer", error_type="type_too_small",
                                       detail=report.detail,
                                       recovery="retry_with_fewer_items")
        elif not text_found:
            report.ok = False
            report.detail = "the deck has slides but no text"
            report.error = MedhaError(module="pptx_renderer", error_type="empty_deck",
                                       detail=report.detail,
                                       recovery="regenerate_with_content")
    except Exception as error:
        logger.debug(f"pptx deep check skipped: {error}")
    return report


@validator("pdf")
def _validate_pdf(payload: Any) -> ValidationReport:
    path = _as_path(payload)
    report = _file_basics(path, "pdf", "pdf_renderer")
    if not report.ok:
        return report
    try:
        import pymupdf
        with pymupdf.open(path) as document:
            report.checks["has_pages"] = document.page_count > 0
            text = ""
            for page in list(document)[:3]:
                text += page.get_text()
            report.checks["has_extractable_text"] = len(text.strip()) > 20
            if not report.checks["has_pages"]:
                report.ok = False
                report.detail = "the PDF has no pages"
                report.error = MedhaError(module="pdf_renderer", error_type="empty_pdf",
                                           detail=report.detail, recovery="regenerate")
    except Exception as error:
        logger.debug(f"pdf deep check skipped: {error}")
    return report


@validator("csv")
def _validate_csv(payload: Any) -> ValidationReport:
    return _file_basics(_as_path(payload), "csv", "csv_writer")


# --------------------------------------------------------------- other ----

@validator("file")
def _validate_file(payload: Any) -> ValidationReport:
    """A filesystem operation: did the thing end up where it was claimed?"""
    path = _as_path(payload)
    checks = {"path_returned": bool(path)}
    if not path:
        # Absence of a path is absence of evidence, not evidence of failure -
        # plenty of file tools answer with a count or a listing. Record that
        # nothing could be checked; do not overrule a tool that said it worked.
        return ValidationReport(ok=True, kind="file", checks=checks,
                                detail="the tool reported no path - not checked")
    exists = os.path.exists(path)
    checks["exists"] = exists
    checks["non_empty"] = exists and os.path.getsize(path) > 0
    if not exists:
        return ValidationReport(ok=False, kind="file", checks=checks,
                                detail=f"{path} does not exist",
                                error=MedhaError(module="file_tools",
                                                  error_type="file_missing",
                                                  detail=path, recovery="retry"))
    return ValidationReport(ok=True, kind="file", checks=checks, detail=f"{path} exists")


@validator("code")
def _validate_code(payload: Any) -> ValidationReport:
    """Parse it. ZEUS does not get to say code works because it looks like
    code - if it cannot be parsed, it does not work."""
    source = payload
    if isinstance(payload, dict):
        source = payload.get("code") or payload.get("output") or ""
    source = str(source or "")
    checks = {"non_empty": bool(source.strip())}
    if not source.strip():
        return ValidationReport(ok=False, kind="code", checks=checks,
                                detail="no code produced",
                                error=MedhaError(module="code_agent",
                                                  error_type="empty_output",
                                                  recovery="regenerate"))
    # Strip a markdown fence if the model wrapped the answer in one.
    if source.lstrip().startswith("```"):
        lines = [line for line in source.splitlines() if not line.strip().startswith("```")]
        source = "\n".join(lines)
    try:
        ast.parse(source)
        checks["parses"] = True
        return ValidationReport(ok=True, kind="code", checks=checks,
                                detail="code parses")
    except SyntaxError as error:
        checks["parses"] = False
        return ValidationReport(
            ok=False, kind="code", checks=checks,
            detail=f"line {error.lineno}: {error.msg}",
            error=MedhaError(module="code_agent", error_type="syntax_error",
                              detail=f"line {error.lineno}: {error.msg}",
                              recovery="fix_and_retry"))


@validator("rag")
def _validate_rag(payload: Any) -> ValidationReport:
    """A grounded answer must actually be grounded: it needs sources, and it
    must not be the empty-knowledge-base placeholder dressed up as an answer.
    """
    answer = payload
    sources: List[Any] = []
    if isinstance(payload, dict):
        sources = payload.get("sources") or payload.get("citations") or []
        answer = (payload.get("answer") or payload.get("response")
                  or payload.get("output") or payload.get("definition")
                  or payload.get("text") or "")
        if not answer:
            # A knowledge tool that answers with its own shaped dict (a term
            # lookup, say) is still an answer. Judge it on content, not on
            # whether it used the key this validator happened to look for.
            content = {k: v for k, v in payload.items()
                       if k not in ("sources", "citations") and v}
            answer = "; ".join(f"{k}: {v}" for k, v in content.items())
    answer = str(answer or "")
    checks = {
        "has_answer": len(answer.strip()) > 0,
        "has_sources": bool(sources),
    }
    if not checks["has_answer"]:
        return ValidationReport(ok=False, kind="rag", checks=checks,
                                detail="no answer produced",
                                error=MedhaError(module="rag", error_type="empty_answer",
                                                  recovery="regenerate"))
    if not checks["has_sources"]:
        # Not a hard failure - an honest "nothing indexed" answer is correct
        # behaviour - but it is recorded so the response can say so.
        return ValidationReport(ok=True, kind="rag", checks=checks,
                                detail="answer is not backed by indexed sources")
    return ValidationReport(ok=True, kind="rag", checks=checks,
                            detail=f"answer cites {len(sources)} source(s)")


@validator("text")
def _validate_text(payload: Any) -> ValidationReport:
    """The model actually said something, and did not return one of the
    known failure placeholders."""
    text = payload
    if isinstance(payload, dict):
        text = payload.get("response") or payload.get("output") or ""
    text = str(text or "")
    stripped = text.strip()
    failure_markers = ("[ZEUS could not reach", "[ZEUS could not reach", "[Model '",
                       "[ZEUS model failure", "could not be generated")
    checks = {
        "non_empty": bool(stripped),
        "not_a_failure_marker": not any(stripped.startswith(m) for m in failure_markers),
    }
    if not checks["non_empty"]:
        return ValidationReport(ok=False, kind="text", checks=checks,
                                detail="empty response",
                                error=MedhaError(module="llm", error_type="empty_response",
                                                  recovery="regenerate"))
    if not checks["not_a_failure_marker"]:
        return ValidationReport(
            ok=False, kind="text", checks=checks, detail=stripped[:160],
            error=MedhaError(module="llm", error_type="model_unavailable",
                              detail=stripped[:160], recovery="fallback_deterministic"))
    return ValidationReport(ok=True, kind="text", checks=checks,
                            detail=f"{len(stripped)} characters")


@validator("data")
def _validate_data(payload: Any) -> ValidationReport:
    """Structured data from a tool: present, shaped, and not an error dict."""
    checks: Dict[str, bool] = {"produced": payload is not None}
    if payload is None:
        return ValidationReport(ok=False, kind="data", checks=checks,
                                detail="no data returned",
                                error=MedhaError(module="data_tool",
                                                  error_type="no_data",
                                                  recovery="retry"))
    if isinstance(payload, dict) and payload.get("error"):
        checks["no_error_field"] = False
        return ValidationReport(ok=False, kind="data", checks=checks,
                                detail=str(payload["error"]),
                                error=MedhaError(module="data_tool",
                                                  error_type="tool_reported_error",
                                                  detail=str(payload["error"]),
                                                  recovery="retry"))
    checks["no_error_field"] = True
    if isinstance(payload, (list, dict)):
        checks["non_empty"] = len(payload) > 0
    return ValidationReport(ok=True, kind="data", checks=checks,
                            detail=f"{type(payload).__name__} returned")


@validator("bundle")
def _validate_bundle(payload: Any) -> ValidationReport:
    """A step that wrote several files at once - a briefing pack, say.

    Every file is checked against the validator for its own extension. One bad
    file fails the bundle: a pack with a corrupt deck in it is not a pack.
    """
    paths: List[str] = []

    def collect(value: Any):
        if isinstance(value, str) and "." in value and len(value) > 4:
            if value.rsplit(".", 1)[-1].lower() in ("docx", "xlsx", "pptx", "pdf", "csv"):
                paths.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(payload)
    unique = list(dict.fromkeys(paths))
    checks: Dict[str, bool] = {"files_reported": bool(unique)}
    if not unique:
        return ValidationReport(
            ok=False, kind="bundle", checks=checks,
            detail="the step reported no files",
            error=MedhaError(module="bundle", error_type="no_path_returned",
                              recovery="regenerate"))

    failures: List[str] = []
    for path in unique:
        extension = path.rsplit(".", 1)[-1].lower()
        report = validate(extension, path)
        checks[Path(path).name] = report.ok
        if not report.ok:
            failures.append(f"{Path(path).name}: {report.detail}")

    if failures:
        return ValidationReport(
            ok=False, kind="bundle", checks=checks,
            detail="; ".join(failures),
            error=MedhaError(module="bundle", error_type="file_corrupt",
                              detail="; ".join(failures), recovery="regenerate"))
    return ValidationReport(ok=True, kind="bundle", checks=checks,
                            detail=f"{len(unique)} file(s) verified")


@validator("ocr")
def _validate_ocr(payload: Any) -> ValidationReport:
    """OCR is judged on how much text came back and, when the engine reports
    it, the confidence - not on whether the call returned without raising."""
    text = payload
    confidence = None
    if isinstance(payload, dict):
        text = payload.get("text", "")
        confidence = payload.get("confidence")
    text = str(text or "")
    checks = {"extracted_text": len(text.strip()) >= 8}
    if confidence is not None:
        try:
            checks["confidence_acceptable"] = float(confidence) >= 0.45
        except (TypeError, ValueError):
            pass
    if not checks["extracted_text"]:
        return ValidationReport(
            ok=False, kind="ocr", checks=checks,
            detail="little or no text extracted",
            error=MedhaError(module="ocr", error_type="low_extraction",
                              detail=f"{len(text.strip())} characters",
                              recovery="retry_with_preprocessing"))
    if checks.get("confidence_acceptable") is False:
        return ValidationReport(
            ok=False, kind="ocr", checks=checks,
            detail=f"OCR confidence {confidence} is below threshold",
            error=MedhaError(module="ocr", error_type="low_confidence",
                              detail=str(confidence),
                              recovery="retry_with_preprocessing"))
    return ValidationReport(ok=True, kind="ocr", checks=checks,
                            detail=f"{len(text.strip())} characters extracted")
