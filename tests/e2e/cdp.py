"""
A small Chrome DevTools Protocol driver for the ZEUS window.

Playwright's connect_over_cdp() cannot attach to Qt WebEngine: it starts with
Browser.setDownloadBehavior / context management, which Qt WebEngine refuses
("Browser context management is not supported"). This driver attaches straight
to the page target and uses only page-level domains:

* Runtime.evaluate           - read the DOM, await promises
* Input.dispatchMouseEvent   - real mouse presses at the element's centre
* Input.dispatchKeyEvent     - real key presses
* Input.insertText           - typing

Loopback only; the harness enables the debugging port for tests alone.
"""

from __future__ import annotations

import itertools
import json
import time
import urllib.request
from typing import Any, Optional

import websocket  # websocket-client (requirements-dev.txt)


class CDPError(RuntimeError):
    pass


_KEYS = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
}


_NO_ARG = object()


class Page:
    def __init__(self, port: int, url_prefix: str = "agni://", timeout: float = 60):
        self.port = port
        deadline = time.time() + timeout
        target = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
                    targets = json.loads(r.read().decode("utf-8"))
                target = next((t for t in targets if t.get("type") == "page"
                               and t.get("url", "").startswith(url_prefix)), None)
                if target:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        if not target:
            raise CDPError(f"no page starting with {url_prefix} on port {port}")
        self._ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=120,
                                               suppress_origin=True)
        self._ids = itertools.count(1)
        self.send("Runtime.enable")

    # -- protocol ---------------------------------------------------------------

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 120) -> dict:
        msg_id = next(self._ids)
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._ws.settimeout(max(0.1, deadline - time.time()))
            reply = json.loads(self._ws.recv())
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise CDPError(f"{method}: {reply['error']}")
                return reply.get("result", {})
        raise CDPError(f"{method}: no reply")

    def evaluate(self, expression: str, arg: Any = _NO_ARG, timeout: float = 120) -> Any:
        """Evaluate an expression - or, when ``arg`` is given, a function source
        called with ``arg``."""
        expr = expression.strip()
        if arg is not _NO_ARG:
            expr = f"({expr})({json.dumps(arg)})"
        result = self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                                "returnByValue": True, "userGesture": True},
                           timeout=timeout)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise CDPError(f"JS error: {text}")
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass

    # -- conveniences -----------------------------------------------------------

    @property
    def url(self) -> str:
        try:
            return self.evaluate("location.href", timeout=5) or ""
        except Exception:
            return ""

    def wait_for(self, expression: str, arg: Any = _NO_ARG, timeout: float = 30) -> Any:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.evaluate(expression, arg, timeout=10)
                if last:
                    return last
            except CDPError:
                pass
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for: {expression} (last={last!r})")

    def text(self, selector: str) -> str:
        return self.evaluate("(s) => { const e = document.querySelector(s); return e ? e.innerText : null; }",
                             selector)

    def _centre(self, selector: str, contains: str = "") -> tuple:
        box = self.wait_for(
            """([s, t]) => {
                 const els = [...document.querySelectorAll(s)].filter(e => !t || e.innerText.includes(t));
                 const e = els[0]; if (!e) return null;
                 e.scrollIntoView({block: 'center', inline: 'center'});
                 const r = e.getBoundingClientRect();
                 if (!r.width || !r.height) return null;
                 return [r.left + r.width / 2, r.top + r.height / 2];
               }""", [selector, contains], timeout=15)
        time.sleep(0.35)          # let smooth scrolling settle, then measure again
        again = self.evaluate(
            """([s, t]) => { const e = [...document.querySelectorAll(s)].filter(e => !t || e.innerText.includes(t))[0];
                              if (!e) return null;
                              const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }""",
            [selector, contains])
        return tuple(again or box)

    def click(self, selector: str, contains: str = "") -> None:
        x, y = self._centre(selector, contains)
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": kind, "x": x, "y": y}
            if kind != "mouseMoved":
                params.update(button="left", clickCount=1)
            self.send("Input.dispatchMouseEvent", params)

    def key(self, name: str) -> None:
        spec = _KEYS[name]
        self.send("Input.dispatchKeyEvent", {"type": "keyDown", **spec})
        self.send("Input.dispatchKeyEvent", {"type": "keyUp", **{k: v for k, v in spec.items() if k != "text"}})

    def fill(self, selector: str, text: str) -> None:
        self.click(selector)
        self.evaluate("(s) => { const e = document.querySelector(s); e.value = ''; e.focus(); return true; }", selector)
        self.send("Input.insertText", {"text": text})
