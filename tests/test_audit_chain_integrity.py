"""
The tamper-evident audit chain under the conditions that broke it for real:
two processes appending at once, and a process with a different key (a test
run with its own data folder) appending to the main log.

Every test uses its own folder - the real logs/audit_chain.jsonl is never read.
"""

import hashlib
import hmac
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from security.audit_chain import AuditChain

ROOT = Path(__file__).resolve().parent.parent


class Chain(AuditChain):
    """An AuditChain whose log, key and anchor live in a test folder."""

    def __init__(self, log_dir: Path, key_dir: Path = None):
        super().__init__()
        self.log_dir, self.key_dir = Path(log_dir), Path(key_dir or log_dir)
        self.anchor = None

    def path(self):
        return self.log_dir / "audit_chain.jsonl"

    def key_path(self):
        return self.key_dir / ".audit_key"

    def _read_anchor(self):
        return self.anchor

    def _write_anchor(self, seq, digest):
        self.anchor = {"seq": seq, "hash": digest}


def _records(chain):
    return [json.loads(line) for line in chain.path().read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(chain, records):
    chain.path().write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _signed(chain, seq, prev, detail="x"):
    record = {"seq": seq, "ts": 1.0, "event": "test", "actor": "", "detail": detail, "prev": prev}
    record["hash"] = chain._sign(record)
    return record


def test_a_clean_chain_verifies(tmp_path):
    chain = Chain(tmp_path)
    for i in range(20):
        assert chain.append("test", f"record {i}")
    report = chain.verify()
    assert report["ok"] and report["records"] == 20 and report["forks"] == []


def test_two_writers_in_one_process_never_reuse_a_number(tmp_path):
    a, b = Chain(tmp_path), Chain(tmp_path)
    b._key = a._get_key()
    a.append("test", "a1")
    b.append("test", "b1")          # b has never seen a's record in memory
    a.append("test", "a2")          # a's cached tail is stale - it must re-read the file
    assert [r["seq"] for r in _records(a)] == [1, 2, 3]
    assert a.verify()["ok"] and not a.verify()["forks"]


def test_two_processes_appending_at_once_keep_one_chain(tmp_path):
    Chain(tmp_path).append("test", "genesis")           # creates the shared key and log
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from tests.test_audit_chain_integrity import Chain
        chain = Chain(Path({str(tmp_path)!r}))
        for i in range(60):
            assert chain.append("test", sys.argv[1] + str(i))
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, name], cwd=str(ROOT)) for name in ("p", "q")]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    chain = Chain(tmp_path)
    records = _records(chain)
    assert len(records) == 121
    assert [r["seq"] for r in records] == list(range(1, 122))
    report = chain.verify()
    assert report["ok"] and report["forks"] == []


def test_an_authentic_concurrent_fork_is_reported_not_hidden(tmp_path):
    chain = Chain(tmp_path)
    r1 = _signed(chain, 1, "0" * 64)
    r2 = _signed(chain, 2, r1["hash"])
    fork_a = _signed(chain, 3, r2["hash"], "from the firewall process")
    fork_b = _signed(chain, 3, r2["hash"], "from the app")
    r4 = _signed(chain, 4, fork_b["hash"])
    _write(chain, [r1, r2, fork_a, fork_b, r4])
    report = chain.verify()
    assert report["ok"] is True
    assert len(report["forks"]) == 1 and report["forks"][0]["seq"] == 3
    assert "concurrent" in report["detail"]


def test_a_copied_line_is_tampering(tmp_path):
    chain = Chain(tmp_path)
    r1 = _signed(chain, 1, "0" * 64)
    r2 = _signed(chain, 2, r1["hash"])
    _write(chain, [r1, r2, r2])
    report = chain.verify()
    assert not report["ok"] and "appears twice" in report["detail"]


def test_a_modified_record_is_caught(tmp_path):
    chain = Chain(tmp_path)
    for i in range(5):
        chain.append("test", f"record {i}")
    records = _records(chain)
    records[2]["detail"] = "rewritten"
    _write(chain, records)
    report = chain.verify()
    assert not report["ok"] and "modified" in report["detail"]


def test_a_deleted_record_is_caught(tmp_path):
    chain = Chain(tmp_path)
    for i in range(5):
        chain.append("test", f"record {i}")
    records = _records(chain)
    del records[2]
    _write(chain, records)
    chain.anchor = None
    report = chain.verify()
    assert not report["ok"] and ("gap" in report["detail"] or "link" in report["detail"])


def test_a_truncated_tail_is_caught_by_the_anchor(tmp_path):
    chain = Chain(tmp_path)
    for i in range(5):
        chain.append("test", f"record {i}")
    _write(chain, _records(chain)[:3])
    report = chain.verify()
    assert not report["ok"] and "anchor" in report["detail"]


def test_a_process_with_another_key_cannot_break_the_main_chain(tmp_path):
    main = Chain(tmp_path / "logs", tmp_path / "data")
    for i in range(3):
        main.append("test", f"real {i}")
    stranger = Chain(tmp_path / "logs", tmp_path / "temp_data")     # same log, different key
    assert stranger.owns() is False
    assert stranger.append("test", "from a test run")
    assert len(_records(main)) == 3                                    # nothing added to the main log
    side = list((tmp_path / "logs").glob("audit_chain.foreign-*.jsonl"))
    assert len(side) == 1 and "from a test run" in side[0].read_text(encoding="utf-8")
    assert main.verify()["ok"]


def _foreign(key_dir: Path, log_dir: Path) -> Chain:
    other = Chain(log_dir, key_dir)
    other._owner_cache[str(other.path())] = True        # simulate the old code, which never checked
    return other


def test_repair_plan_quarantines_only_a_confirmed_foreign_tail(tmp_path, monkeypatch):
    import agni_ops.audit_repair as audit_repair
    import security.audit_chain as module
    main = Chain(tmp_path / "logs", tmp_path / "data")
    for i in range(5):
        main.append("test", f"real {i}")
    anchor = dict(main.anchor)
    stranger = _foreign(tmp_path / "temp", tmp_path / "logs")
    for i in range(3):
        stranger.append("company_intelligence", f"test run {i}")
    main.anchor = anchor                                  # the anchor still names record 5
    monkeypatch.setattr(module, "audit_chain", main)
    decision = audit_repair.plan()
    assert decision["action"] == "quarantine"
    assert decision["foreign_count"] == 3 and decision["foreign_lines"] == [6, 8]
    assert decision["last_authentic"] == {"seq": 5, "line": 5}


def test_repair_refuses_when_authentic_records_follow_a_bad_one(tmp_path, monkeypatch):
    import agni_ops.audit_repair as audit_repair
    import security.audit_chain as module
    main = Chain(tmp_path)
    for i in range(5):
        main.append("test", f"real {i}")
    records = _records(main)
    records[2]["detail"] = "tampered"
    _write(main, records)
    monkeypatch.setattr(module, "audit_chain", main)
    assert audit_repair.plan()["action"] == "refuse"


def test_repair_refuses_without_anchor_confirmation(tmp_path, monkeypatch):
    import agni_ops.audit_repair as audit_repair
    import security.audit_chain as module
    main = Chain(tmp_path / "logs", tmp_path / "data")
    for i in range(3):
        main.append("test", f"real {i}")
    stranger = _foreign(tmp_path / "temp", tmp_path / "logs")
    stranger.append("x", "foreign")
    main.anchor = None
    monkeypatch.setattr(module, "audit_chain", main)
    assert audit_repair.plan()["action"] == "refuse"


def test_repair_apply_moves_keeps_and_records_everything(tmp_path, monkeypatch):
    import agni_ops.audit_repair as audit_repair
    import security.audit_chain as module
    main = Chain(tmp_path / "logs", tmp_path / "data")
    for i in range(4):
        main.append("test", f"real {i}")
    anchor = dict(main.anchor)
    original_real = main.path().read_text(encoding="utf-8")
    stranger = _foreign(tmp_path / "temp", tmp_path / "logs")
    for i in range(2):
        stranger.append("company_intelligence", f"test run {i}")
    before = main.path().read_text(encoding="utf-8")
    main.anchor = anchor
    monkeypatch.setattr(module, "audit_chain", main)

    class Log:
        def __init__(self):
            self.lines = []

        def say(self, line=""):
            self.lines.append(line)

    assert audit_repair.apply(Log()) == 0
    logs = tmp_path / "logs"
    backup = next(logs.glob("audit_chain.backup_*.jsonl"))
    quarantine = next(logs.glob("audit_chain.foreign-quarantine_*.jsonl"))
    assert backup.read_text(encoding="utf-8") == before                    # nothing lost
    assert quarantine.read_text(encoding="utf-8") == before[len(original_real):]
    records = _records(main)
    assert main.path().read_text(encoding="utf-8").startswith(original_real)
    assert records[-1]["event"] == "audit_repair" and "moved unchanged" in records[-1]["detail"]
    assert main.verify()["ok"]
