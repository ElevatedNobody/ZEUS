"""
Path normalisation and protected locations.

The previous check compared a path's text against five system folders. It
missed every spelling Windows accepts for the same place - `\\\\?\\C:\\Windows`,
`\\\\localhost\\C$\\Windows`, `C:\\WINDOWS\\..\\Windows` - and it protected nothing of
ZEUS's own: a tool call could rewrite `config.ini` (turning off the login),
overwrite the audit log, or read the credential database.

`check(path)` resolves the path to one canonical absolute form first and only
then compares, and it knows the locations ZEUS itself must keep out of reach of
any prompt-driven tool.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional, Tuple

SYSTEM_ROOTS = [
    "C:\\Windows",
    "C:\\Program Files\\WindowsApps",
    "C:\\ProgramData\\Microsoft",
    "C:\\Recovery",
    "C:\\System Volume Information",
    "/etc", "/boot", "/System", "/usr/bin", "/usr/sbin",
]

# Argument names that carry a filesystem path in ZEUS's tool signatures.
PATH_ARGUMENTS = ("path", "src", "dst", "root", "folder", "file", "file_path",
                  "image_path", "source_path", "destination", "input_path",
                  "output_path", "working_dir", "directory", "name_or_path",
                  "target", "audio_path", "pdf_path")

_UNC = re.compile(r"^[\\/]{2}(?![?.][\\/])")
_EXTENDED = re.compile(r"^[\\/]{2}[?.][\\/](UNC[\\/])?", re.IGNORECASE)


def canonical(path: str) -> Tuple[Optional[Path], str]:
    """Return (resolved path, reason). A None path means 'refuse'."""
    if not isinstance(path, str) or not path.strip():
        return None, "empty path"
    text = path.strip().strip('"').strip("'")
    if "\x00" in text:
        return None, "path contains a NUL byte"

    extended = _EXTENDED.match(text)
    if extended:
        if extended.group(1):
            return None, "network (UNC) paths are not permitted"
        text = text[extended.end():]
    if _UNC.match(text):
        return None, "network (UNC) paths are not permitted"
    try:
        resolved = Path(os.path.expandvars(text)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "path could not be resolved"
    if _UNC.match(str(resolved)):
        return None, "network (UNC) paths are not permitted"
    return resolved, ""


def _within(child: Path, parent: Path) -> bool:
    try:
        child_text = os.path.normcase(str(child))
        parent_text = os.path.normcase(str(parent))
    except Exception:
        return False
    return child_text == parent_text or child_text.startswith(parent_text.rstrip("\\/") + os.sep)


def protected_roots() -> list:
    from core.paths import protected_roots as project_roots
    roots = []
    for item in list(project_roots()):
        try:
            roots.append(Path(item).resolve())
        except Exception:
            continue
    return roots


def system_roots() -> list:
    out = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    candidates = list(SYSTEM_ROOTS) + ([windir] if windir else [])
    for item in candidates:
        try:
            out.append(Path(item).resolve())
        except Exception:
            continue
    return out


def check(path: str, extra_protected: Iterable[Path] = ()) -> Tuple[bool, str]:
    """(allowed, reason) for a path an AI-invoked tool wants to touch."""
    resolved, reason = canonical(path)
    if resolved is None:
        return False, reason
    for root in system_roots():
        if _within(resolved, root):
            return False, f"'{resolved}' is inside a protected system location"
    for root in list(protected_roots()) + [Path(p) for p in extra_protected]:
        if _within(resolved, root):
            return False, (f"'{resolved}' is part of ZEUS's protected data, code or "
                           f"configuration and cannot be touched by a tool")
    return True, ""


def paths_in(arguments: dict) -> list:
    """Every path-like argument value in a tool call."""
    found = []
    for key, value in (arguments or {}).items():
        if key in PATH_ARGUMENTS and isinstance(value, str) and value.strip():
            found.append((key, value))
    return found
