"""
The zero-regression contract of the ZEUS upgrade (docs/MEDHA_FEATURE_INVENTORY.md).

tests/baseline/zero_regression_baseline.json was generated from the LIVE
registries before the upgrade began (scripts/feature_inventory.py). Every
tool, agent, agent-tool binding, bridge method, model slot, validator,
auto-fix strategy, task kind, pipeline stage, database table, BAT file, ops
command, decision route target, config key and interface capability in it
must still exist. Things may be ADDED; nothing may disappear.

The one deliberate exception is recorded in RETIRED below, each with where
the capability lives now - an entry there without a replacement is itself a
failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# name -> where the capability now lives. Only VISUAL components may be
# retired (the interface is rebuilt from scratch); a backend capability never.
RETIRED: dict = {
    # The previous page called these to open native Qt dialogs. The ZEUS page
    # uses in-page forms that call the replacements; the old methods stay in
    # the bridge contract (bridge_methods), so nothing that called them breaks.
    "bridge:change_password": "bridge:account_password (Settings / user menu → Change password)",
    "bridge:create_user": "bridge:account_create (Security Center → Users)",
    "bridge:unlock_vault": "bridge:vault_open (Vault screen)",
}


def _load():
    import feature_inventory
    baseline = json.loads(feature_inventory.BASELINE.read_text(encoding="utf-8"))
    return feature_inventory, baseline


def test_nothing_that_existed_before_the_upgrade_is_gone():
    feature_inventory, baseline = _load()
    live = feature_inventory.collect()
    lost = feature_inventory.compare(baseline, live)
    for section in list(lost):
        lost[section] = [name for name in lost[section] if name not in RETIRED]
        if not lost[section]:
            del lost[section]
    assert not lost, "features present before the upgrade are missing now: " + json.dumps(lost, indent=1)


def test_every_retirement_names_its_replacement():
    for name, replacement in RETIRED.items():
        assert replacement and replacement.strip(), f"{name} was retired without a replacement"


def test_the_baseline_is_the_pre_upgrade_one():
    """Regenerating the baseline after features were removed would hide the
    loss. The counts below are what the live system had on 2026-09-26."""
    _, baseline = _load()
    assert len(baseline["tools"]) >= 210
    assert len(baseline["agents"]) >= 23
    assert len(baseline["bridge_methods"]) >= 20
    assert len(baseline["ui_capabilities"]) >= 77
    assert len(baseline["bat_files"]) >= 13
