"""
ZEUS / ZEUS Security Manager
Permission levels, confirmation gating, path validation, command screening and
the audit log. Every tool call passes through here AFTER `security.policy` has
authorised it (see core/tool_registry.py).

What changed for ZEUS:
  * path checks resolve every Windows spelling of a path first and protect
    ZEUS's own data, logs, configuration and code (security/pathguard.py);
  * command screening normalises whitespace and case and knows the PowerShell
    and cmd forms of the destructive commands, plus commands that reach the
    network;
  * confirmation goes through the approval gateway, so the desktop interface
    shows a real dialog instead of waiting on an invisible console prompt;
  * every audit line is also appended to the tamper-evident audit chain.
"""

import json
import re
import time
from enum import IntEnum
from pathlib import Path
from typing import Callable, Optional

from core.logger import get_logger

logger = get_logger("security")

ROOT = Path(__file__).resolve().parent.parent
from core.paths import log_dir as _log_dir  # noqa: E402
AUDIT_LOG = _log_dir() / "audit.log"


class RiskLevel(IntEnum):
    SAFE = 0
    LOW_RISK = 1
    MEDIUM_RISK = 2
    HIGH_RISK = 3
    CRITICAL = 4


# Tools/actions that require confirmation at or above this level
CONFIRM_THRESHOLD = RiskLevel.HIGH_RISK

# Kept for compatibility - the literal patterns the project always refused.
BLOCKED_PATTERNS = [
    "format c:",
    "rm -rf /",
    "del /f /s /q c:\\",
    "shutdown /s /f",
    ":(){ :|:& };:",
]

# Matched against the command after whitespace is collapsed and case folded.
_BLOCKED_REGEXES = [
    r"\bformat(\.com)?\s+[a-z]:",
    r"\brm\s+(-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|--recursive\s+--force)\s+(/|~|[a-z]:)",
    r"\b(del|erase)\b[^|&]*\s/[sq]\b[^|&]*\s[a-z]:\\?\s*$",
    r"\b(rd|rmdir)\b[^|&]*\s/s\b[^|&]*\s[a-z]:\\?\s*$",
    r"\bremove-item\b[^|&]*-recurse[^|&]*\s[a-z]:\\?(\*)?\s*$",
    r"\bremove-item\b[^|&]*\s[a-z]:\\?(\*)?\s[^|&]*-recurse",
    r"\bdiskpart\b", r"\bbcdedit\b", r"\bvssadmin\s+delete\b", r"\bwbadmin\s+delete\b",
    r"\bcipher\s+/w\b", r"\bformat-volume\b", r"\bclear-disk\b",
    r"\breg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)",
    r"\bshutdown(\.exe)?\s+/[sr]\b.*\s/f\b",
    r"\bnet\s+user\s+\S+\s+\S*\s*/add\b", r"\bnet\s+localgroup\s+administrators\b.*/add",
    r"\bset-mppreference\b.*-disable", r"\bnetsh\s+advfirewall\s+set\s+\w+\s+state\s+off",
    r"\bset-netfirewallprofile\b.*-enabled\s+false",
    r"\bpowershell(\.exe)?\b.*\s-e(nc|ncodedcommand)?\s+[a-z0-9+/=]{16,}",
    # commands that reach the network are refused while ZEUS is sovereign
    r"\binvoke-webrequest\b", r"\binvoke-restmethod\b", r"\b(iwr|irm)\s+https?://",
    r"\b(curl|wget)(\.exe)?\s+[^|&]*https?://", r"\bcertutil\b.*-urlcache",
    r"\bbitsadmin\b.*/transfer", r"\bstart-bitstransfer\b",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
]
_BLOCKED = [re.compile(pattern, re.IGNORECASE) for pattern in _BLOCKED_REGEXES]

# Kept for compatibility with callers that read it.
PROTECTED_PATHS = [
    "C:\\Windows",
    "C:\\Windows\\System32",
    "/etc",
    "/boot",
    "/System",
]


class SecurityError(Exception):
    pass


class SecurityManager:
    def __init__(self):
        self._confirm_callback: Optional[Callable[[str], bool]] = None
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    def set_confirm_callback(self, callback: Callable[[str], bool]):
        """CLI / tests register a function(message) -> bool here. The desktop
        interface attaches to `security.approvals.approval_gateway` instead."""
        self._confirm_callback = callback

    # ---------- Path validation ----------

    def validate_path(self, path: str) -> bool:
        """Reject protected system paths, ZEUS's own protected data and code,
        network (UNC) paths and anything that cannot be resolved."""
        from security import pathguard
        allowed, reason = pathguard.check(str(path))
        if not allowed:
            logger.warning(f"Blocked path: {path} ({reason})")
        return allowed

    # ---------- Command screening ----------

    def is_blocked_command(self, command: str) -> bool:
        """Full screening for text that WILL be executed by a shell."""
        lowered = " ".join((command or "").lower().split())
        if any(pattern in lowered for pattern in BLOCKED_PATTERNS):
            return True
        return any(regex.search(lowered) for regex in _BLOCKED)

    @staticmethod
    def _matches_literal(text: str) -> bool:
        lowered = " ".join((text or "").lower().split())
        return any(pattern in lowered for pattern in BLOCKED_PATTERNS)

    # ---------- Confirmation ----------

    def request_confirmation(self, action_description: str, risk: RiskLevel,
                             force: bool = False, tool: str = "", origin: str = "command") -> bool:
        """
        Returns True if the action is allowed to proceed.
        Below the threshold (and not forced by policy) actions auto-approve but
        are still audited. Otherwise a human decides: the interface's approval
        dialog, the CLI prompt, or - with neither attached - NO.
        """
        approved = True
        if risk >= CONFIRM_THRESHOLD or force:
            from security.approvals import approval_gateway
            approved = approval_gateway.request(
                tool=tool or action_description.split(":")[0],
                description=action_description, risk=RiskLevel(int(risk)).name,
                origin=origin, fallback=self._confirm_callback)
            if not approval_gateway.attached and self._confirm_callback is None:
                logger.warning("No confirmation callback registered - denying by default")
        self._audit(action_description, risk, approved)
        return approved

    def check_and_confirm(self, tool_name: str, risk: RiskLevel, details: str = "",
                          force: bool = False, origin: str = "command") -> bool:
        """Convenience wrapper: block outright, else request confirmation.

        Only tools that hand text to a shell get the full command screening;
        for every other tool the arguments are prose (a document topic, a
        note) and only the literal catastrophic patterns are refused."""
        shell_tool = tool_name in ("run_command",)
        blocked = (self.is_blocked_command(details or tool_name) if shell_tool
                   else self._matches_literal(details or tool_name))
        if blocked:
            self._audit(f"BLOCKED: {tool_name} {details}", RiskLevel.CRITICAL, False)
            try:
                from security import events
                events.record("command_blocked", f"{tool_name}: {details[:200]}", severity="warning")
            except Exception:
                pass
            raise SecurityError(f"Action '{tool_name}' matched a blocked pattern and was refused.")
        return self.request_confirmation(f"{tool_name}: {details}" if details else tool_name,
                                         risk, force=force, tool=tool_name, origin=origin)

    # ---------- Audit log ----------

    def _audit(self, description: str, risk: RiskLevel, approved: bool):
        entry = {
            "ts": time.time(),
            "description": description,
            "risk": RiskLevel(int(risk)).name,
            "approved": approved,
        }
        try:
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")
        try:
            from security.audit_chain import audit_chain
            user = ""
            try:
                from auth.session_manager import session_manager
                user = session_manager.username or ""
            except Exception:
                pass
            audit_chain.append("action", description[:500], actor=user,
                               risk=entry["risk"], approved=approved)
        except Exception as e:
            logger.error(f"Failed to write audit chain: {e}")
        logger.info(f"AUDIT [{entry['risk']}] approved={approved} - {description[:200]}")


# Singleton used across the app
security_manager = SecurityManager()
