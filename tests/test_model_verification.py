"""
Model verification must report what the model DID, typed by what it is FOR.

Regression tests for the 2026-09-24 setup screenshots:
  * bge-m3 "NOT WORKING - 0-dim vector": the runtime had answered HTTP 500 and
    the embedding was read out of the error body;
  * qwen3-vl:8b "NOT WORKING": its token budget went into reasoning, and it
    was never shown an image;
  * qwen3:4b "PASS" although its answer was its own leaked reasoning.
A fake Ollama on loopback plays each of those parts.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import model_lifecycle
from core.model_output import validate_vector


class _Fake(BaseHTTPRequestHandler):
    behaviour: dict = {}
    loaded: set = set()

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send(200, {"models": [{"name": n, "size": 1_000_000_000, "digest": "d" * 64}
                                        for n in self.behaviour]})
        elif self.path.startswith("/api/ps"):
            self._send(200, {"models": [{"name": n, "size": 1_000_000_000, "size_vram": 900_000_000,
                                         "expires_at": "x"} for n in self.loaded]})
        else:
            self._send(404, {})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        model = request.get("model", "")
        mode = self.behaviour.get(model, "")
        if self.path == "/api/show":
            caps = {"embed": ["embedding"], "embed500": ["embedding"], "vision": ["completion", "vision"],
                    "vision_thinks": ["completion", "vision", "thinking"]}.get(mode, ["completion", "thinking"])
            return self._send(200, {"capabilities": caps})
        if request.get("keep_alive") == 0:
            self.loaded.discard(model)
            return self._send(200, {"done_reason": "unload"})
        if mode == "embed500":
            return self._send(500, {"error": "llama-server process has terminated: exit status 0xc0000409"})
        self.loaded.add(model)
        if self.path == "/api/embed":
            inputs = request.get("input")
            inputs = inputs if isinstance(inputs, list) else [inputs]
            vectors = []
            for text in inputs:
                text = text.lower()
                vectors.append([1.0 if "pump" in text or "pressure" in text else 0.1,
                                1.0 if "boiler" in text else 0.1, 1.0 if "budget" in text else 0.1])
            return self._send(200, {"embeddings": vectors})
        if self.path == "/api/chat":
            think = request.get("think")
            if mode == "leaks" and think is False:
                return self._send(200, {"message": {"content": "Hmm, the user wants me to reply with READY."},
                                        "done_reason": "stop"})
            return self._send(200, {"message": {"content": "READY", "thinking": "..."}, "done_reason": "stop"})
        if self.path == "/api/generate":
            if not request.get("images"):
                return self._send(200, {"response": "", "done_reason": "load"})
            if mode == "vision_thinks" and request.get("options", {}).get("num_predict", 0) < 100:
                return self._send(200, {"response": "", "done_reason": "length", "thinking": "hmm"})
            return self._send(200, {"response": "Red.", "done_reason": "stop"})
        self._send(404, {})


@pytest.fixture()
def fake(monkeypatch):
    _Fake.behaviour = {"bge-m3:latest": "embed", "broken-embed:latest": "embed500",
                       "leaky:4b": "leaks", "clean:8b": "clean", "eyes:8b": "vision_thinks"}
    _Fake.loaded = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(model_lifecycle, "host", lambda: url)
    import core.sysmem
    monkeypatch.setattr(core.sysmem, "has_headroom", lambda need, margin=1.0: (True, "test"))
    yield url
    server.shutdown()


def test_vector_validation_rejects_every_unusable_shape():
    assert validate_vector([0.1, 0.2])[0]
    for bad, reason in ((None, "no vector"), ([], "0-dimensional"), ([0.0, 0.0], "all-zero"),
                        ([float("nan"), 1.0], "NaN"), (["a", "b"], "non-numeric")):
        ok, why = validate_vector(bad)
        assert not ok and reason in why, (bad, why)


def test_an_embedding_model_is_tested_as_an_embedding_model(fake):
    from agni_ops.models import EMBEDDING, test_model
    report = test_model("bge-m3")
    assert report.type == EMBEDDING
    assert report.function, report.function_detail
    assert "ranked the pump document first" in report.function_detail
    assert not _Fake.loaded, "the model was left loaded after its test"


def test_an_http_500_is_reported_as_a_runtime_failure_not_a_0_dim_vector(fake):
    from agni_ops.models import test_model
    report = test_model("broken-embed")
    assert report.status in ("DEGRADED", "FAIL")
    assert "0-dim" not in (report.cause + report.function_detail)
    assert "HTTP 500" in report.cause or "0xc0000409" in report.cause


def test_a_model_that_leaks_reasoning_does_not_pass_on_a_quoted_prompt(fake):
    from agni_ops.models import test_model
    report = test_model("leaky:4b")
    # It answers cleanly when allowed to think - that is recorded, and
    # ZEUS then asks it to think; the leak itself is never a pass.
    assert report.thinking_control["think_false_leaks"] is True
    assert "ignores think=false" in report.function_detail


def test_a_vision_model_is_shown_an_image_and_must_name_its_colour(fake):
    from agni_ops.models import VISION, test_model
    report = test_model("eyes:8b")
    assert report.type == VISION
    assert report.function, report.function_detail
    assert "red" in report.function_detail.lower()


def test_thinking_is_switched_off_only_where_it_is_honoured(monkeypatch):
    from core import ollama_client
    monkeypatch.setattr(ollama_client, "_calibrated_here", {})
    measured = []
    monkeypatch.setattr(ollama_client, "_measure_think_false",
                        lambda host, model: measured.append(model) or False)
    # measured to honour think=false -> off; measured to leak -> on
    monkeypatch.setattr("core.model_calibration.think_false_honoured", lambda model: True)
    assert ollama_client._think_flag("h", "qwen3:8b", False) is False
    monkeypatch.setattr("core.model_calibration.think_false_honoured", lambda model: False)
    assert ollama_client._think_flag("h", "qwen3:4b", False) is True
    # a request that asks to think always thinks
    assert ollama_client._think_flag("h", "qwen3:8b", True) is True
    # never measured, under pytest: the original think=false, no hidden call
    monkeypatch.setattr("core.model_calibration.think_false_honoured", lambda model: None)
    assert ollama_client._think_flag("h", "deepseek-r1:8b", False) is False
    assert measured == []
    # never measured, in ZEUS: measured ONCE on first use, then remembered
    monkeypatch.setattr(ollama_client, "_lifecycle_enabled", lambda: True)
    assert ollama_client._think_flag("h", "leaky:4b", False) is True
    assert ollama_client._think_flag("h", "leaky:4b", False) is True
    assert measured == ["leaky:4b"]


def test_a_leaking_model_is_moved_off_a_latency_slot(monkeypatch):
    from core import model_router as mr
    router = mr.ModelRouter()
    monkeypatch.setattr(mr.ollama_client, "list_models", lambda force=False: ["qwen3:4b", "qwen2.5:3b-instruct-q4_K_M"])
    monkeypatch.setattr("core.model_calibration.think_false_leaks", lambda model: model == "qwen3:4b")
    monkeypatch.setattr("security.integrity.model_integrity.may_use", lambda name: True)
    assert router.route("fast") == "qwen2.5:3b-instruct-q4_K_M"


def test_a_vision_model_that_cannot_load_falls_back_to_a_smaller_one(monkeypatch):
    class Runtime(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"models": [{"name": "big-vl:8b", "size": 6_000_000_000},
                                        {"name": "small-vl:3b", "size": 3_000_000_000}]})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if request["model"] == "big-vl:8b":
                return self._send(500, {"error": "llama-server process has terminated: 0xc0000409"})
            self._send(200, {"response": "A red circle.", "done_reason": "stop"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Runtime)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from core import model_router
        from core.ollama_client import OllamaClient
        monkeypatch.setitem(model_router.DEFAULT_PREFERENCES, "vision", ["big-vl:8b", "small-vl:3b"])
        monkeypatch.setattr("security.integrity.model_integrity.may_use", lambda name: True)
        client = OllamaClient(host=f"http://127.0.0.1:{server.server_address[1]}")
        assert client.chat_vision("big-vl:8b", "what is this?", "aGk=") == "A red circle."
        assert client.chat_vision("big-vl:8b", "what is this?", "aGk=", fallback=False).startswith(
            "[ZEUS model failure")
    finally:
        server.shutdown()


def test_the_model_failure_reply_is_treated_as_a_failure_not_an_answer():
    from core.ollama_client import MODEL_FAILURE_PREFIX
    from core.orchestration.validators import validate
    report = validate("text", f"{MODEL_FAILURE_PREFIX}: 'qwen3:8b' - HTTP 500: out of memory]")
    assert not report.ok
