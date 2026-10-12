"""
The name, in one place.

ZEUS is written in a lot of windows, banners, reports and generated
documents, and it is always written the same way: the name, and directly
beneath it what the name stands for. Keeping both strings here means the
tagline cannot drift into three slightly different wordings across the
title bar, the login screen and the front page of a generated deck - which
is exactly what happens when a strapline is retyped wherever it is needed.

Nothing in this module imports anything. It is safe to read from the
earliest startup code, before Qt, before the database, before the logger.
"""

from __future__ import annotations

APP_NAME = "ZEUS"

# The expansion. Title Case for headings and window chrome; TAGLINE_UPPER for
# the all-caps console banners, where mixed case looks like a mistake.
APP_TAGLINE = "Autonomous AI Command & Intelligence System"

# The name the project carried before ZEUS. Internal modules, the database
# file and the generated-documents folder keep it for compatibility.
LEGACY_NAME = "ZEUS"
APP_TAGLINE_UPPER = APP_TAGLINE.upper()

# What the product is, for the places that want a sentence rather than a
# strapline: the window title of the main workbench, the About panel.
APP_DESCRIPTION = "Autonomous AI Command & Intelligence System - local models, local data, audited actions"

APP_VERSION = "3.0.0"


def title(suffix: str = "") -> str:
    """A window title: "ZEUS" or "ZEUS — Access"."""
    return f"{APP_NAME} — {suffix}" if suffix else APP_NAME


def banner(width: int = 68, rule: str = "=") -> str:
    """The console banner: a rule, the name, the tagline, a rule.

    Used by every command-line entry point, so `python main.py`,
    SETUP_VOICE.bat and the readiness check all open the same way.
    """
    line = rule * width
    return (f"{line}\n"
            f"  {APP_NAME}\n"
            f"  {APP_TAGLINE}\n"
            f"{line}")


def lockup(separator: str = "\n") -> str:
    """Name and tagline together, for anywhere that renders one string."""
    return f"{APP_NAME}{separator}{APP_TAGLINE}"
