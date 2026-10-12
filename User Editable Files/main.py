"""
ZEUS - Main Entry Point (User Editable).

A desktop application: PySide6 windows, with the ZEUS interface drawn by Qt
WebEngine inside the process (no web server, no browser, no open port).
Startup: sovereign runtime (offline flags + egress guard) -> system init ->
model discovery -> capability readiness -> MANDATORY LOGIN -> interface.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict

# ============================================================================
# USER EDITABLE STARTUP & ADMIN ACCOUNT OVERRIDES
# ============================================================================

# Optional: Set these before starting ZEUS to automatically create,
# rename, or reset the password of your Admin account on startup!
# (Remember to clear OVERRIDE_ADMIN_NEW_PASSWORD back to "" after signing in.)
OVERRIDE_ADMIN_USERNAME: str = ""
OVERRIDE_RENAME_ADMIN_TO: str = ""
OVERRIDE_ADMIN_NEW_PASSWORD: str = ""

# Default voice behavior when started without command-line flags:
DEFAULT_ENABLE_VOICE: bool = True

# ============================================================================

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / "core").is_dir() else HERE
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import branding
from core.logger import setup_logger, get_logger
from core.decision_engine import DecisionEngine
from core.security_manager import security_manager
from memory.memory_manager import MemoryManager
from memory.database import init_db
from plugins.plugin_loader import discover_plugins

logger = get_logger("main")


class MedhaAssistant:
    def __init__(self, use_ui: bool = True, use_voice: bool = False, progress=None):
        # progress(step, state, detail): the ZEUS interface shows each real
        # start-up step while the workspace is built after sign-in.
        say = progress or (lambda *_args, **_kwargs: None)
        setup_logger()
        say("database", "running")
        init_db()
        say("database", "done")
        say("plugins", "running")
        loaded_plugins = discover_plugins()
        if loaded_plugins:
            logger.info(f"Plugins loaded: {', '.join(loaded_plugins)}")
        say("plugins", "done", f"{len(loaded_plugins or [])} loaded")
        say("memory", "running")
        self.memory = MemoryManager()
        say("memory", "done")
        say("engine", "running")
        self.engine = DecisionEngine()
        say("engine", "done")
        self.use_ui = use_ui
        self.use_voice = use_voice
        self.tts = None
        self.stt = None
        self.window = None
        self._listening = False

        security_manager.set_confirm_callback(self._confirm)

        if use_voice:
            say("voice", "running")
            self._init_voice()
            say("voice", "done" if self.use_voice else "unavailable",
                "" if self.use_voice else "text mode - voice did not start")
        else:
            say("voice", "off", "voice is switched off")

    def _init_voice(self):
        """Bring up whatever voice this machine can actually do.

        Provider selection, offline enforcement and the honest state all live
        in `voice.providers`. What matters here is what happens when there is
        no voice: ZEUS carries on in TEXT MODE. Voice is a feature of the
        application, not a precondition for it, so nothing in this method can
        prevent startup - the worst case is a logged reason and typed replies.
        """
        try:
            from voice.providers import VoiceState, voice_manager
        except Exception as error:                    # a broken voice package
            logger.warning(f"Voice subsystem unavailable ({error}); text mode.")
            self.use_voice = False
            return

        self.voice = voice_manager
        state = voice_manager.start()
        for line in voice_manager.banner():
            logger.info(line)

        # These stay for the existing call sites; they are now whichever
        # provider was actually selected, or None for text mode.
        self.tts = voice_manager.output
        self.stt = voice_manager.input

        if state in (VoiceState.AVAILABLE, VoiceState.DEGRADED):
            self.use_voice = True
        else:
            # DISABLED and ERROR both mean "no voice", and neither is fatal.
            self.use_voice = False
            logger.info(f"Continuing in text mode: {voice_manager.detail}")

    # ------------------------------------------------------------- session --

    def lock_session(self, switch_user: bool = False):
        """Lock the session (or sign out). The ZEUS interface reacts to the
        session event by replacing the workspace with its sign-in screen; the
        application keeps running. Only Quit terminates ZEUS."""
        from auth.session_manager import session_manager
        if switch_user:
            session_manager.logout()
        else:
            session_manager.lock("user request")

    def voice_report(self) -> Dict[str, Any]:
        """What the voice subsystem is actually doing right now - used by the
        UI, the startup check and the offline test.

        Every field comes from a probe or a load that happened. Nothing here
        reports a capability that was merely configured.
        """
        try:
            from voice.providers import voice_manager
            status = voice_manager.report()
        except Exception as e:
            status = {"state": "error", "detail": str(e)}
        status["runtime"] = {
            "voice_mode": self.use_voice,
            "stt_available": bool(self.stt),
            "tts_available": bool(self.tts),
        }
        return status

    def close_voice(self):
        """Release audio devices and model memory on shutdown."""
        try:
            from voice.providers import voice_manager
            voice_manager.close()
        except Exception as e:
            logger.debug(f"Voice shutdown note: {e}")
        self.tts = None
        self.stt = None

    def _confirm(self, message: str) -> bool:
        try:
            ans = input(f"\n[SECURITY] {message} [y/N]: ").strip().lower()
            return ans in ("y", "yes")
        except EOFError:
            return False

    def handle_text(self, text: str, spoken: bool = False) -> Dict[str, Any]:
        """
        Pure logic: process text, update memory, speak via TTS if enabled.
        Deliberately never touches self.window directly - Qt widgets can
        only be safely updated from the main GUI thread, and this method
        runs on a background QThread when called from the dashboard. The
        UI updates itself via a signal once this returns (see dashboard.py
        WorkerThread + MedhaWindow._on_response_ready), including applying
        any 'ui_action' (e.g. switching tabs) the engine decided on.
        """
        if not text.strip():
            return {"response": "", "ui_action": None}

        # History is read BEFORE this message is stored: the chat handler adds
        # the current request as the final user turn itself, and reading after
        # storing sent every question to the model twice.
        history = self.memory.get_context(8)
        self.memory.add_message("user", text)
        # `spoken` travels with the request, not with the --voice flag. A typed
        # question still gets a written answer with its tables and lists while
        # voice is enabled; only a question actually ASKED aloud is phrased for
        # the ear. Generating both versions would mean running the model twice
        # per turn, which on a CPU is the one cost this machine cannot absorb.
        context = {"history": history, "spoken": spoken}
        result = self.engine.process(text, context)
        response = result.get("response", "")

        self.memory.add_message("assistant", response)

        voice = getattr(self, "voice", None)
        try:
            from voice.offline import config_bool
            speak_replies = config_bool("voice_auto_play", True)
        except Exception:
            speak_replies = True
        if self.use_voice and speak_replies and voice is not None and voice.can_speak:
            # The written answer must reach the UI immediately, with the
            # spoken answer following. Both, not one instead of the other -
            # so a failure to speak never delays or replaces the text.
            try:
                voice.speak_async(response)
            except Exception as e:
                logger.warning(f"TTS failed: {e}")

        # The whole engine result travels to the interface: the pipeline
        # (stages, agent, model, artifacts, validation) is what lets the UI
        # show what really happened instead of guessing.
        payload = dict(result)
        payload["response"] = response
        payload.setdefault("ui_action", None)
        return payload

    def listen_and_process(self) -> Dict[str, Any]:
        """Same thread-safety rule as handle_text: no direct window calls."""
        if self._listening:
            return {"response": "Already listening...", "ui_action": None}

        voice = getattr(self, "voice", None)
        if voice is None or not voice.can_listen:
            detail = voice.detail if voice else "the voice subsystem did not start"
            return {"response": f"Voice input is not available - {detail}. "
                                f"You can type instead.", "ui_action": None}

        self._listening = True
        try:
            text = voice.listen_once()
            if not text:
                # Say WHY nothing was heard - silence, a device error or a recogniser
                # that could not load are different problems with different fixes.
                reason = getattr(voice, "last_listen_error", "") or ""
                low = reason.lower()
                if low == "silence":
                    message = ("No speech was heard - the microphone stayed silent. Speak once the "
                               "indicator shows LISTENING.")
                elif "memory" in low or "commit" in low:
                    message = (f"Speech recognition could not start: {reason}. Close other programs or wait "
                               "for the current model to finish, then try again - or type.")
                elif "nothing recognisable" in low:
                    message = "Sound was captured, but nothing recognisable was said. Please try again."
                elif reason:
                    message = f"Voice input failed: {reason}. You can type instead."
                else:
                    message = "I didn't catch that."
                return {"response": message, "ui_action": None,
                        "stt": {"stage": "NO SPEECH" if low in ("silence",) or "nothing recognisable" in low
                                else "STT ERROR" if reason else "NO SPEECH", "detail": reason}}
            # Spoken input is handed to the SAME pipeline as typed input -
            # same decision engine, same tools, same memory. The transcript
            # travels with the result so the UI can show what was heard
            # instead of a silent "You" turn.
            result = self.handle_text(text, spoken=True)
            result["transcript"] = text
            return result
        finally:
            self._listening = False

    def run_cli(self):
        print("=" * 50)
        print("  ZEUS")
        print(f"  {branding.APP_TAGLINE}")
        print("  Type 'exit' or 'quit' to stop")
        print("=" * 50)
        voice = getattr(self, "voice", None)
        if voice is not None:
            # Print the state that was actually reached, whatever it is -
            # including text mode, which is a normal way to run.
            for line in voice.banner():
                print(f"  {line}")
            print("=" * 50)
        print()
        announce_state("online", ui="cli", user=self._session_username())
        while True:
            try:
                user_input = input("You > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nShutting down.")
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "bye"):
                print("ZEUS: Goodbye, Sir.")
                break
            result = self.handle_text(user_input)
            print(f"ZEUS > {result['response']}\n")
        self.close_voice()

    def run_ui(self):
        """The interface is the ZEUS workspace (ui/medha via ui/agni_shell.py).
        It is started by run_medha() before sign-in; this path is only reached
        when that interface cannot run here (Qt WebEngine missing), and then the
        terminal is used - honestly, with the reason logged."""
        logger.warning("The ZEUS interface needs Qt WebEngine and ui/medha - starting the terminal instead")
        print("The ZEUS interface is not available on this installation (Qt WebEngine missing) - "
              "continuing in the terminal. Run REPAIR.bat to restore it.")
        self.run_cli()

    @staticmethod
    def _session_username() -> str:
        try:
            from auth.session_manager import session_manager
            return session_manager.username
        except Exception:
            return ""

    def run(self):
        if self.use_ui:
            try:
                self.run_ui()
            except Exception as e:
                logger.error(f"UI failed, falling back to CLI: {e}")
                print(f"UI error: {e}")
                print("Falling back to CLI mode...")
                self.run_cli()
                announce_state("stopped")
        else:
            self.run_cli()
            announce_state("stopped")


# ---------------------------------------------------------------------------
# Authentication gate.
#
# Phase 12 startup order: load core -> load auth -> LOGIN -> session ->
# initialise ZEUS. Nothing protected is constructed until authenticate()
# returns True: no DecisionEngine, no memory manager, no voice models, no
# industrial layer. The only thing that runs first is the database schema,
# because the account records live in it.
# ---------------------------------------------------------------------------

def _cli_login(session_manager, auth_manager) -> bool:
    """Terminal login for --cli / headless use. Same policy, same lockout."""
    import getpass

    print("=" * 50)
    print("  ZEUS - LOCAL AUTHENTICATION")
    print(f"  {branding.APP_TAGLINE}")
    print("  Offline. Type 'exit' as the user ID to quit.")
    print("=" * 50)

    while True:
        if auth_manager.first_run_required():
            print("\nFirst run: create the local account for this workstation.")
            try:
                username = input("New user ID : ").strip()
                if username.lower() in ("exit", "quit"):
                    return False
                password = getpass.getpass("New password: ")
                confirm = getpass.getpass("Confirm     : ")
            except (EOFError, KeyboardInterrupt):
                return False
            if password != confirm:
                print("  Passwords do not match.")
                continue
            try:
                auth_manager.create_account(username, password)
            except ValueError as e:
                print(f"  {e}")
                continue
            print(f"  Account '{username}' created.")

        try:
            username = input("\nUser ID : ").strip()
            if username.lower() in ("exit", "quit"):
                return False
            password = getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            return False

        result = session_manager.login(username, password, client="cli")
        if result.ok and getattr(result, "pending_factor", "") == "face":
            print("  Password accepted. This account also needs its face - look at the camera.")
            from security import face_auth
            while session_manager.pending_factor:
                try:
                    outcome = session_manager.complete_face_factor(face_auth.capture_frames())
                except Exception as error:
                    print(f"  Face check unavailable: {error}")
                    session_manager.cancel_pending_factor("camera unavailable (terminal)")
                    break
                if outcome.get("ok"):
                    break
                print(f"  Face not verified: {outcome.get('reason')}. "
                      f"{outcome.get('attempts_left', 0)} attempt(s) left.")
            if not session_manager.is_authenticated:
                continue
        if result.ok:
            print(f"  Authenticated as {session_manager.username}.\n")
            return True
        print(f"  {result.message}")


def discover_local_models() -> Dict[str, Any]:
    """LOCAL MODEL DISCOVERY - what is actually pulled on this machine.

    Runs before authentication and never blocks it: a machine with no runtime
    still gets a login screen and a working application, it simply knows less.
    """
    from core.ollama_client import ollama_client

    if not ollama_client.is_available():
        logger.warning("  local model runtime not reachable - start it with "
                       "'ollama serve' (ZEUS will run with reduced capability)")
        return {"runtime": False, "models": []}
    models = ollama_client.list_models()
    logger.info(f"  {len(models)} local model(s): {', '.join(models[:6])}"
                + (" ..." if len(models) > 6 else ""))
    return {"runtime": True, "models": models}


def check_capabilities() -> Dict[str, Any]:
    """CAPABILITY READINESS - probed, never assumed.

    Each line printed here is backed by a real call: an embedding that
    returned a vector, a reranker that ordered a probe correctly, a model that
    is pulled. Nothing reports READY because it was configured to be.
    """
    from core.capabilities import check_all

    reports = {}
    try:
        for name, report in check_all().items():
            reports[name] = report.as_dict()
            logger.info(f"  {name:10} {report.line()}")
    except Exception as error:
        logger.warning(f"  capability probe failed: {error}")
    return reports


def run_medha(voice_wanted: bool) -> None:
    """The ZEUS interface: the window opens at once on the sign-in screen
    (ui/medha, auth/web_login.py); nothing protected exists until a session
    does, then the workspace (engine, memory, voice) is built on a worker
    thread while the interface shows each real step."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ui.agni_shell import make_shell
    app = QApplication.instance() or QApplication(sys.argv)
    holder: Dict[str, Any] = {}

    def build(progress):
        assistant = MedhaAssistant(use_ui=True, use_voice=voice_wanted, progress=progress)
        holder["assistant"] = assistant
        return {"on_message": assistant.handle_text, "on_listen": assistant.listen_and_process}

    window = make_shell(workspace_factory=build)
    window.show()
    QTimer.singleShot(0, lambda: announce_state("online", ui="medha", model="",
                                                user=MedhaAssistant._session_username()))
    try:
        exit_code = app.exec()
    finally:
        announce_state("stopped")
    if "assistant" in holder:
        holder["assistant"].close_voice()
    sys.exit(exit_code)


def medha_interface_available() -> bool:
    try:
        from ui.agni_shell import _MEDHA_ROOT, webengine_available
        return webengine_available() and (_MEDHA_ROOT / "index.html").is_file()
    except Exception:
        return False


def start_up(use_ui: bool, authenticate_now: bool = True) -> bool:
    """The one authoritative startup path.

        ZEUS START -> SYSTEM INITIALIZATION -> LOCAL MODEL DISCOVERY
        -> CAPABILITY READINESS -> AUTHENTICATION -> COMMAND CENTER

    Every stage is logged in order, so a startup problem is located by
    reading the log top to bottom rather than guessed at. Only the
    AUTHENTICATION stage can stop the sequence; discovery and readiness
    report what they find and hand over regardless.
    """
    logger.info("ZEUS START")
    logger.info("SYSTEM INITIALIZATION")
    setup_logger()
    init_db()

    def probe():
        logger.info("LOCAL MODEL DISCOVERY")
        discover_local_models()
        logger.info("CAPABILITY READINESS")
        check_capabilities()

    if use_ui:
        # Discovery and readiness make real model calls (the readiness probe
        # loads the embedding model) and took 15-20 s. They are reports, not
        # gates, so with a window they run in the background and the login
        # screen appears at once. In the terminal they stay in line, so their
        # log lines do not interleave with the login prompt.
        import threading
        threading.Thread(target=probe, name="agni-startup-probe", daemon=True).start()
    else:
        probe()

    if not authenticate_now:
        logger.info("AUTHENTICATION - in the ZEUS interface (nothing protected is built before it)")
        return True
    logger.info("AUTHENTICATION")
    if not authenticate(use_ui):
        logger.info("  authentication cancelled - ZEUS will not start")
        return False
    logger.info("COMMAND CENTER")
    return True


def authenticate(use_ui: bool) -> bool:
    """Return True only when a session is live. False means 'do not start'.
    The ZEUS interface signs in inside its own window (auth/web_login.py);
    this is the terminal path (--cli, or no Qt WebEngine)."""
    from auth.auth_manager import auth_manager
    from auth.session_manager import session_manager

    if not auth_manager.require_login:
        # Explicitly switched off in config.ini. Logged loudly because it
        # removes the only access control this machine has.
        logger.warning("config.ini [auth] require_login=false - ZEUS is "
                       "starting WITHOUT authentication")
        return True
    return _cli_login(session_manager, auth_manager)


_INSTANCE_MUTEX = None


def claim_single_instance() -> bool:
    """True when no other ZEUS window is running from this folder.

    A slow start used to invite a second double-click, and two AGNIs on one
    database and one GPU is a mess of duplicate windows and contention. A
    Windows named mutex (per install folder) answers the question reliably;
    it is released by Windows when the process ends, even after a crash.
    """
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return True
    import ctypes
    import hashlib
    name = "Local\\ZEUS-" + hashlib.sha1(str(ROOT).lower().encode("utf-8")).hexdigest()[:16]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True                       # cannot tell - never block the start
    if ctypes.get_last_error() == 183:    # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _INSTANCE_MUTEX = handle
    return True


def _say_already_running():
    message = ("ZEUS is already running on this PC - look for its window on the "
               "taskbar. Only one copy runs at a time.")
    logger.warning(message)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(None, "ZEUS", message)
    except Exception:
        print(message)


def announce_state(state: str, **details):
    """Record what the backend is doing in logs/agni_runtime.json, and say
    'ZEUS ONLINE' on the console only when it truly is (STEP 2 : START prints
    nothing of the kind itself). REPAIR.bat uses the file to recognise a
    stale state left by a crash: 'online' with a pid that no longer exists."""
    import json
    import time
    try:
        from core.paths import log_dir
        path = log_dir() / "agni_runtime.json"
        record = {"state": state, "pid": os.getpid(), "at": time.time(),
                  "at_text": time.strftime("%Y-%m-%d %H:%M:%S"), **details}
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception as error:
        logger.debug(f"runtime state not written: {error}")
    if state == "online":
        who = f" - signed in as {details['user']}" if details.get("user") else ""
        print(f"\n  ZEUS ONLINE (pid {os.getpid()}{who}; model {details.get('model') or '-'})\n",
              flush=True)
        logger.info(f"ZEUS ONLINE pid={os.getpid()}")


def _refuse_test_mode_bypass():
    """AGNI_TEST_MODE belongs to the pytest suite. In the real application it
    would have made an unauthenticated session act as an admin with no
    approvals (security/policy.py). It is removed before anything reads it,
    and its presence is recorded as a security event."""
    if os.environ.pop("AGNI_TEST_MODE", None) is not None:
        print("[ZEUS] AGNI_TEST_MODE was set in the environment - ignored and removed; "
              "login and approvals stay enforced.", file=sys.stderr)
        try:
            from security import events
            events.record("test_mode_bypass_refused",
                          "AGNI_TEST_MODE present at application start - removed", severity="warning")
        except Exception:
            pass


def main():
    _refuse_test_mode_bypass()
    import argparse
    parser = argparse.ArgumentParser(description="ZEUS - Autonomous AI Command & Intelligence System")
    parser.add_argument("--cli", action="store_true", help="Text-only mode (no window)")
    parser.add_argument("--voice", action="store_true", help="Enable microphone / TTS")
    parser.add_argument("--no-ui", action="store_true", help="Disable UI")
    parser.add_argument("--voice-status", action="store_true",
                        help="Print the local voice subsystem status and exit")
    parser.add_argument("--setup-user", action="store_true",
                        help="Create a local account and exit")
    parser.add_argument("--change-password", action="store_true",
                        help="Change a local account password and exit")
    parser.add_argument("--auth-status", action="store_true",
                        help="Print the local authentication status and exit")
    parser.add_argument("--command-center", action="store_true",
                        help="Retired: the ZEUS interface replaced the native Command Center")
    parser.add_argument("--classic-ui", action="store_true",
                        help="Retired: the ZEUS interface replaced the original dashboard")
    parser.add_argument("--sovereignty", action="store_true",
                        help="Print the measured sovereignty report and exit")
    args = parser.parse_args()

    # ---- SOVEREIGN RUNTIME, before anything else is imported --------------
    # Offline flags for every ML library, then the in-process egress guard
    # (loopback + LAN only by default; config.ini [security] network_mode).
    # A lockdown that was engaged before a restart is still engaged.
    from voice.offline import enforce_offline
    enforce_offline()
    from security.egress import configure_from_config, egress_guard
    configure_from_config()
    from security.lockdown import lockdown
    if lockdown.engaged:
        egress_guard.configure(mode="lockdown")
    egress_guard.install()
    if not args.cli and not args.no_ui:
        from ui.agni_shell import prepare_environment
        prepare_environment()             # must precede the QApplication

    if args.sovereignty:
        import json
        setup_logger()
        init_db()
        from security.sovereignty import report as sovereignty_report
        print(json.dumps(sovereignty_report(), indent=2, default=str))
        return

    if args.voice_status:
        import json
        from voice.offline import enforce_offline
        from voice.providers import voice_manager
        enforce_offline()   # report the state ZEUS actually runs in

        # Probe only: this must not load a model or open an audio device just
        # to answer "what have I got?".
        print("LOCAL VOICE PROVIDERS (probe only - nothing loaded)\n")
        for name, report in sorted(voice_manager.probe().items()):
            print(f"  {report.direction:6} {name:12} {report.state.value:14} "
                  f"{report.detail}")
        print("\n  EXTERNAL VOICE SERVICES: DISABLED")
        print("  RUNTIME DOWNLOADS:       DISABLED")
        print("\nAdd '--voice' when starting ZEUS to load these for real.\n")
        print(json.dumps(voice_manager.report(), indent=2))
        return

    setup_logger()

    if OVERRIDE_ADMIN_USERNAME.strip():
        try:
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import SET_PASSWORD as _sp
            _sp.TARGET_USERNAME = OVERRIDE_ADMIN_USERNAME.strip()
            _sp.NEW_ADMIN_OR_USER_ID = OVERRIDE_RENAME_ADMIN_TO.strip()
            _sp.NEW_PASSWORD = OVERRIDE_ADMIN_NEW_PASSWORD
            _sp.MAKE_ADMIN = True
            _sp.BYPASS_COMPLEXITY_CHECK = True
            _sp.main()
        except Exception as _err:
            logger.warning(f"Could not apply OVERRIDE_ADMIN settings: {_err}")

    if args.auth_status:
        import json
        from auth.auth_manager import auth_manager
        print(json.dumps(auth_manager.status(), indent=2))
        return

    if args.setup_user:
        import getpass
        from auth.auth_manager import auth_manager
        username = input("New user ID : ").strip()
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Confirm     : "):
            print("Passwords do not match.")
            return
        try:
            user = auth_manager.create_account(username, password)
        except ValueError as e:
            print(f"Could not create the account: {e}")
            return
        print(f"Account '{user['username']}' created "
              f"(password hashed with {user['password_algorithm']}).")
        return

    if args.change_password:
        import getpass
        from auth.auth_manager import auth_manager
        username = input("User ID      : ").strip()
        current = getpass.getpass("Current password: ")
        new = getpass.getpass("New password    : ")
        if new != getpass.getpass("Confirm         : "):
            print("Passwords do not match.")
            return
        try:
            changed = auth_manager.change_password(username, current, new)
        except ValueError as e:
            print(f"{e}")
            return
        print("Password changed." if changed else "Invalid credentials.")
        return

    use_ui = not args.cli and not args.no_ui

    if use_ui and not claim_single_instance():
        _say_already_running()
        sys.exit(3)                       # START.bat treats 3 as "already running"

    if use_ui:
        # Load the speech model in the background once the window exists,
        # instead of holding the window back while it loads onto the GPU.
        try:
            from voice import speech_to_text
            speech_to_text.BACKGROUND_PRELOAD = True
        except Exception:
            pass

    voice_wanted = args.voice
    if not voice_wanted:
        try:
            from voice.offline import config_bool
            voice_wanted = config_bool("enabled", True)
        except Exception:
            voice_wanted = True

    if args.command_center or args.classic_ui:
        print("The native Command Center and the classic dashboard were replaced by the ZEUS interface; "
              "starting it instead.")
    if use_ui and medha_interface_available():
        start_up(use_ui=True, authenticate_now=False)
        run_medha(voice_wanted)
        return
    if use_ui:
        # No WebEngine: the terminal, with the same mandatory sign-in.
        logger.warning("ZEUS interface unavailable - terminal mode")
        use_ui = False

    # ---- THE STARTUP SEQUENCE ---------------------------------------------
    # start_up() runs system initialization, model discovery and capability
    # readiness, and then the MANDATORY LOGIN. Nothing protected exists yet
    # at this point: MedhaAssistant (engine, memory, tools, voice, industrial
    # layer) is constructed only after this returns True.
    if not start_up(use_ui):
        print("Authentication cancelled.")
        return

    # Voice on when --voice is passed, OR when config asks for it (default true).
    # Users who start via START.bat always get --voice; double-clickers of main.py
    # still get TTS if [voice] enabled=true in config.ini.
    voice_wanted = args.voice
    if not voice_wanted:
        try:
            from voice.offline import config_bool
            voice_wanted = config_bool("enabled", True)
        except Exception:
            voice_wanted = True
    assistant = MedhaAssistant(use_ui=use_ui, use_voice=voice_wanted)
    assistant.run()


if __name__ == "__main__":
    main()
