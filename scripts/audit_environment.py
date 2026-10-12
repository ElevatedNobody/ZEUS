"""
Phase A - standalone environment audit.

    python scripts/audit_environment.py            # audit + write the report
    python scripts/audit_environment.py --json     # print the full JSON
    python scripts/audit_environment.py --no-ollama  # skip the daemon check

Writes config/industrial_environment.json and prints a compact summary.
Read-only: installs nothing, downloads nothing, contacts nothing except the
local Ollama daemon on loopback.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industrial import ENVIRONMENT_REPORT_PATH  # noqa: E402
from industrial.environment import audit, summarise  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the ZEUS industrial environment")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--no-ollama", action="store_true", help="skip the Ollama check")
    parser.add_argument("--no-write", action="store_true", help="do not write the report file")
    args = parser.parse_args()

    report = audit(write=not args.no_write, check_ollama=not args.no_ollama)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(summarise(report))
        if not args.no_write:
            print(f"\nReport written to {ENVIRONMENT_REPORT_PATH}")

    return 0 if report["environment_status"] != "NOT_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
