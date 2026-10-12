"""
Start the REAL ZEUS application for UI tests - against throw-away data.

    python tests/e2e/app_harness.py --data <dir> [--cdp 9333] [--screenshot out.png]
                                    [--seconds 20] [--seed-demo]

* AGNI_DATA_DIR / AGNI_LOG_DIR / AGNI_DOCUMENTS_DIR point into --data, so the
  operator's real database, vault and documents are never touched;
* a test account is created and signed in through the real SessionManager
  (the same code the login screen calls - authentication is not bypassed, it
  is performed);
* the ZEUS interface is started exactly as main.py starts it;
* with --cdp, Qt WebEngine exposes Chromium's debugging protocol on
  127.0.0.1 only, for Playwright's connect_over_cdp();
* with --screenshot, the window is captured after it has loaded.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--cdp", type=int, default=0)
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--user", default="e2e.operator")
    parser.add_argument("--password", default="E2E-Test-Passw0rd")
    parser.add_argument("--role", default="admin")
    parser.add_argument("--network-mode", default="sovereign")
    parser.add_argument("--web-login", action="store_true",
                        help="start like main.run_medha: the ZEUS sign-in screen, no session yet")
    parser.add_argument("--setup", action="store_true", help="with --web-login: no account at all (first run)")
    parser.add_argument("--seed-fixture-kf", action="store_true",
                        help="ingest the labelled SYNTHETIC test-fixture corpus (origin test_fixture) into the "
                             "throw-away knowledge base")
    args = parser.parse_args()

    data = Path(args.data).resolve()
    for sub in ("data", "logs", "documents"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    os.environ["AGNI_DATA_DIR"] = str(data / "data")
    os.environ["AGNI_LOG_DIR"] = str(data / "logs")
    os.environ["AGNI_DOCUMENTS_DIR"] = str(data / "documents")
    if args.cdp:
        os.environ["AGNI_E2E_CDP_PORT"] = str(args.cdp)
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)

    from voice.offline import enforce_offline
    enforce_offline()
    from security.egress import egress_guard
    egress_guard.configure(mode=args.network_mode)
    egress_guard.install()
    from ui.agni_shell import prepare_environment
    prepare_environment()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from memory.database import init_db, set_user_role, get_user
    init_db()
    from auth.auth_manager import auth_manager
    from auth.session_manager import session_manager
    if args.web_login:
        # The account exists (unless --setup); signing in is done THROUGH the
        # page by the test, via the same auth/web_login.py the app uses.
        if not args.setup and not get_user(args.user):
            auth_manager.create_account(args.user, args.password)
            set_user_role(get_user(args.user)["id"], args.role)
        if args.seed_demo:
            from industrial import plant_data
            plant_data.seed_demo_data()
        if args.seed_fixture_kf:
            from knowledge_fabric import ORIGIN_FIXTURE, ingest
            corpus = ROOT / "tests" / "fixtures" / "knowledge_fabric" / "corpus"
            for path in sorted(corpus.glob("*.md")):
                overrides = {"confidentiality": "confidential"} if "Board" in path.name else {}
                ingest.ingest_document(path, origin=ORIGIN_FIXTURE, overrides=overrides, embed=False)
        import main as agni_main
        from ui.agni_shell import make_shell

        def build(progress):
            assistant = agni_main.MedhaAssistant(use_ui=True, use_voice=False, progress=progress)
            return {"on_message": assistant.handle_text, "on_listen": assistant.listen_and_process}

        window = make_shell(workspace_factory=build)
        window.show()
        print(f"READY cdp={args.cdp or '-'} web-login", flush=True)
        if args.screenshot:
            QTimer.singleShot(int(max(4.0, args.seconds - 1) * 1000),
                              lambda: (window.grab().save(args.screenshot), print(f"SCREENSHOT {args.screenshot}", flush=True)))
        if args.seconds:
            QTimer.singleShot(int(args.seconds * 1000), app.quit)
        return app.exec()
    if not get_user(args.user):
        auth_manager.create_account(args.user, args.password)
    set_user_role(get_user(args.user)["id"], args.role)
    result = session_manager.login(args.user, args.password, client="e2e")
    if not result.ok:
        print(f"LOGIN FAILED: {result.message}", flush=True)
        return 2
    if args.seed_demo:
        from industrial import plant_data
        plant_data.seed_demo_data()

    import main as agni_main
    assistant = agni_main.MedhaAssistant(use_ui=True, use_voice=False)
    from ui.agni_shell import run_ui

    def on_lock(switch_user=False):
        # Tests exercise lock through the bridge; the real login dialog would
        # block a headless run, so the harness records the lock and re-shows.
        if switch_user:
            session_manager.logout()
        else:
            session_manager.lock()
        print(f"LOCKED switch_user={switch_user}", flush=True)
        window.hide()

    app, window = run_ui(on_message=assistant.handle_text, on_listen=assistant.listen_and_process,
                         on_lock=on_lock, username=args.user)
    assistant.window = window
    print(f"READY cdp={args.cdp or '-'}", flush=True)

    if args.screenshot:
        def grab():
            window.grab().save(args.screenshot)
            print(f"SCREENSHOT {args.screenshot}", flush=True)
        QTimer.singleShot(int(max(4.0, args.seconds - 1) * 1000), grab)
    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
