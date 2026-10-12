"""
Auto-correction: when a validator says no, try something SPECIFIC.

The rule this module exists to enforce: a retry that changes nothing is not a
repair, it is a second chance at the same failure. So every strategy here
either changes the arguments, changes the approach, or declines to retry at
all - and the attempt budget is hard, because an assistant stuck in a repair
loop is worse than one that reports a clear failure.

Strategies are keyed by the `error_type` a validator produced, which is why
those slugs are stable machine-readable strings rather than messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from core.logger import get_logger
from core.orchestration.contracts import MedhaError, PlanStep

logger = get_logger("orchestration.autofix")

# Two repair attempts per step. Beyond that the failure is real and the user
# is told plainly instead of being made to wait.
MAX_ATTEMPTS = 2


@dataclass
class Repair:
    """A concrete change to try on the next attempt."""

    args: Dict[str, Any]
    note: str
    strategy: str


Strategy = Callable[[MedhaError, PlanStep], Optional[Repair]]
_STRATEGIES: Dict[str, Strategy] = {}


def strategy(error_type: str):
    def decorate(function: Strategy):
        _STRATEGIES[error_type] = function
        return function
    return decorate


def plan_repair(error: Optional[MedhaError], step: PlanStep) -> Optional[Repair]:
    """What to try next, or None when nothing sensible can be attempted."""
    if error is None:
        return None
    if step.attempts >= MAX_ATTEMPTS:
        logger.info(f"step '{step.id}' exhausted its {MAX_ATTEMPTS} repair attempts")
        return None
    handler = _STRATEGIES.get(error.error_type)
    if handler is None:
        logger.debug(f"no repair strategy for '{error.error_type}'")
        return None
    try:
        repair = handler(error, step)
    except Exception as failure:                 # a broken strategy must not kill the run
        logger.warning(f"repair strategy '{error.error_type}' raised: {failure}")
        return None
    if repair:
        logger.info(f"repairing '{step.tool}' via {repair.strategy}: {repair.note}")
    return repair


def known_strategies() -> Dict[str, str]:
    return {name: function.__doc__ or "" for name, function in _STRATEGIES.items()}


# ------------------------------------------------------------ strategies --

@strategy("layout_overflow")
def _fix_overflow(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """Content ran off the slide: ask the renderer for fewer items per page."""
    args = dict(step.args)
    current = int(args.get("max_items") or 6)
    args["max_items"] = max(2, current - 2)
    args["compact"] = True
    return Repair(args=args, strategy="retry_with_fewer_items",
                  note=f"reducing to {args['max_items']} items per slide")


@strategy("type_too_small")
def _fix_small_type(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """Text below the legibility floor: same fix - less on the page."""
    return _fix_overflow(error, step)


@strategy("file_missing")
def _fix_missing(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """The file was never written. Re-run the step once - a transient write
    failure (locked file, missing folder) usually clears."""
    return Repair(args=dict(step.args), strategy="regenerate",
                  note="the file was not created; regenerating")


@strategy("file_empty")
def _fix_empty(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """Zero bytes on disk - regenerate rather than hand over a dead file."""
    return Repair(args=dict(step.args), strategy="regenerate",
                  note="the file was empty; regenerating")


@strategy("file_corrupt")
def _fix_corrupt(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """It would not reopen. Write to a fresh path so a half-written file on
    disk cannot be picked up instead."""
    args = dict(step.args)
    for key in ("path", "output_path", "filename"):
        if key in args and isinstance(args[key], str) and args[key]:
            stem, _, extension = args[key].rpartition(".")
            if stem:
                args[key] = f"{stem}_v2.{extension}"
            break
    return Repair(args=args, strategy="regenerate_to_new_path",
                  note="the file would not reopen; writing a fresh copy")


@strategy("empty_document")
def _fix_empty_document(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """It opened but had no content - the content step, not the write, failed."""
    args = dict(step.args)
    args["require_content"] = True
    return Repair(args=args, strategy="regenerate_with_content",
                  note="regenerating with content required")


@strategy("empty_workbook")
def _fix_empty_workbook(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """Same as an empty document, for spreadsheets."""
    return _fix_empty_document(error, step)


@strategy("empty_deck")
def _fix_empty_deck(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """Same as an empty document, for decks."""
    return _fix_empty_document(error, step)


@strategy("broken_formula_reference")
def _fix_formulas(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """A formula points at a sheet that is not in the book. Rebuild with
    cross-sheet references switched off rather than ship #REF! cells."""
    args = dict(step.args)
    args["cross_sheet_formulas"] = False
    return Repair(args=args, strategy="rebuild_formulas",
                  note="rebuilding without cross-sheet references")


@strategy("syntax_error")
def _fix_syntax(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """Code did not parse. Feed the parser's own message back to the coding
    model - a targeted correction, not a fresh guess."""
    args = dict(step.args)
    previous = args.get("fix_hint", "")
    args["fix_hint"] = (f"{previous}\nThe previous attempt failed to parse: "
                        f"{error.detail}. Return corrected, complete code only.").strip()
    return Repair(args=args, strategy="fix_and_retry",
                  note=f"feeding the syntax error back: {error.detail}")


@strategy("low_extraction")
def _fix_extraction(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """OCR found almost nothing. Retry with preprocessing turned up before
    concluding the page is blank."""
    args = dict(step.args)
    args["preprocess"] = True
    args["upscale"] = True
    return Repair(args=args, strategy="retry_with_preprocessing",
                  note="retrying OCR with preprocessing and upscaling")


@strategy("low_confidence")
def _fix_confidence(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """Low OCR confidence gets the same treatment as low extraction."""
    return _fix_extraction(error, step)


@strategy("tool_reported_error")
def _fix_tool_error(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """The tool itself said no. One retry only - if it says no twice, it
    means it."""
    if step.attempts >= 1:
        return None
    return Repair(args=dict(step.args), strategy="retry_once",
                  note="retrying the tool once")


@strategy("no_path_returned")
def _fix_no_path(error: MedhaError, step: PlanStep) -> Optional[Repair]:
    """The step ran but reported no file. Re-run once."""
    if step.attempts >= 1:
        return None
    return Repair(args=dict(step.args), strategy="retry_once",
                  note="no path was reported; retrying once")
