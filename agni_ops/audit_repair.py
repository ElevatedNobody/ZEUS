"""
python -m agni_ops audit-repair [--apply]

Handles ONE situation, and refuses every other: records at the END of
logs/audit_chain.jsonl that were not signed with this installation's key -
written by a process that had a different data folder (and so a different
key), e.g. a test run - AND the anchor in the database (kept apart from the
log) confirms the last authentic record comes right before them.

Nothing is deleted:
  * a full copy of the log is saved first (audit_chain.backup_<stamp>.jsonl);
  * the foreign records are MOVED, unchanged, to
    audit_chain.foreign-quarantine_<stamp>.jsonl;
  * an authentic record describing the repair (how many lines, which
    sequence numbers, where they went, the backup) is appended to the chain,
    so the repair is itself part of the audit history.

Without --apply it only reports what it would do. Exit 0 = nothing to do or
done, 1 = refused / failed.
"""

from __future__ import annotations

import hmac
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List


def plan() -> Dict[str, Any]:
    from security.audit_chain import audit_chain
    path = audit_chain.path()
    if not path.exists():
        return {"action": "none", "reason": "no audit chain yet"}
    lines = path.read_text(encoding="utf-8").splitlines()
    signed: List[bool] = []
    records: List[Dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            return {"action": "refuse", "reason": "the log contains a line that is not a record - "
                                                  "that is not what this command repairs"}
        records.append(record)
        signed.append(hmac.compare_digest(str(record.get("hash", "")), audit_chain._sign(record)))
    if all(signed):
        return {"action": "none", "reason": "every record is signed with this installation's key",
                "verify": audit_chain.verify()}
    first_bad = signed.index(False)
    if not all(not s for s in signed[first_bad:]):
        return {"action": "refuse", "reason": f"a record at line {first_bad + 1} fails its signature and authentic "
                                              f"records follow it - that looks like tampering, not a foreign writer. "
                                              f"Investigate; nothing was changed."}
    if first_bad == 0:
        return {"action": "refuse", "reason": "no record verifies with this installation's key"}
    last_good = records[first_bad - 1]
    anchor = audit_chain._read_anchor()
    if not anchor or anchor["seq"] != last_good["seq"] or anchor["hash"] != last_good["hash"]:
        return {"action": "refuse", "reason": f"the database anchor ({anchor}) does not confirm record "
                                              f"{last_good['seq']} as the last authentic one - nothing was changed"}
    foreign = records[first_bad:]
    return {"action": "quarantine", "keep_lines": first_bad, "foreign_lines": [first_bad + 1, len(records)],
            "foreign_count": len(foreign), "foreign_seq": [foreign[0].get("seq"), foreign[-1].get("seq")],
            "written": [time.strftime("%Y-%m-%d %H:%M", time.localtime(foreign[0]["ts"])),
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(foreign[-1]["ts"]))],
            "events": sorted({r.get("event", "?") for r in foreign}),
            "last_authentic": {"seq": last_good["seq"], "line": first_bad},
            "anchor_confirms": True}


def apply(log) -> int:
    from security.audit_chain import _process_lock, audit_chain
    path = audit_chain.path()
    with _process_lock(path.with_suffix(".lock")):
        decision = plan()
        if decision["action"] != "quarantine":
            log.say(f"  {decision['action'].upper()}: {decision['reason']}")
            return 0 if decision["action"] == "none" else 1
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(f"audit_chain.backup_{stamp}.jsonl")
        quarantine = path.with_name(f"audit_chain.foreign-quarantine_{stamp}.jsonl")
        shutil.copy2(path, backup)
        raw = path.read_text(encoding="utf-8").splitlines(keepends=True)
        keep, moved = raw[:decision["keep_lines"]], raw[decision["keep_lines"]:]
        quarantine.write_text("".join(moved), encoding="utf-8")
        temporary = path.with_name(f"audit_chain.rewrite_{stamp}.tmp")
        temporary.write_text("".join(keep), encoding="utf-8")
        temporary.replace(path)
    audit_chain.reset_cache()
    audit_chain.append("audit_repair",
                       f"{decision['foreign_count']} record(s) not signed with this installation's key (lines "
                       f"{decision['foreign_lines'][0]}-{decision['foreign_lines'][1]}, seq {decision['foreign_seq'][0]}-"
                       f"{decision['foreign_seq'][1]}, written {decision['written'][0]} - {decision['written'][1]}, "
                       f"events {', '.join(decision['events'])}) moved unchanged to {quarantine.name}; "
                       f"full copy of the log before the repair: {backup.name}",
                       actor="operator (agni_ops audit-repair --apply)", severity="notice")
    result = audit_chain.verify()
    log.say(f"  backup:      {backup}")
    log.say(f"  quarantined: {quarantine} ({decision['foreign_count']} records)")
    log.say(f"  verify:      {'OK' if result['ok'] else 'BROKEN'} - {result['detail']}")
    return 0 if result["ok"] else 1
