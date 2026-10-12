"""
The ZEUS interface host.

The ZEUS interface (ui/medha - sign-in and workspace) is displayed by Qt
WebEngine - Chromium, inside this process's window - and talks to the backend
through ONE object exposed over QWebChannel (`AgniBridge`). There is no web
server, no open port and no browser:

  * pages are served from the local ui/medha folder through a private `agni://`
    scheme; nothing else on disk is reachable from the page;
  * a request interceptor refuses every URL that is not agni:, qrc:, data: or
    blob: - the interface cannot load anything from a network even if a bug
    tried to;
  * the profile is off-the-record (no disk cache, no cookies), Chromium's own
    background networking is switched off by flags;
  * every bridge call is type-checked against ui/bridge_contract.py and, where
    marked, refused unless an authenticated, unlocked session exists;
  * the window opens on the sign-in screen; only the auth_* methods (and hello /
    state) answer before a session exists - auth/web_login.py decides every
    sign-in, the page only renders it;
  * the workspace (engine, memory, voice) is built after sign-in, on a worker
    thread, reporting each real step (`workspace` events);
  * LOCK and sign-out tear the page down (about:blank), then reload it at the
    sign-in screen - nothing the operator saw stays in memory behind the lock.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.logger import get_logger

logger = get_logger("ui.agni")

# The ZEUS interface (ui/medha) - the only interface this window serves.
_MEDHA_ROOT = Path(__file__).resolve().parent / "medha"
WEB_ROOT = _MEDHA_ROOT
SCHEME = b"agni"
_CHROMIUM_FLAGS = ("--disable-background-networking --disable-component-update "
                   "--disable-domain-reliability --disable-sync --no-pings "
                   "--disable-breakpad --dns-prefetch-disable --no-default-browser-check "
                   "--disable-features=AutofillServerCommunication,OptimizationHints,"
                   "MediaRouter,Translate,InterestFeedContentSuggestions,CertificateTransparencyComponentUpdater")


def prepare_environment() -> None:
    """Must run BEFORE the QApplication exists: Chromium flags and the custom
    scheme can only be set up before WebEngine initialises."""
    existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--disable-background-networking" not in existing:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (existing + " " + _CHROMIUM_FLAGS).strip()
    cdp = os.environ.get("AGNI_E2E_CDP_PORT", "").strip()
    if cdp.isdigit():
        # Test harness only (Playwright over CDP, loopback). Never set by START.bat.
        os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = f"127.0.0.1:{cdp}"
        logger.warning(f"E2E test mode: remote debugging on 127.0.0.1:{cdp}")
    try:
        from PySide6.QtWebEngineCore import QWebEngineUrlScheme
        if QWebEngineUrlScheme.schemeByName(SCHEME).name() != SCHEME:
            scheme = QWebEngineUrlScheme(SCHEME)
            scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
            scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme
                            | QWebEngineUrlScheme.Flag.LocalScheme
                            | QWebEngineUrlScheme.Flag.LocalAccessAllowed)
            QWebEngineUrlScheme.registerScheme(scheme)
    except Exception as error:
        logger.warning(f"agni:// scheme not registered: {error}")


def webengine_available() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
        import PySide6.QtWebChannel  # noqa: F401
        return (WEB_ROOT / "index.html").is_file()
    except Exception:
        return False


BUSY_REPLY = {"response": "Still working on the previous request - this one was not started. "
                          "Send it again when the answer arrives.", "busy": True}


def voice_summary_from(report: Dict[str, Any]) -> Dict[str, Any]:
    """The voice line the interface shows. It must never lie: speech is offered
    only when a provider has loaded, and GPU is named only when that provider
    reported a CUDA device after loading - never from configuration."""
    output = report.get("output") or {}
    can_speak = bool(output.get("available"))
    device = str((output.get("evidence") or {}).get("device", "")).lower() if can_speak else ""
    return {"state": report.get("state"), "detail": report.get("detail"),
            "can_listen": bool((report.get("input") or {}).get("available")),
            "can_speak": can_speak,
            "input": (report.get("input") or {}).get("provider"),
            "output": output.get("provider"),
            "device": {"cuda": "GPU", "cpu": "CPU", "mps": "MPS"}.get(device)}


# The rest of this module needs Qt; it is imported lazily by run_ui().

def _build_classes():
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QUrl, Qt, Signal, Slot
    from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                         QWebEngineUrlRequestInterceptor,
                                         QWebEngineUrlSchemeHandler, QWebEngineUrlRequestJob)

    class SchemeHandler(QWebEngineUrlSchemeHandler):
        """Serves ui/medha over agni://app/... - and nothing outside it."""

        def requestStarted(self, job: QWebEngineUrlRequestJob):
            url = job.requestUrl()
            relative = url.path().lstrip("/") or "index.html"
            if relative == "js/qwebchannel.js":
                # Qt's own channel client, read from Qt's resources so it always
                # matches the installed Qt version.
                from PySide6.QtCore import QFile
                resource = QFile(":/qtwebchannel/qwebchannel.js")
                if resource.open(QIODevice.OpenModeFlag.ReadOnly):
                    buffer = QBuffer(parent=job)
                    buffer.setData(resource.readAll())
                    resource.close()
                    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                    job.reply(QByteArray(b"text/javascript"), buffer)
                    return
            target = (WEB_ROOT / relative).resolve()
            try:
                target.relative_to(WEB_ROOT.resolve())
            except ValueError:
                job.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
                return
            if not target.is_file():
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".js":
                mime = "text/javascript"
            elif target.suffix == ".woff2":
                mime = "font/woff2"
            buffer = QBuffer(parent=job)
            buffer.setData(QByteArray(target.read_bytes()))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            job.reply(QByteArray(mime.encode()), buffer)

    class Interceptor(QWebEngineUrlRequestInterceptor):
        """Refuse every request that would leave this machine."""

        ALLOWED = {"agni", "qrc", "data", "blob", "about"}

        def interceptRequest(self, info):
            scheme = info.requestUrl().scheme().lower()
            if scheme not in self.ALLOWED:
                info.block(True)
                try:
                    from security import events
                    events.record("ui_request_blocked", info.requestUrl().toString()[:200],
                                  severity="warning")
                except Exception:
                    pass

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):
            logger.debug(f"[web] {message} ({source}:{line})")

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            # The page may only ever show the ZEUS interface.
            return url.scheme() in ("agni", "about")

        def createWindow(self, _type):
            return None                       # no pop-ups, no new windows

        def featurePermissionRequested(self, origin, feature):
            # The interface never needs the camera, location or notifications.
            # Microphone capture is done by the Python voice stack, not the page.
            self.setFeaturePermission(origin, feature,
                                      QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)

    class AgniBridge(QObject):
        """The one object the page can talk to. See ui/bridge_contract.py."""

        reply = Signal(int, str)          # request id, JSON {"ok": ..., "result"|"error": ...}
        event = Signal(str, str)          # kind, JSON payload
        _deliver_reply = Signal(int, str)
        _deliver_event = Signal(str, str)

        def __init__(self, shell):
            super().__init__()
            self.shell = shell
            self._deliver_reply.connect(self.reply, Qt.ConnectionType.QueuedConnection)
            self._deliver_event.connect(self.event, Qt.ConnectionType.QueuedConnection)
            self._history = []
            self._speak = True
            # One request through the engine at a time. A second one while the
            # first runs is answered 'busy' - never silently dropped, never run
            # alongside it (the page guards too; this holds if the page does not).
            self._engine = threading.Lock()

        # -- thread-safe emission -------------------------------------------------

        def push(self, kind: str, payload: Any) -> None:
            try:
                self._deliver_event.emit(kind, json.dumps(payload, default=str))
            except RuntimeError:
                pass                              # the page is being torn down

        def _answer(self, request_id: int, ok: bool, result: Any) -> None:
            body = {"ok": ok, ("result" if ok else "error"): result}
            try:
                self._deliver_reply.emit(request_id, json.dumps(body, default=str))
            except RuntimeError:
                pass

        # -- the single entry point -------------------------------------------------

        @Slot(int, str, str)
        def request(self, request_id: int, method: str, payload_json: str):
            from ui import bridge_contract
            try:
                payload = json.loads(payload_json or "{}")
                args = bridge_contract.validate(method, payload)
            except (ValueError, bridge_contract.ContractError) as error:
                self._answer(request_id, False, str(error))
                return
            _spec, needs_auth, thread = bridge_contract.METHODS[method]
            if needs_auth and not self._authenticated():
                self._answer(request_id, False, "ZEUS is locked - authentication required")
                return
            if needs_auth:
                try:                               # operator activity resets the idle timer
                    from auth.session_manager import session_manager
                    session_manager.touch()
                except Exception:
                    pass
            handler = getattr(self, f"_m_{method}")
            if thread == "main":
                try:
                    self._answer(request_id, True, handler(**args))
                except Exception as error:
                    logger.exception(f"bridge {method} failed")
                    self._answer(request_id, False, str(error))
                return

            def work():
                try:
                    self._answer(request_id, True, handler(**args))
                except Exception as error:
                    logger.exception(f"bridge {method} failed")
                    self._answer(request_id, False, str(error))

            threading.Thread(target=work, name=f"agni-ui-{method}", daemon=True).start()

        @staticmethod
        def _authenticated() -> bool:
            try:
                from security.policy import current_identity
                return current_identity().authenticated
            except Exception:
                return False

        # -- methods ------------------------------------------------------------------

        def _m_hello(self):
            from core import branding
            from security.policy import current_identity
            from ui import bridge_contract
            identity = current_identity()
            voice = self.shell.voice_summary()
            return {"app": branding.APP_NAME, "tagline": branding.APP_TAGLINE,
                    "version": branding.APP_VERSION, "user": identity.username,
                    "role": identity.role, "authenticated": identity.authenticated,
                    "voice": voice, "speak": self._speak, "contract": bridge_contract.describe()}

        def _m_state(self):
            from core.assistant_state import assistant_state
            return assistant_state.snapshot()

        def _m_status(self):
            return self.shell.system_status()

        def _m_submit(self, text: str):
            text = text.strip()
            if not text:
                return {"response": ""}
            from security.policy import origin_scope
            if not self._engine.acquire(blocking=False):
                return dict(BUSY_REPLY)
            try:
                with origin_scope("command"):
                    result = self.shell.on_message(text) if self.shell.on_message else {
                        "response": "No engine is connected."}
            finally:
                self._engine.release()
            record = {"ts": time.time(), "request": text, "result": result}
            self._history.append(record)
            del self._history[:-60]
            return result

        def _m_listen(self):
            if not self.shell.on_listen:
                return {"response": "Voice input is not connected."}
            if not self._engine.acquire(blocking=False):
                return dict(BUSY_REPLY)
            try:
                result = self.shell.on_listen()
            finally:
                self._engine.release()
            if isinstance(result, dict) and result.get("transcript"):
                self._history.append({"ts": time.time(), "request": result["transcript"],
                                      "result": result, "spoken": True})
            return result

        def _m_history(self):
            return self._history[-30:]

        def _m_stop_speaking(self):
            try:
                from voice.providers import voice_manager
                voice_manager.stop()
                return {"stopped": True}
            except Exception as error:
                return {"stopped": False, "detail": str(error)}

        def _m_set_speak(self, enabled: bool):
            self._speak = bool(enabled)
            self.shell.set_speak(self._speak)
            return {"speak": self._speak}

        def _m_tool(self, name: str, args: dict):
            from core.tool_registry import registry
            from security.policy import origin_scope
            with origin_scope("ui"):
                outcome = registry.execute(name, **args)
            return {"success": outcome.success, "output": outcome.output,
                    "error": outcome.error, "ms": round(outcome.duration_ms)}

        def _m_resolve_approval(self, id: int, approved: bool):
            from security.approvals import approval_gateway
            from auth.session_manager import session_manager
            done = approval_gateway.resolve(id, approved, by=session_manager.username or "operator")
            return {"resolved": done}

        def _m_lock(self):
            self.shell.request_lock(False)
            return {"locked": True}

        def _m_logout(self):
            self.shell.request_lock(True)
            return {"logged_out": True}

        def _m_unlock_vault(self):
            return self.shell.unlock_vault_dialog()

        def _m_pick_file(self, purpose: str):
            from PySide6.QtWidgets import QFileDialog
            filters = {"document": "Documents (*.pdf *.docx *.xlsx *.xlsm *.csv *.pptx *.txt *.md "
                                   "*.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
                       "image": "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)",
                       "audio": "Audio (*.wav *.mp3 *.m4a *.flac *.ogg *.webm *.mp4)",
                       "text": "Text (*.txt *.md)",
                       "csv": "CSV (*.csv)"}.get(purpose, "All files (*.*)")
            path, _ = QFileDialog.getOpenFileName(self.shell, "Choose a file", str(Path.home()), filters)
            return {"path": path or ""}

        def _m_pick_folder(self, purpose: str):
            from PySide6.QtWidgets import QFileDialog
            path = QFileDialog.getExistingDirectory(self.shell, "Choose a folder", str(Path.home()))
            return {"path": path or ""}

        def _m_open_path(self, path: str):
            from core.tool_registry import registry
            from security.policy import origin_scope
            with origin_scope("ui"):
                outcome = registry.execute("open_document", name_or_path=path)
            return {"success": outcome.success, "output": outcome.output, "error": outcome.error}

        def _m_probe_network(self):
            from security.egress import environment_probe
            from security.audit_chain import audit_chain
            result = environment_probe()
            audit_chain.append("network_probe", json.dumps(result))
            return result

        def _m_create_user(self):
            return self.shell.create_user_dialog()

        def _m_change_password(self):
            return self.shell.change_password_dialog()

        def _m_telemetry(self):
            from core.tool_registry import registry
            from security.policy import origin_scope
            with origin_scope("ui"):
                outcome = registry.execute("plant_telemetry")
            return outcome.output if outcome.success else {"error": outcome.error}

        # -- ZEUS sign-in (auth/web_login.py decides; the page only renders) -------

        def _signed_in_after(self, result: Dict[str, Any]) -> Dict[str, Any]:
            if result.get("ok") and not result.get("pending_factor"):
                self.shell.on_signed_in()
            return result

        def _m_auth_state(self):
            state = self.shell.auth.state()
            state["workspace"] = self.shell.workspace_snapshot()
            return state

        def _m_auth_setup(self, username: str, password: str, confirm: str):
            return self._signed_in_after(self.shell.auth.setup(username, password, confirm))

        def _m_auth_login(self, username: str, password: str):
            return self._signed_in_after(self.shell.auth.login(username, password))

        def _m_auth_unlock(self, password: str):
            return self._signed_in_after(self.shell.auth.unlock(password))

        def _face_event(self, kind: str, payload: Dict[str, Any]) -> None:
            self.push(kind, payload)
            if payload.get("state") == "ACCESS GRANTED":
                self.shell.on_signed_in()

        def _m_auth_face(self, purpose: str):
            return self.shell.auth.face_start(purpose, self._face_event)

        def _m_auth_face_cancel(self):
            return self.shell.auth.face_cancel()

        def _m_auth_cancel_factor(self):
            return self.shell.auth.cancel_factor()

        def _m_auth_switch_user(self):
            return self.shell.auth.switch_user()

        def _m_quit(self):
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            QTimer.singleShot(0, QApplication.quit)
            return {"quitting": True}

        # -- ZEUS workspace ----------------------------------------------------------

        def _m_workspace(self):
            return self.shell.workspace_snapshot()

        def _m_ask_company(self, question: str):
            """Company intelligence with LIVE stages: the same policy check as the
            company_intelligence tool, then the pipeline, pushing each stage."""
            from knowledge_fabric import service
            from knowledge_fabric.tools import analysis_summary
            from security import policy
            decision = policy.authorize("company_intelligence", 0, {"question": question}, origin="ui")
            if not decision.allowed:
                return {"ok": False, "error": decision.reason}
            obj = service.analyze(question, progress=lambda event: self.push("analysis", event),
                                  make_artifacts=False)
            return analysis_summary(obj)

        def _m_vault_open(self, passphrase: str, confirm: str):
            from core.vault import VaultError, vault
            from memory.database import log_vault_event
            configured = vault.is_configured()
            try:
                if not configured:
                    if passphrase != confirm:
                        return {"ok": False, "reason": "The passphrases do not match."}
                    if len(passphrase) < 10:
                        return {"ok": False, "reason": "Use at least 10 characters - there is no way to recover it."}
                    vault.create(passphrase)
                    log_vault_event("create", succeeded=True)
                    return {"ok": True, "created": True}
                if vault.unlock(passphrase):
                    log_vault_event("unlock", succeeded=True)
                    return {"ok": True}
                log_vault_event("unlock", detail="wrong passphrase", succeeded=False)
                try:
                    from security import events
                    events.record("vault_unlock_failed", "wrong passphrase", severity="notice")
                except Exception:
                    pass
                return {"ok": False, "reason": "That passphrase is not correct."}
            except VaultError as error:
                return {"ok": False, "reason": str(error)}

        def _m_account_password(self, current: str, new: str, confirm: str):
            from auth.auth_manager import auth_manager
            from auth.session_manager import session_manager
            if new != confirm:
                return {"ok": False, "reason": "The new passwords do not match."}
            try:
                changed = auth_manager.change_password(session_manager.username, current, new)
            except ValueError as error:
                return {"ok": False, "reason": str(error)}
            return {"ok": bool(changed), "reason": "" if changed else "The current password was not correct."}

        def _m_account_create(self, username: str, password: str, confirm: str, role: str):
            from security.policy import current_identity
            if current_identity().role != "admin":
                return {"ok": False, "reason": "Only an administrator can create accounts."}
            if role not in ("viewer", "operator", "engineer", "admin"):
                return {"ok": False, "reason": "Unknown role."}
            if password != confirm:
                return {"ok": False, "reason": "The passwords do not match."}
            from auth.auth_manager import auth_manager
            from memory import database
            try:
                user = auth_manager.create_account(username.strip(), password, is_admin=(role == "admin"))
                database.set_user_role(user["id"], role)
            except ValueError as error:
                return {"ok": False, "reason": str(error)}
            return {"ok": True, "username": user["username"], "role": role}

        # -- ZEUS NETWORK (Connection Mode in the user menu) ------------------------

        @staticmethod
        def _crudex(fn, *args):
            """A refusal comes back as {"ok": False, "error": reason} - shown in the
            menu, never hidden. No argument (the API key included) is ever put in
            a log line or an error message."""
            from crudex_net import service
            try:
                result = fn(*args)
                return {"ok": True, **result}
            except service.Refused as error:
                return {"ok": False, "error": str(error)}
            except Exception as error:                       # nothing here may echo an argument
                logger.warning(f"ZEUS NETWORK action failed: {type(error).__name__}")
                return {"ok": False, "error": f"{type(error).__name__}: the action did not complete"}

        def _m_crudex_state(self):
            from crudex_net import service
            return self._crudex(service.state)

        def _m_crudex_set_mode(self, mode: str):
            from crudex_net import service
            if mode not in ("online", "offline"):
                return {"ok": False, "error": "mode is 'online' or 'offline'"}
            return self._crudex(service.set_mode, mode)

        def _m_crudex_save_key(self, api_key: str):
            from crudex_net import service
            return self._crudex(service.save_key, api_key)

        def _m_crudex_remove_key(self):
            from crudex_net import service
            return self._crudex(service.remove_key)

        def _m_crudex_connect(self):
            from crudex_net import service
            return self._crudex(service.connect)

        def _m_crudex_disconnect(self):
            from crudex_net import service
            return self._crudex(service.disconnect)

        def _m_crudex_set_server(self, url: str, ca_file: str):
            from crudex_net import service
            return self._crudex(service.set_server, url, ca_file)

    return SchemeHandler, Interceptor, Page, AgniBridge


def run_ui(on_message=None, on_listen=None, model_name: str = "", on_lock=None,
           username: str = "", workspace_factory=None):
    """Same signature as the other interfaces' run_ui, so main.py can choose."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    window = make_shell(on_message=on_message, on_listen=on_listen, on_lock=on_lock,
                        username=username, workspace_factory=workspace_factory)
    window.show()
    return app, window


def make_shell(on_message=None, on_listen=None, on_lock=None, username: str = "",
               workspace_factory=None):
    """workspace_factory(progress) -> {"on_message", "on_listen"}: the ZEUS
    start-up. The window opens on the sign-in screen; the workspace (engine,
    memory, voice) is built on a worker thread only after a session exists,
    reporting each real step as a `workspace` event."""
    from PySide6.QtCore import QTimer, QUrl, Qt, Signal
    from PySide6.QtGui import QColor, QIcon
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QMainWindow

    SchemeHandler, Interceptor, Page, AgniBridge = _build_classes()

    class AgniShell(QMainWindow):
        # A lock can start on any thread (the idle timeout is detected inside
        # a policy check on a worker); the interface is torn down on the GUI
        # thread through this queued signal.
        lock_requested = Signal(bool)

        def __init__(self):
            super().__init__()
            self._locking = False
            self.lock_requested.connect(self._do_lock, Qt.ConnectionType.QueuedConnection)
            from core import branding
            self.on_message = on_message
            self.on_listen = on_listen
            self.on_lock = on_lock
            self.setWindowTitle(f"{branding.APP_NAME} · {branding.APP_TAGLINE}")
            self.setMinimumSize(1180, 720)
            self.resize(1520, 920)
            self.setStyleSheet("QMainWindow { background: #000000; }")
            icon = WEB_ROOT / "img" / "medha.ico"
            if icon.is_file():
                self.setWindowIcon(QIcon(str(icon)))

            self.profile = QWebEngineProfile(self)           # off-the-record
            self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            self.profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            self.profile.setSpellCheckEnabled(False)
            self._scheme_handler = SchemeHandler(self)
            self.profile.installUrlSchemeHandler(SCHEME, self._scheme_handler)
            self._interceptor = Interceptor(self)
            self.profile.setUrlRequestInterceptor(self._interceptor)

            self.view = QWebEngineView(self)
            self.page = Page(self.profile, self.view)
            self.page.setBackgroundColor(QColor("#000000"))
            settings = self.page.settings()
            for attribute, value in (
                    (QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False),
                    (QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False),
                    (QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False),
                    (QWebEngineSettings.WebAttribute.PluginsEnabled, False),
                    (QWebEngineSettings.WebAttribute.PdfViewerEnabled, False),
                    (QWebEngineSettings.WebAttribute.DnsPrefetchEnabled, False),
                    (QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, False)):
                try:
                    settings.setAttribute(attribute, value)
                except Exception:
                    pass
            self.view.setPage(self.page)
            self.setCentralWidget(self.view)

            from auth.web_login import AuthController
            self.auth = AuthController()
            self.workspace_factory = workspace_factory
            self._workspace = {"state": "ready" if (on_message and not workspace_factory) else "not_started",
                               "steps": [], "detail": ""}
            self._workspace_lock = threading.Lock()

            self.bridge = AgniBridge(self)
            self.channel = QWebChannel(self.page)
            self.channel.registerObject("agni", self.bridge)
            self.page.setWebChannel(self.channel)

            self._subscribe()
            self.load()

        # -- page lifecycle ---------------------------------------------------

        def load(self):
            self.page.setUrl(QUrl("agni://app/index.html"))

        def blank(self):
            """Tear the interface down: nothing the operator saw stays in memory
            behind the lock screen - including Hermes' working memory of this session."""
            self.page.setUrl(QUrl("about:blank"))
            try:
                from memory import hermes_context
                hermes_context.forget_session()
            except Exception:
                pass

        # -- subscriptions: backend -> page ---------------------------------------

        def _subscribe(self):
            from core.assistant_state import assistant_state
            assistant_state.subscribe(self._on_state)
            try:
                from voice.speech_to_text import subscribe_level, subscribe_stage
                subscribe_level(self._on_level)
                subscribe_stage(self._on_stt_stage)
            except Exception:
                pass
            from security import events
            events.subscribe(self._on_security)
            from security.approvals import approval_gateway
            approval_gateway.attach(async_handler=self._on_approval,
                                    sync_handler=self._approval_dialog)
            from security.lockdown import lockdown
            lockdown.subscribe(self._on_lockdown)
            try:
                from auth.session_manager import session_manager
                session_manager.on_change(self._on_session)
            except Exception:
                pass
            self._events_timer = QTimer(self)
            self._events_timer.timeout.connect(self._drain_background_events)
            self._events_timer.start(2000)
            # The ZEUS NETWORK client: it stays OFFLINE until someone is signed in
            # AND Online mode is chosen with a saved key; it disconnects on lock and
            # sign-out by itself (session events). Its events are shown live.
            try:
                from crudex_net.client import client as crudex_client
                crudex_client.subscribe(lambda event: self.bridge.push("crudex", event))
                crudex_client.start()
            except Exception as error:
                logger.warning(f"ZEUS NETWORK client not started: {error}")
            try:
                from voice.providers import voice_manager
                provider = voice_manager.output_provider()
                if provider is not None:
                    provider.on_state_change(self._on_speech)
            except Exception:
                pass

        def _on_speech(self, provider, state):
            """Speech output state -> assistant state SPEAKING (real playback only)."""
            from core.assistant_state import State, assistant_state
            speaking = str(state).lower() in ("speaking", "generating", "preparing")
            assistant_state.clear(State.SPEAKING)
            if speaking:
                assistant_state.push(State.SPEAKING, f"speaking ({provider})")
            self.bridge.push("speech", {"provider": provider, "state": str(state)})

        def _on_state(self, snapshot):
            self.bridge.push("state", snapshot)

        _last_level = 0.0

        def _on_stt_stage(self, stage, detail=""):
            """The recogniser's real stage (voice/speech_to_text.py) -> the MIC indicator."""
            self.bridge.push("stt", {"stage": stage, "detail": str(detail or "")[:240]})

        def _on_level(self, level):
            now = time.time()
            if now - self._last_level > 0.04:               # ~25 updates/second max
                self._last_level = now
                self.bridge.push("level", {"level": round(min(1.0, level * 6), 3)})

        def _on_security(self, event):
            self.bridge.push("security", event)

        def _on_approval(self, request):
            self.bridge.push("approval", request.as_dict())

        def _approval_dialog(self, request) -> bool:
            from PySide6.QtWidgets import QMessageBox
            answer = QMessageBox.question(self, "ZEUS - approval required",
                                          f"{request.description}\n\nRisk: {request.risk}\n\nAllow this action?")
            return answer == QMessageBox.StandardButton.Yes

        def _on_lockdown(self, status):
            self.bridge.push("lockdown", status)
            if status.get("engaged"):
                self.lock_requested.emit(False)

        def _on_session(self, event, _session):
            self.bridge.push("session", {"event": event})
            if event in ("lock", "logout") and not self._locking:
                # Locked from elsewhere (idle timeout, lockdown): the interface
                # must go too - not stay on screen over a locked session.
                self.lock_requested.emit(event == "logout")

        def _drain_background_events(self):
            try:
                from core.background_events import pop_all_events
                for timestamp, message in pop_all_events():
                    self.bridge.push("notification", {"ts": timestamp, "message": message})
            except Exception:
                pass

        # -- the workspace, built after sign-in --------------------------------------

        def workspace_snapshot(self) -> Dict[str, Any]:
            with self._workspace_lock:
                return {"state": self._workspace["state"], "detail": self._workspace["detail"],
                        "steps": list(self._workspace["steps"])}

        def on_signed_in(self):
            """A session now exists: build the workspace once (per process)."""
            with self._workspace_lock:
                state = self._workspace["state"]
                if state in ("building", "ready") or self.workspace_factory is None:
                    snapshot = dict(self._workspace)
                    start = False
                else:
                    self._workspace.update(state="building", steps=[], detail="")
                    start = True
            if not start:
                self.bridge.push("workspace", {"state": snapshot["state"], "steps": snapshot["steps"]})
                return
            threading.Thread(target=self._build_workspace, name="medha-workspace", daemon=True).start()

        def _build_workspace(self):
            def progress(step: str, state: str, detail: str = ""):
                with self._workspace_lock:
                    steps = [s for s in self._workspace["steps"] if s["step"] != step]
                    steps.append({"step": step, "state": state, "detail": detail})
                    self._workspace["steps"] = steps
                self.bridge.push("workspace", {"state": "building", "step": step, "step_state": state,
                                               "detail": detail})
            try:
                callbacks = self.workspace_factory(progress)
                self.on_message = callbacks.get("on_message")
                self.on_listen = callbacks.get("on_listen")
                with self._workspace_lock:
                    self._workspace.update(state="ready", detail="")
                self.bridge.push("workspace", {"state": "ready"})
            except Exception as error:
                logger.exception("workspace start-up failed")
                with self._workspace_lock:
                    self._workspace.update(state="failed", detail=str(error))
                self.bridge.push("workspace", {"state": "failed", "detail": str(error)})

        # -- services the bridge uses -----------------------------------------------

        def voice_summary(self) -> Dict[str, Any]:
            try:
                from voice.providers import voice_manager
                return voice_summary_from(voice_manager.report())
            except Exception as error:
                return {"state": "unavailable", "detail": str(error),
                        "can_listen": False, "can_speak": False, "device": None}

        def set_speak(self, enabled: bool):
            try:
                from voice import offline
                offline.voice_config()["voice_auto_play"] = "true" if enabled else "false"
            except Exception:
                pass
            self._speak_enabled = enabled

        def system_status(self) -> Dict[str, Any]:
            status: Dict[str, Any] = {"ts": time.time()}
            try:
                import psutil
                memory = psutil.virtual_memory()
                status["cpu"] = psutil.cpu_percent(interval=None)
                status["ram_used_gb"] = round((memory.total - memory.available) / 1024 ** 3, 1)
                status["ram_total_gb"] = round(memory.total / 1024 ** 3, 1)
            except Exception:
                pass
            try:
                from core.hardware import profile
                gpu = profile().nvidia
                status["gpu"] = ({"name": gpu.name, "vram_gb": gpu.vram_total_gb,
                                  "vram_free_gb": gpu.vram_free_gb} if gpu else None)
            except Exception:
                status["gpu"] = None
            try:
                from core.ollama_client import ollama_client
                up = ollama_client.is_available()
                status["runtime"] = {"up": up, "models": len(ollama_client.list_models()) if up else 0}
            except Exception:
                status["runtime"] = {"up": False, "models": 0}
            try:
                from core.orchestration.router import capability_router
                status["resident_models"] = list(capability_router.loaded_models())
            except Exception:
                status["resident_models"] = []
            from security.egress import egress_guard
            from security.lockdown import lockdown
            from core.vault import vault
            net = egress_guard.status()
            status["network"] = {"mode": net["mode"], "installed": net["installed"],
                                 "external_allowed": net["external_egress_allowed"],
                                 "external_blocked": net["external_egress_blocked"]}
            status["lockdown"] = lockdown.engaged
            status["vault"] = {"configured": vault.is_configured(), "unlocked": vault.is_unlocked}
            from security.approvals import approval_gateway
            status["approvals_pending"] = len(approval_gateway.pending())
            status["voice"] = self.voice_summary()
            try:
                from core.orchestration import activity
                status["pipeline"] = activity.current() or None
                recent = activity.recent(1)
                status["last_run"] = recent[0] if recent else None
            except Exception:
                status["pipeline"] = None
            return status

        def request_lock(self, logout: bool):
            if self.on_lock is None:
                # ZEUS sign-in: the session manager decides; its lock / logout
                # event reloads the page at the sign-in screen (_on_session).
                from auth.session_manager import session_manager
                if logout:
                    self.auth.face_cancel()
                    session_manager.logout()
                else:
                    session_manager.lock("user request")
                return
            self.lock_requested.emit(bool(logout))

        def _do_lock(self, logout: bool):
            if self.on_lock is None:
                # Tear the workspace out of memory: the page is replaced, then
                # reloaded - it starts at the sign-in screen for this state.
                self.blank()
                QTimer.singleShot(50, self.load)
                return
            if self._locking:
                return
            self._locking = True
            self.blank()
            try:
                self.on_lock(switch_user=logout)
            finally:
                self._locking = False
                if self.isVisible():
                    self.load()

        def unlock_vault_dialog(self) -> Dict[str, Any]:
            # Kept for callers of the original bridge method; the ZEUS interface
            # uses the in-page `vault_open` form instead.
            from core.vault import VaultError, vault
            from memory.database import log_vault_event
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            configured = vault.is_configured()
            passphrase, ok = QInputDialog.getText(
                self, "Unlock the vault" if configured else "Set the vault passphrase",
                "Enter your vault passphrase." if configured else
                "At least 10 characters. There is no way to recover it.", QLineEdit.EchoMode.Password)
            if not ok:
                return {"ok": False, "reason": "cancelled"}
            if not configured:
                again, ok = QInputDialog.getText(self, "Set the vault passphrase", "Confirm the passphrase:",
                                                 QLineEdit.EchoMode.Password)
                if not ok or again != passphrase:
                    return {"ok": False, "reason": "the passphrases did not match"}
                if len(passphrase) < 10:
                    return {"ok": False, "reason": "use at least 10 characters"}
            try:
                if not configured:
                    vault.create(passphrase)
                    log_vault_event("create", succeeded=True)
                    return {"ok": True, "created": True}
                if vault.unlock(passphrase):
                    log_vault_event("unlock", succeeded=True)
                    return {"ok": True}
                log_vault_event("unlock", detail="wrong passphrase", succeeded=False)
                try:
                    from security import events
                    events.record("vault_unlock_failed", "wrong passphrase", severity="notice")
                except Exception:
                    pass
                return {"ok": False, "reason": "That passphrase is not correct."}
            except VaultError as error:
                return {"ok": False, "reason": str(error)}

        def create_user_dialog(self) -> Dict[str, Any]:
            from security.policy import current_identity
            if current_identity().role != "admin":
                return {"ok": False, "reason": "only an administrator can create accounts"}
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            username, ok = QInputDialog.getText(self, "New ZEUS account", "User ID:")
            if not ok or not username.strip():
                return {"ok": False, "reason": "cancelled"}
            password, ok = QInputDialog.getText(self, "New ZEUS account",
                                                f"Password for {username} (min 8 characters):",
                                                QLineEdit.EchoMode.Password)
            if not ok:
                return {"ok": False, "reason": "cancelled"}
            again, ok = QInputDialog.getText(self, "New ZEUS account", "Confirm password:",
                                             QLineEdit.EchoMode.Password)
            if not ok or again != password:
                return {"ok": False, "reason": "the passwords did not match"}
            role, ok = QInputDialog.getItem(self, "New ZEUS account", "Role:",
                                            ["operator", "engineer", "viewer", "admin"], 0, False)
            if not ok:
                return {"ok": False, "reason": "cancelled"}
            from auth.auth_manager import auth_manager
            from memory import database
            try:
                user = auth_manager.create_account(username.strip(), password, is_admin=(role == "admin"))
                database.set_user_role(user["id"], role)
            except ValueError as error:
                return {"ok": False, "reason": str(error)}
            return {"ok": True, "username": user["username"], "role": role}

        def change_password_dialog(self) -> Dict[str, Any]:
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            from auth.auth_manager import auth_manager
            from auth.session_manager import session_manager
            username = session_manager.username
            current, ok = QInputDialog.getText(self, "Change password", "Current password:",
                                               QLineEdit.EchoMode.Password)
            if not ok:
                return {"ok": False, "reason": "cancelled"}
            new, ok = QInputDialog.getText(self, "Change password", "New password (min 8):",
                                           QLineEdit.EchoMode.Password)
            if not ok:
                return {"ok": False, "reason": "cancelled"}
            again, ok = QInputDialog.getText(self, "Change password", "Confirm new password:",
                                             QLineEdit.EchoMode.Password)
            if not ok or again != new:
                return {"ok": False, "reason": "the new passwords did not match"}
            try:
                changed = auth_manager.change_password(username, current, new)
            except ValueError as error:
                return {"ok": False, "reason": str(error)}
            return {"ok": bool(changed), "reason": "" if changed else "the current password was wrong"}

        # -- compatibility with main.py's calls on the Command Center --------------

        def set_user(self, username: str):
            self.bridge.push("session", {"event": "user", "user": username})

        def refresh_todos(self):
            self.bridge.push("refresh", {"panel": "tasks"})

        def refresh_notes_panel(self):
            self.bridge.push("refresh", {"panel": "notes"})

        def refresh_tasks(self):
            self.bridge.push("refresh", {"panel": "pipeline"})

        def closeEvent(self, event):
            try:
                if getattr(self, "auth", None) is not None:
                    self.auth.face_cancel()           # the camera is released, not left to time out
            except Exception:
                pass
            try:
                from security.approvals import approval_gateway
                approval_gateway.detach()
                self._events_timer.stop()
                self.blank()
            except Exception:
                pass
            super().closeEvent(event)

    return AgniShell()
