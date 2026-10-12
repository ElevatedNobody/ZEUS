"""
python -m agni_ops <command> - what the ZEUS operator BAT files run.

    system                      this machine (read-only)
    preflight                   STEP 2 : START checks          exit 1 = cannot start
    repair                      REPAIR.bat                     exit 1 = something still broken
    firewall <sub>              firewall rules                 exit 0/1/2/3/4 (see agni_ops/firewall.py)
    test <suite> [options]      verification suites            exit 1 = any FAIL
    install-verify              STEP 1 : INSTALL verification after installing
    projects <action>           ZEUS projects: health | db | offline | models [--live] | reindex [--embed]
    network <action>            ZEUS Network: check | selftest
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agni_ops.runlog import RunLog                           # noqa: E402
from agni_ops.status import (FAIL, NOT_TESTED, PASS, Result, guarded, summarize, worst)   # noqa: E402

SUITES = ("all", "models", "stt", "whisper", "voice", "vision", "memory", "hermes", "face", "firewall",
          "security", "agents", "artifacts", "rag", "regression", "projects", "jarvis", "network")


def _print(log: RunLog, results: List[Result]):
    for result in results:
        log.result(result)


def _summary(log: RunLog, results: List[Result]) -> int:
    counts = summarize(results)
    log.heading("SUMMARY")
    log.say("  " + "   ".join(f"{state}: {count}" for state, count in counts.items() if count))
    overall = worst([r.status for r in results])
    log.say(f"  OVERALL: {overall}")
    log.say(f"  log: {log.path}")
    return 1 if FAIL in [r.status for r in results] else 0


# ------------------------------------------------------------------ test ----

def run_suite(name: str, args, log: RunLog) -> List[Result]:
    from agni_ops import face, firewall, memory_diag, models, security_diag, suites, system, voice
    workdir = Path(tempfile.mkdtemp(prefix="agni_test_"))
    results: List[Result] = []
    if name == "models":
        log.heading("MODELS - presence, checksum, load, typed inference, validation")
        reports = guarded("model matrix", "models", lambda: [r.to_result() for r in models.run_matrix(log=log)])
        results += reports
        return results
    if name == "stt":
        status = voice.whisper_status(load=True, workdir=workdir)
        log.say(voice.render_status(status))
        for line in status:
            results.append(Result(f"WHISPER: {line['check']}", PASS if line["ok"] else (NOT_TESTED if line["ok"] is None else FAIL),
                                  line["detail"], group="stt"))
        log.heading("STT - devices > Whisper runtime > known speech > live listen through the app's own path")
        results.append(voice.stt_devices())
        results += guarded("whisper", "whisper", voice.whisper_suite, workdir, True, False)
        results += guarded("round trip", "stt", lambda: [voice.round_trip(workdir)])
        results += guarded("live listen", "stt", lambda: [voice.stt_live_trace(6.0, prompt=args.interactive)])
    elif name == "whisper":
        log.heading("WHISPER - staged, each stage in its own worker process")
        results += guarded("whisper", "whisper", voice.whisper_suite, workdir)
    elif name == "voice":
        log.heading("VOICE - microphone, speaking, round trip")
        results.append(voice.microphone_test(4.0, prompt=args.interactive))
        results += guarded("tts", "tts", voice.tts_suite, workdir)
        results += guarded("round trip", "voice", lambda: [voice.round_trip(workdir)])
    elif name == "vision":
        log.heading("VISION - the vision model on a real image, OCR, PDF")
        from core.hardware import model_plan
        vision_model = model_plan()["slots"].get("vision", "qwen3-vl:8b")
        results += guarded("vision model", "vision",
                           lambda: [models.test_model(vision_model, slot="vision").to_result()])
        for r in results:
            r.group = "vision"
        results += suites.offline_selftest(only_groups=["vision", "ocr", "documents"])
    elif name == "memory":
        log.heading("MEMORY - write policy, update, recall, context injection")
        results += guarded("memory", "memory", memory_diag.suite, False)
    elif name == "hermes":
        log.heading("HERMES - integrity, recall, and the live model using the memory")
        results += guarded("hermes", "hermes", memory_diag.suite, True)
        results += guarded("hermes context", "hermes", memory_diag.context_checks)
    elif name == "face":
        log.heading("FACE LOGIN - model, camera, liveness, identity, access decision")
        if args.interactive and not args.no_camera:
            print("    >>> Face the camera, alone, in good light <<<", flush=True)
            time.sleep(2)
        results += guarded("face", "face", face.suite, not args.no_camera)
    elif name == "firewall":
        log.heading("FIREWALL - rules, enforcement, tamper")
        results += guarded("firewall", "firewall", firewall.run, "all")
    elif name == "security":
        log.heading("SECURITY - bypass, login, RBAC, audit, registry, vault, injection")
        results += guarded("security", "security", security_diag.suite)
    elif name == "agents":
        log.heading("AGENTS & TOOLS - orchestration, plant tools, RAG")
        results += suites.offline_selftest(only_groups=["agents", "tools", "rag"])
    elif name == "artifacts":
        log.heading("ARTIFACTS - DOCX / XLSX / PPTX generated and validated")
        results += suites.offline_selftest(only_groups=["artifacts", "documents"])
    elif name == "rag":
        from agni_ops import rag
        log.heading("RAG - evaluation on the synthetic fixture, corpus integrity, coverage")
        results += guarded("rag evaluation", "rag", rag.evaluation_results)
        results += guarded("rag corpus", "rag", rag.corpus_results)
    elif name in ("projects", "jarvis"):
        from agni_ops import jarvis_ops
        log.heading("ZEUS - databases, event chain, evidence, model routing, offline capture, pipeline suite")
        results += guarded("jarvis", "jarvis", jarvis_ops.health)
    elif name == "network":
        from agni_ops import network_ops
        log.heading("ZEUS NETWORK - configuration, TLS, key storage, firewall, and the two-device test")
        results += guarded("network check", "network", network_ops.check)
        results += network_ops.selftest()
    elif name == "regression":
        log.heading("REGRESSION - the pytest suite")
        log.say("  (about 10 minutes)")
        results += suites.regression()
    _print(log, results)
    return results


def test(args) -> int:
    log = RunLog("test")
    started = time.time()
    from agni_ops import models, report, suites, system
    results: List[Result] = []
    if args.suite == "all":
        log.heading("SYSTEM")
        info = system.snapshot()
        system_results = system.check(info)
        _print(log, system_results)
        results += system_results
        for name in ("models", "whisper", "voice", "face", "memory", "hermes", "firewall", "security", "rag"):
            results += run_suite(name, args, log)
        log.heading("OFFLINE SELF-TEST - OCR, PDF, vision pipeline, RAG, documents, agents, tools")
        selftest = suites.offline_selftest()
        _print(log, selftest)
        results += selftest
        if not args.no_regression:
            results += run_suite("regression", args, log)
        table = models.matrix_table([_report_from(r) for r in results if r.group == "models"
                                     and r.component.startswith("model ")])
        paths = report.write(results, info, table, command="all", started=started)
        log.heading("REPORT")
        log.say(f"  {paths['txt']}")
        log.say(f"  {paths['json']}")
        log.say(f"  archived: {paths['archived']}")
    else:
        results += run_suite(args.suite, args, log)
    code = _summary(log, results)
    log.close()
    return code


def _report_from(result: Result):
    """Rebuild a ModelReport from a model Result, for the matrix table."""
    from agni_ops.models import ModelReport
    d = result.details
    yes = {"YES": True, "NO": False}
    return ModelReport(model=result.component.replace("model ", "", 1), type=d.get("type", ""),
                       present=yes.get(d.get("present")), checksum=d.get("checksum", ""),
                       load=yes.get(d.get("load")), inference=yes.get(d.get("inference")),
                       function=yes.get(d.get("function")), load_s=d.get("latency_load_s"),
                       inference_s=d.get("latency_inference_s"), resident_gb=d.get("resident_gb"),
                       vram_gb=d.get("vram_gb"), status=result.status, cause=result.reason,
                       function_detail=result.reason, fallback=result.fallback)


# ------------------------------------------------------------- commands ----

def main(argv=None, prog: str = "agni_ops") -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("system")
    sub.add_parser("preflight")
    sub.add_parser("repair")
    sub.add_parser("install-verify")
    fw = sub.add_parser("firewall")
    from agni_ops.firewall import COMMANDS
    fw.add_argument("action", choices=COMMANDS)
    t = sub.add_parser("test")
    t.add_argument("suite", choices=SUITES, nargs="?", default="all")
    t.add_argument("--deep", action="store_true")
    t.add_argument("--no-camera", action="store_true")
    t.add_argument("--interactive", action="store_true")
    t.add_argument("--no-regression", action="store_true")
    from agni_ops import rag as _rag
    _rag.add_arguments(sub.add_parser("rag"))
    jv = sub.add_parser("projects", aliases=["jarvis"], help="ZEUS project checks")
    jv.add_argument("action", choices=("health", "db", "offline", "models", "reindex"))
    jv.add_argument("--live", action="store_true", help="models: make real calls and validate the answers")
    jv.add_argument("--embed", action="store_true", help="reindex: also embed with the local embedding model")
    jv.add_argument("--no-suite", action="store_true", help="health: skip the pytest suite")
    net_cmd = sub.add_parser("network", help="ZEUS Network: check | selftest")
    net_cmd.add_argument("action", choices=("check", "selftest"))
    sub.add_parser("security-check")
    stt_status = sub.add_parser("stt-status", help="WHISPER STATUS - runtime, model, load, microphone, pipeline")
    stt_status.add_argument("--no-load", action="store_true", help="files and runtime only - load nothing")
    repair_audit = sub.add_parser("audit-repair")
    repair_audit.add_argument("--apply", action="store_true")
    model_cmd = sub.add_parser("models", help="model check / probe")
    model_cmd.add_argument("action", choices=("check", "probe"))
    model_cmd.add_argument("--kind", choices=("chat", "all"), default="chat",
                           help="probe: chat + vision models (default) or every configured model")
    args = parser.parse_args(argv)

    if args.command == "stt-status":
        from agni_ops import voice
        log = RunLog("stt_status")
        lines = voice.whisper_status(load=not args.no_load)
        log.say(voice.render_status(lines))
        failed = any(line["ok"] is False for line in lines)
        log.say(f"\n  exit code {1 if failed else 0}")
        log.close()
        return 1 if failed else 0

    if args.command == "models":
        from agni_ops import models
        from core import model_roles
        log = RunLog(f"model_{args.action}")
        if args.action == "probe":
            log.heading("MODEL PROBE - each model loaded, tested and unloaded, ONE AT A TIME")
            wanted = models.chat_models() if args.kind == "chat" else models.configured_models()
            reports = models.run_matrix(wanted, log=log)
        else:
            log.heading("MODEL CHECK - roles on this hardware and the last recorded tests (nothing is loaded)")
            health = models.load_health()
            reports = []
            for name, record in sorted(health.items()):
                log.say(f"  {name:<34}{record.get('status', '?'):<9}tested "
                        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(record['tested_at'])) if record.get('tested_at') else 'never'}")
        records = [r.to_dict() for r in reports] if reports else list(models.load_health().values())
        for record in records:
            log.say(f"\n  {record.get('model')}  [{record.get('status')}]  {record.get('slot', '')}")
            for p in models.verification_points(record):
                log.say(f"    {p['status']:<8} {p['point']:<22} {p['detail']}")
        log.heading("ROLES")
        failed = False
        for role in model_roles.roster():
            log.say(f"  {role['role']:<18}{role['state']:<14}{role['model'] or '-':<30}{(role['fallback'] or role['reason'])[:110]}")
            failed = failed or role["state"] in ("RUNTIME_DOWN", "UNTRUSTED")
        failed = failed or any(r.get("status") == FAIL for r in records)
        log.say(f"\n  exit code {1 if failed else 0}")
        log.close()
        return 1 if failed else 0

    if args.command == "audit-repair":
        from agni_ops import audit_repair
        log = RunLog("audit_repair")
        log.heading("AUDIT CHAIN REPAIR" + ("" if args.apply else " - dry run (add --apply to act)"))
        if args.apply:
            code = audit_repair.apply(log)
        else:
            decision = audit_repair.plan()
            log.say(json.dumps({k: v for k, v in decision.items() if k != "verify"}, indent=2, default=str))
            code = 0 if decision["action"] in ("none", "quarantine") else 1
        log.close()
        return code

    if args.command == "rag":
        from agni_ops import rag
        log = RunLog("rag")
        log.heading(f"ZEUS KNOWLEDGE - {args.action}")
        code = rag.run(args, log)
        log.say(f"  exit code {code}")
        log.close()
        return code

    if args.command in ("projects", "jarvis"):
        from agni_ops import jarvis_ops
        titles = {"health": "ZEUS HEALTH CHECK", "db": "DATABASE CHECK", "offline": "OFFLINE CHECK",
                  "models": "ZEUS MODEL ROUTING" + (" - LIVE CALLS" if args.live else " (nothing is loaded)"),
                  "reindex": "ZEUS PROJECT INDEX"}
        log = RunLog(f"projects_{args.action}")
        log.heading(titles[args.action])
        runners = {"health": lambda: jarvis_ops.health(run_suite=not args.no_suite), "db": jarvis_ops.db_checks,
                   "offline": jarvis_ops.offline_checks,
                   "models": lambda: jarvis_ops.model_routes() + (jarvis_ops.live_models() if args.live else []),
                   "reindex": lambda: jarvis_ops.reindex(args.embed)}
        results = guarded(f"jarvis {args.action}", "jarvis", runners[args.action])
        _print(log, results)
        code = _summary(log, results)
        log.close()
        return code

    if args.command == "network":
        from agni_ops import network_ops
        log = RunLog(f"network_{args.action}")
        log.heading("ZEUS NETWORK CHECK" if args.action == "check" else
                    "ZEUS NETWORK TWO-DEVICE TEST - a server and two devices on THIS machine (loopback, TLS)")
        results = guarded("network check", "network", network_ops.check) if args.action == "check" \
            else network_ops.selftest()
        _print(log, results)
        code = _summary(log, results)
        log.close()
        return code

    if args.command == "security-check":
        from agni_ops import firewall, rag, security_diag
        log = RunLog("security")
        log.heading("SECURITY CHECK - access control, audit, injection, firewall, knowledge access")
        results = guarded("security", "security", security_diag.suite)
        results += guarded("firewall status", "firewall", firewall.run, "status")
        rag_checks = guarded("knowledge access", "rag", rag.evaluation_results)
        results += [r for r in rag_checks if any(key in r.component for key in
                                                 ("access_control", "injection", "abstention", "RAG evaluation"))]
        _print(log, results)
        code = _summary(log, results)
        log.close()
        return code

    if args.command == "system":
        from agni_ops import system
        info = system.snapshot()
        print(json.dumps(info, indent=2, default=str))
        return 0

    if args.command == "preflight":
        from agni_ops import preflight
        log = RunLog("start")
        log.heading("ZEUS PREFLIGHT")
        log.say(f"  {'COMPONENT':<28}{'STATUS':<14}REASON / FALLBACK")
        results = preflight.run()
        for r in results:
            log.status(r.status, f"{r.component:<26}", r.reason)
            if r.fallback and r.status != PASS:
                log.say(f"{'':<17}FALLBACK: {r.fallback}")
        if preflight.fatal(results):
            log.say("\n  ZEUS CANNOT START - an essential component failed. Run REPAIR.bat.")
            log.close()
            return 1
        log.say("\n  Preflight done - starting the backend. 'ZEUS ONLINE' appears once it is running.")
        log.close()
        return 0

    if args.command == "repair":
        from agni_ops import preflight, repair
        log = RunLog("repair")
        log.heading("ZEUS REPAIR")
        results = repair.run()
        _print(log, results)
        log.heading("VERIFY (preflight after repair)")
        after = preflight.run()
        _print(log, after)
        code = _summary(log, results + after)
        log.close()
        return code

    if args.command == "firewall":
        from agni_ops import firewall
        log = RunLog("firewall")
        log.heading(f"ZEUS FIREWALL - {args.action}")
        results = guarded(f"firewall {args.action}", "firewall", firewall.run, args.action)
        _print(log, results)
        _summary(log, results)
        code = firewall.exit_code(results)
        log.say(f"  exit code {code}")
        log.close()
        return code

    if args.command == "install-verify":
        from agni_ops import face, firewall, jarvis_ops, memory_diag, models, network_ops, system, voice
        log = RunLog("install")
        log.heading("INSTALL VERIFICATION - ONLINE & OFFLINE COMPONENTS")
        info = system.snapshot()
        results = system.check(info)
        _print(log, results)
        log.heading("MODELS")
        results += [r.to_result() for r in models.run_matrix(log=log)]
        log.heading("WHISPER")
        whisper = voice.whisper_suite(Path(tempfile.mkdtemp(prefix="agni_install_")))
        _print(log, whisper)
        results += whisper
        log.heading("FACE (no camera)")
        faces = face.suite(camera=False)
        _print(log, faces)
        results += faces
        log.heading("MEMORY / HERMES")
        from agni_ops.repair import memory_stores
        memory = [memory_stores()] + memory_diag.suite(False)
        _print(log, memory)
        results += memory
        log.heading("ZEUS DATABASES & OFFLINE / ONLINE")
        proj_res = guarded("project databases", "jarvis", jarvis_ops.db_checks) + \
            guarded("offline checks", "jarvis", jarvis_ops.offline_checks) + \
            guarded("network checks", "network", network_ops.check)
        _print(log, proj_res)
        results += proj_res
        log.heading("FIREWALL")
        fw = firewall.status() + firewall.audit()
        _print(log, fw)
        results += fw
        code = _summary(log, results)
        log.close()
        return code

    if args.command == "test":
        return test(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
