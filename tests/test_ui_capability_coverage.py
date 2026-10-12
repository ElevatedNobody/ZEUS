"""
Nothing the previous interface could do is lost in the ZEUS interface.

The baseline (tests/baseline/zero_regression_baseline.json, `ui_capabilities`)
lists the 77 tools and bridge methods the previous interface reached. Every one
must be reached from ui/medha/ - directly, or through a documented replacement
that does the same thing in-page (the old ones stay in the bridge contract, so
nothing that called them breaks).
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZEUS = ROOT / "ui" / "medha"

# Native Qt dialogs replaced by in-page forms that call the same backend.
REPLACED = {
    "bridge:change_password": "bridge:account_password",
    "bridge:create_user": "bridge:account_create",
    "bridge:unlock_vault": "bridge:vault_open",
}


def _reached():
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ZEUS / "js").rglob("*.js"))
    tools = set(re.findall(r"""M\.tool\(\s*["']([a-z0-9_]+)["']""", text))
    calls = set(re.findall(r"""M\.call\(\s*["']([a-z0-9_]+)["']""", text))
    return {f"tool:{t}" for t in tools} | {f"bridge:{c}" for c in calls}


def test_every_previous_capability_is_reachable():
    baseline = json.loads((ROOT / "tests" / "baseline" / "zero_regression_baseline.json").read_text(encoding="utf-8"))
    reached = _reached()
    missing = [c for c in baseline["ui_capabilities"] if c not in reached and REPLACED.get(c) not in reached]
    assert not missing, f"capabilities the ZEUS interface no longer reaches: {missing}"


def test_replaced_bridge_methods_still_exist_for_compatibility():
    from ui.bridge_contract import METHODS
    for old, new in REPLACED.items():
        assert old.split(":")[1] in METHODS and new.split(":")[1] in METHODS


def test_every_tool_the_interface_calls_is_registered():
    from core.tool_registry import registry
    unknown = [c for c in _reached() if c.startswith("tool:") and registry.get(c[5:]) is None]
    assert not unknown, unknown


def test_every_bridge_method_the_interface_calls_is_in_the_contract():
    from ui.bridge_contract import METHODS
    unknown = [c for c in _reached() if c.startswith("bridge:") and c[7:] not in METHODS]
    assert not unknown, unknown


def test_the_requested_screens_exist():
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ZEUS / "js").rglob("*.js"))
    screens = set(re.findall(r"""M\.screen\(\s*["']([a-z_]+)["']""", text)) | {"excel", "deck", "report"}
    for required in ("command", "company", "knowledge", "documents", "evidence", "reasoning", "industrial",
                     "artifacts", "excel", "deck", "report", "memory", "models", "agents", "automation",
                     "vault", "security", "firewall", "audit", "settings", "productivity"):
        assert required in screens, required
    auth = (ZEUS / "js" / "auth.js").read_text(encoding="utf-8")
    for state in ("LOCKED", "INITIALIZING", "CAMERA READY", "SCANNING", "FACE DETECTED", "LIVENESS CHECK",
                  "VERIFYING IDENTITY", "IDENTITY VERIFIED", "ACCESS GRANTED", "ACCESS DENIED", "CAMERA ERROR",
                  "AUTHENTICATION TIMEOUT", "SECOND FACTOR REQUIRED", "SESSION EXPIRED", "LOCKDOWN"):
        assert state in auth, state


def test_the_page_loads_nothing_from_outside():
    html = (ZEUS / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html and "connect-src 'none'" in html
    everything = html + "\n".join(p.read_text(encoding="utf-8") for p in ZEUS.rglob("*.*") if p.suffix in (".js", ".css"))
    assert not re.search(r"https?://", everything), "no URL to any network resource may appear in the interface"
    assert "fetch(" not in everything and "XMLHttpRequest" not in everything and "WebSocket" not in everything
