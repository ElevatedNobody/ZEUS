"""
A local server that speaks Ollama's HTTP protocol.

This is test infrastructure, not a product component. It exists because the
bugs it catches are INTERFACE bugs - the wrong endpoint, the wrong model-name
form, a vector that never arrives - and those cannot be caught by mocking the
client, only by making a real HTTP request against something that answers the
way Ollama answers.

It implements exactly what ZEUS uses:

    GET  /api/tags         the installed model list, in Ollama's `name:tag` form
    POST /api/embeddings   {model, prompt} -> {"embedding": [...]}
    POST /api/chat         {model, messages} -> {"message": {"content": ...}}

Embeddings are deterministic hashed bag-of-words vectors: the same text always
gives the same vector, and similar texts give similar vectors, which is enough
for retrieval ordering to be meaningful and testable without shipping weights.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional

DIMENSION = 256

# Named the way Ollama actually names them - `bge-m3:latest`, not `bge-m3`.
# That single detail is what the original model matcher got wrong.
DEFAULT_MODELS = [
    "bge-m3:latest",
    "qwen2.5:3b-instruct-q4_K_M",
    "mistral:7b-instruct-q4_K_M",
    "qwen2.5vl:3b",
]


def _vector(text: str) -> List[float]:
    """A deterministic vector where lexical overlap produces cosine similarity."""
    vector = [0.0] * DIMENSION
    for token in re.findall(r"[a-z0-9]+", (text or "").lower()):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % DIMENSION
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class _Handler(BaseHTTPRequestHandler):
    models: List[str] = list(DEFAULT_MODELS)
    embeddings_enabled: bool = True
    chat_reply: str = "Grounded answer from the local model."

    def log_message(self, *args):            # keep the test output readable
        return

    def _send(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": name} for name in self.models]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send({"error": "bad json"}, 400)
            return

        if self.path.startswith("/api/embeddings"):
            if not self.embeddings_enabled:
                # What a runtime without the embeddings endpoint looks like.
                self._send({"error": "embedding not supported"}, 400)
                return
            model = request.get("model", "")
            if not any(model == name or name.startswith(model.split(":")[0])
                       for name in self.models):
                self._send({"error": f"model '{model}' not found"}, 404)
                return
            self._send({"embedding": _vector(request.get("prompt", ""))})
            return

        if self.path.startswith("/api/chat"):
            self._send({"message": {"role": "assistant", "content": self.chat_reply}})
            return

        if self.path.startswith("/api/generate"):
            self._send({"response": ""})
            return

        self._send({"error": "not found"}, 404)


class FakeOllama:
    """Start/stop helper. Use as a context manager."""

    def __init__(self, models: Optional[List[str]] = None,
                 embeddings_enabled: bool = True):
        _Handler.models = list(models if models is not None else DEFAULT_MODELS)
        _Handler.embeddings_enabled = embeddings_enabled
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self.host = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def __enter__(self) -> "FakeOllama":
        self._thread.start()
        return self

    def __exit__(self, *exception):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)

    @staticmethod
    def set_chat_reply(text: str):
        _Handler.chat_reply = text
