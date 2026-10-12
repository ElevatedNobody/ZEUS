"""
The zero-regression baseline, read from the LIVE registries - never typed.

    python scripts/feature_inventory.py            write tests/baseline/zero_regression_baseline.json
                                                   and docs/generated/FEATURE_REGISTRY.md
    python scripts/feature_inventory.py --check    compare the live system with the baseline;
                                                   exit 1 if anything that existed is gone

Why generated: the previous inventory counted by hand and its own summary line
was wrong three times. A registry cannot miscount itself. docs/
MEDHA_FEATURE_INVENTORY.md explains the features in words; this file is the
part a test can hold the system to (tests/test_zero_regression_contract.py).

Runs against a throw-away data directory so reading the registries never
touches the operator's database.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "baseline" / "zero_regression_baseline.json"
REGISTRY_DOC = ROOT / "docs" / "generated" / "FEATURE_REGISTRY.md"

# Sections whose members may only ever GROW. Removing one is a regression.
MONOTONIC = ("tools", "agents", "bridge_methods", "model_slots", "validators",
             "autofix_strategies", "task_kinds", "pipeline_stages", "database_tables",
             "bat_files", "ops_commands", "route_targets", "ui_capabilities", "config_keys")


def _sandbox() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="medha_inventory_"))
    for name in ("AGNI_DATA_DIR", "AGNI_LOG_DIR", "AGNI_DOCUMENTS_DIR"):
        os.environ[name] = str(scratch / name.lower())
    sys.path.insert(0, str(ROOT))
    logging.disable(logging.CRITICAL)


def _tools() -> Dict[str, Dict[str, str]]:
    from core.tool_registry import registry
    import core.builtin_tools, core.agni_tools, core.vault_tools  # noqa: F401
    for optional in ("industrial.tools", "industrial.plant_tools", "core.orchestration.tools",
                     "knowledge_fabric.tools"):
        try:
            __import__(optional)
        except ModuleNotFoundError as error:
            if optional.split(".")[0] not in str(error):
                raise
    tools = getattr(registry, "_tools", None) or getattr(registry, "tools", {})
    out = {}
    for name, tool in sorted(tools.items()):
        fn = getattr(tool, "func", None) or getattr(tool, "function", None)
        risk = getattr(tool, "risk", getattr(tool, "risk_level", ""))
        out[name] = {"module": getattr(fn, "__module__", "?"),
                     "risk": str(getattr(risk, "name", risk))}
    return out


def _agents() -> Dict[str, Dict[str, Any]]:
    from core.orchestration import agents
    out = {}
    for spec in agents.describe_agents():
        out[spec["id"]] = {"name": spec.get("name", ""), "tools": sorted(spec.get("tools", []))}
    return out


def _bridge() -> List[str]:
    from ui.bridge_contract import METHODS
    return sorted(METHODS)


def _model_slots() -> List[str]:
    from core.model_router import DEFAULT_PREFERENCES
    return sorted(DEFAULT_PREFERENCES)


def _validators() -> List[str]:
    from core.orchestration import validators
    return sorted(validators.registered_kinds())


def _autofix() -> List[str]:
    from core.orchestration import autofix
    return sorted(autofix.known_strategies())


def _contracts() -> Dict[str, List[str]]:
    from core.orchestration import contracts
    kinds = sorted(k.value for k in contracts.TaskKind)
    stages = []
    for name in dir(contracts):
        obj = getattr(contracts, name)
        if isinstance(obj, type) and name.lower().endswith("stage"):
            stages = sorted(member.value for member in obj)
    return {"task_kinds": kinds, "pipeline_stages": stages}


def _route_targets() -> List[str]:
    from core import decision_engine
    return sorted({target for _pattern, target, _args in decision_engine._ROUTES})


def _tables() -> List[str]:
    from memory import database
    database.init_db()
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    finally:
        conn.close()


def _bat_files() -> List[str]:
    return sorted(p.name for p in ROOT.glob("*.bat"))


def _ops_commands() -> List[str]:
    source = (ROOT / "agni_ops" / "cli.py").read_text(encoding="utf-8")
    commands = set(re.findall(r'add_parser\(\s*"([a-z0-9_-]+)"', source))
    try:
        from agni_ops import suites
        commands.update(f"test {name}" for name in getattr(suites, "SUITES", {}))
    except Exception:
        pass
    try:
        from agni_ops import firewall
        commands.update(f"firewall {name}" for name in getattr(firewall, "COMMANDS", {}))
    except Exception:
        pass
    return sorted(commands)


def _config_keys() -> List[str]:
    import configparser
    parser = configparser.ConfigParser()
    parser.read(ROOT / "config.ini", encoding="utf-8")
    return sorted(f"{section}.{key}" for section in parser.sections() for key in parser[section])


def _ui_capabilities() -> List[str]:
    """Every backend capability the interface exposes: the tools it calls and
    the bridge methods it uses. A new interface must reach all of them."""
    capabilities = set()
    for path in list((ROOT / "ui").rglob("*.js")) + list((ROOT / "ui").rglob("*.html")):
        if "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        capabilities.update(f"tool:{name}" for name in re.findall(r'\bT\("([a-z_]+)"', text))
        capabilities.update(f"tool:{name}" for name in re.findall(r'tool\(\s*"([a-z_]+)"', text))
        capabilities.update(f"bridge:{name}" for name in re.findall(r'\bC\("([a-z_]+)"', text))
        capabilities.update(f"bridge:{name}" for name in re.findall(r'\.call\(\s*"([a-z_]+)"', text))
    return sorted(capabilities)


def _test_count() -> int:
    count = 0
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        count += sum(1 for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name.startswith("test_"))
    return count


def collect() -> Dict[str, Any]:
    _sandbox()
    data: Dict[str, Any] = {}
    data["tools"] = _tools()
    data["agents"] = _agents()
    data["bridge_methods"] = _bridge()
    data["model_slots"] = _model_slots()
    data["validators"] = _validators()
    data["autofix_strategies"] = _autofix()
    data.update(_contracts())
    data["route_targets"] = _route_targets()
    data["database_tables"] = _tables()
    data["bat_files"] = _bat_files()
    data["ops_commands"] = _ops_commands()
    data["config_keys"] = _config_keys()
    data["ui_capabilities"] = _ui_capabilities()
    data["test_functions"] = _test_count()
    return data


def _names(value) -> set:
    return set(value) if isinstance(value, (list, dict)) else set()


def compare(baseline: Dict[str, Any], live: Dict[str, Any]) -> Dict[str, List[str]]:
    """What the baseline had that the live system no longer has."""
    lost = {}
    for section in MONOTONIC:
        missing = sorted(_names(baseline.get(section)) - _names(live.get(section)))
        if missing:
            lost[section] = missing
    for agent, spec in (baseline.get("agents") or {}).items():
        now = (live.get("agents") or {}).get(agent)
        if now:
            gone = sorted(set(spec.get("tools", [])) - set(now.get("tools", [])))
            if gone:
                lost[f"agent:{agent}:tools"] = gone
    return lost


def write_doc(data: Dict[str, Any]) -> None:
    REGISTRY_DOC.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Feature registry (generated - do not edit)", "",
             "Written by `scripts/feature_inventory.py` from the live registries. "
             "`tests/test_zero_regression_contract.py` fails if anything listed here disappears.", ""]
    summary = {k: len(v) for k, v in data.items() if isinstance(v, (list, dict))}
    lines += ["| section | count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    lines += [f"| test functions | {data['test_functions']} |", ""]
    lines += ["## Tools", "", "| tool | module | risk |", "|---|---|---|"]
    lines += [f"| `{n}` | {t['module']} | {t['risk']} |" for n, t in data["tools"].items()]
    lines += ["", "## Agents", "", "| id | name | tools |", "|---|---|---|"]
    lines += [f"| `{i}` | {a['name']} | {len(a['tools'])} |" for i, a in data["agents"].items()]
    for section in ("bridge_methods", "model_slots", "validators", "autofix_strategies",
                    "task_kinds", "pipeline_stages", "database_tables", "bat_files",
                    "ops_commands", "ui_capabilities", "route_targets", "config_keys"):
        lines += ["", f"## {section.replace('_', ' ').title()}", "",
                  ", ".join(f"`{x}`" for x in data[section])]
    REGISTRY_DOC.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    live = collect()
    if args.check:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        lost = compare(baseline, live)
        if lost:
            print("REGRESSION - present in the baseline, missing now:")
            for section, names in lost.items():
                print(f"  {section}: {', '.join(names)}")
            return 1
        print("no regression: everything in the baseline is still present")
        return 0
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(live, indent=1, sort_keys=True), encoding="utf-8")
    write_doc(live)
    counts = {k: len(v) for k, v in live.items() if isinstance(v, (list, dict))}
    print(json.dumps({**counts, "test_functions": live["test_functions"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
