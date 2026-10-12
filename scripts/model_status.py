"""
Print the trusted-model registry status and this machine's model plan.

    python scripts/model_status.py

Read-only: talks to the local Ollama daemon on loopback, changes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from core.hardware import model_plan
    from core.ollama_client import ollama_client
    from security.integrity import model_integrity

    plan = model_plan()
    installed = ollama_client.list_models(force=True) if ollama_client.is_available(force=True) else []
    print(f"\nModel plan for this machine (tier {plan['tier']}):")
    for slot, name in plan["slots"].items():
        have = any(ollama_client._model_matches(m, name) for m in installed)
        print(f"  {slot:<16} {name:<22} {'pulled' if have else 'NOT PULLED'}")

    report = model_integrity.report()
    print(f"\nTrusted registry: {'present' if report.get('exists') else 'not created yet'}"
          f", signature {'valid' if report.get('signature_valid') else 'NOT valid'}")
    for row in report.get("models", []):
        print(f"  {row['status']:<10} {row['model']}")
    missing = [n for n in plan["required"]
               if not any(ollama_client._model_matches(m, n) for m in installed)]
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
