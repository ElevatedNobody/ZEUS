"""
The result vocabulary every ZEUS check reports in.

States (TEST_AGNI.bat, STEP 2 : START, the diagnostic report):

    PASS            the real function ran and its output was validated
    FAIL            it ran and did not work
    WARN            works, with a problem the operator should know about
    DEGRADED        the primary path failed; a verified fallback is in use
    UNAVAILABLE     cannot run here (not installed, needs admin, no hardware)
    INPUT_REQUIRED  the check needs something only a person can supply
                    (a face in front of the camera, speech into the mic)
    NOT_TESTED      deliberately not run, with the reason

Lifecycle states for a model are kept SEPARATE - downloaded is not installed
is not verified is not functional:

    DOWNLOADED -> INSTALLED -> CHECKSUM_VALID -> LOAD_TEST_PASSED
               -> INFERENCE_TEST_PASSED
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"
INPUT_REQUIRED = "INPUT_REQUIRED"
NOT_TESTED = "NOT_TESTED"

ALL_STATES = (PASS, FAIL, WARN, DEGRADED, UNAVAILABLE, INPUT_REQUIRED, NOT_TESTED)

# Worst first - used to roll many results up into one.
_SEVERITY = {FAIL: 0, DEGRADED: 1, UNAVAILABLE: 2, INPUT_REQUIRED: 3, WARN: 4,
             NOT_TESTED: 5, PASS: 6}

# Model lifecycle flags.
DOWNLOADED = "DOWNLOADED"
INSTALLED = "INSTALLED"
CHECKSUM_VALID = "CHECKSUM_VALID"
LOAD_TEST_PASSED = "LOAD_TEST_PASSED"
INFERENCE_TEST_PASSED = "INFERENCE_TEST_PASSED"


@dataclass
class Result:
    """One check. The failure fields are the ones the diagnostic report
    requires: WHAT FAILED, WHERE, WHY, EVIDENCE, FIX ATTEMPTED, CURRENT STATE,
    FALLBACK."""

    component: str
    status: str
    reason: str = ""                    # WHY - one line, plain words
    where: str = ""                     # WHERE - file / subsystem / endpoint
    evidence: str = ""                  # EVIDENCE - the observed output
    fix_attempted: str = ""
    current_state: str = ""
    fallback: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    group: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status == PASS


def worst(statuses: List[str]) -> str:
    if not statuses:
        return NOT_TESTED
    return min(statuses, key=lambda s: _SEVERITY.get(s, 0))


class Timer:
    def __enter__(self):
        self.started = time.time()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc):
        self.seconds = round(time.time() - self.started, 2)
        return False


def guarded(component: str, group: str, fn, *args, **kwargs) -> List[Result]:
    """Run a check function; an unexpected exception becomes a FAIL result
    with the traceback as evidence instead of ending the whole run."""
    import traceback
    started = time.time()
    try:
        out = fn(*args, **kwargs)
        results = out if isinstance(out, list) else [out]
        for result in results:
            result.group = result.group or group
        return results
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return [Result(component, FAIL, reason=f"the check itself crashed: "
                                               f"{type(error).__name__}: {error}",
                       where=f"{getattr(fn, '__module__', '')}.{getattr(fn, '__name__', '')}",
                       evidence=traceback.format_exc()[-1500:], group=group,
                       duration_s=round(time.time() - started, 2))]


def summarize(results: List[Result]) -> Dict[str, int]:
    counts = {state: 0 for state in ALL_STATES}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def optional(value: Optional[Any], default: str = "-") -> str:
    return default if value in (None, "") else str(value)
