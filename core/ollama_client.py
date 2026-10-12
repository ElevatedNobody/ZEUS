"""
ZEUS Ollama Client
Thin, robust wrapper around the local Ollama HTTP API.
Handles connection checks, model discovery, retries, and clean errors -
the rest of ZEUS never talks to Ollama directly.
"""

import configparser
import time as _time
from pathlib import Path
from typing import Generator, List, Optional, Tuple

import httpx

from core.logger import get_logger

logger = get_logger("ollama")

ROOT = Path(__file__).resolve().parent.parent


# 127.0.0.1, not "localhost": on Windows "localhost" resolves to ::1 first,
# Ollama listens on IPv4 only, and Windows takes about 2 seconds to report the
# refused IPv6 connection before trying 127.0.0.1 - on EVERY request.
_LOCAL_DEFAULT = "http://127.0.0.1:11434"


def _prefer_ipv4_loopback(url: str) -> str:
    """http://localhost:11434 -> http://127.0.0.1:11434 (same server, no 2 s wait)."""
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != "localhost":
        return url
    netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port else "")
    return urlunparse(parsed._replace(netloc=netloc))


_SSL_CONTEXT = None


def _http_options() -> dict:
    """One SSL context for the process and no proxy/environment lookups.
    httpx.get()/post() otherwise build a new client per call - reloading the
    CA bundle and reading the Windows proxy settings from the registry - and
    the interface calls the local runtime every few seconds."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        import ssl
        _SSL_CONTEXT = ssl.create_default_context()
    return {"verify": _SSL_CONTEXT, "trust_env": False}


def _get(url, **kwargs):
    return httpx.get(url, **{**_http_options(), **kwargs})


def _post(url, **kwargs):
    return httpx.post(url, **{**_http_options(), **kwargs})


def _is_loopback_url(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _load_host() -> str:
    """The model runtime is on THIS machine. A non-loopback host in config.ini
    is refused unless `[ollama] allow_remote = true` is also set (for a
    deliberately paired LAN model server) - otherwise one edited line would
    send every prompt and every document to another computer."""
    cfg_path = ROOT / "config.ini"
    if not cfg_path.exists():
        return _LOCAL_DEFAULT
    cfg = configparser.ConfigParser()
    try:
        cfg.read(cfg_path, encoding="utf-8")
    except configparser.Error:
        return _LOCAL_DEFAULT
    host = cfg.get("ollama", "host", fallback=_LOCAL_DEFAULT).strip() or _LOCAL_DEFAULT
    if _is_loopback_url(host):
        return _prefer_ipv4_loopback(host)
    if cfg.getboolean("ollama", "allow_remote", fallback=False):
        logger.warning(f"model runtime configured on another machine: {host}")
        return host
    logger.error(f"refusing non-local model runtime '{host}' - set [ollama] "
                 f"allow_remote = true to permit it; using {_LOCAL_DEFAULT}")
    try:
        from security import events
        events.record("remote_runtime_refused", host, severity="warning", persist=False)
    except Exception:
        pass
    return _LOCAL_DEFAULT


# Model families that emit a reasoning ("thinking") trace. For them ZEUS sends
# `think` explicitly and strips any trace from the answer it shows.
_THINKING_FAMILIES = ("qwen3", "deepseek-r1", "qwq", "gpt-oss", "magistral")

# Standard-library checks on model output, shared with the installer and the
# diagnostics (which must work before this module's httpx is installed).
from core.model_output import strip_thinking, validate_vector   # noqa: E402,F401


def is_thinking_model(model: str) -> bool:
    name = (model or "").lower()
    return any(name.startswith(family) for family in _THINKING_FAMILIES) and "-vl" not in name \
        and "embedding" not in name


# Reply prefix for "the runtime answered, but the model failed" - an
# out-of-memory load, a crashed runner, an empty result. Distinct from
# "[ZEUS could not reach ..." (the runtime is down) because the fixes differ,
# and the text validator treats both as failures rather than as an answer.
MODEL_FAILURE_PREFIX = "[ZEUS model failure"

_FAILURE_STARTS = (MODEL_FAILURE_PREFIX, "[ZEUS could not reach", "[ZEUS could not reach", "[Model '",
                   "[Vision model '", "[Could not reach vision model")


def is_failure_text(text: str) -> bool:
    """The client reports failures as bracketed text rather than raising; a
    caller that treats any string as an answer must check with this."""
    return str(text or "").lstrip().startswith(_FAILURE_STARTS)


def _integrity_refusal(model: str) -> Optional[str]:
    """Why a model may not be RUN (security/integrity.py), or None. Enforced
    here as well as in routing, so no path - including the router's fallback -
    executes a model the trusted registry rejects."""
    try:
        from security.integrity import model_integrity
        return model_integrity.execution_refusal(model)
    except Exception as error:                      # pragma: no cover - broken install
        logger.debug(f"integrity check unavailable: {error}")
        return None


def _error_text(response) -> str:
    """Ollama's own error message from a failed response, if it sent one."""
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])[:240]
    except Exception:
        pass
    try:
        return (response.text or "").strip()[:240] or f"HTTP {response.status_code}"
    except Exception:
        return "unknown error"


_prepared: dict = {}          # (host, model) -> time the room was last ensured
_PREPARE_TTL = 30.0


def _lifecycle_enabled() -> bool:
    """The memory gate measures THIS machine's memory and may unload models on
    the configured runtime - never under pytest unless a test opts in (see
    core.model_lifecycle.active_in_this_process)."""
    try:
        from core.model_lifecycle import active_in_this_process
        return active_in_this_process()
    except Exception:
        return False


def _prepare_model(model: str, base: Optional[str] = None) -> Tuple[bool, str]:
    """Make room for `model` before a call that may load it (see
    core/model_lifecycle.py). Never raises: a lifecycle problem must not cost
    the user an answer, so failures to MEASURE are treated as 'go ahead'.
    Checked at most every 30 s per model, so a RAG index of a thousand chunks
    does not ask the daemon a thousand times."""
    if not model or not _lifecycle_enabled():
        return True, ""
    key = (base or "", model)
    if _time.time() - _prepared.get(key, 0.0) < _PREPARE_TTL:
        return True, ""
    try:
        from core.model_lifecycle import ensure_room
        ok, reason, unloaded = ensure_room(model, base=base)
        if ok:
            _prepared[key] = _time.time()
        if unloaded:
            logger.info(f"unloaded {', '.join(unloaded)} to make room for {model}")
        if not ok:
            logger.warning(f"{model}: {reason}")
        return ok, reason
    except Exception as error:
        logger.debug(f"lifecycle check skipped for {model}: {error}")
        return True, ""


_calibrated_here: dict = {}         # model -> honoured, measured by this process


def _measure_think_false(host: str, model: str) -> Optional[bool]:
    """One short call: does `model` answer cleanly with think=false?

    Used the first time an UNMEASURED thinking model is asked not to think,
    so a fresh install is correct from the first answer without paying for
    long reasoning on every call: measured once, then remembered in
    database/model_status.json. None if it could not be measured."""
    try:
        r = _post(f"{host}/api/chat", json={
            "model": model, "stream": False, "think": False, "keep_alive": _keep_alive(),
            "messages": [{"role": "user", "content": "Reply with exactly one word: READY"}],
            "options": {"num_predict": 96, "temperature": 0}}, timeout=180)
        if r.status_code != 200:
            return None
        answer = strip_thinking((r.json().get("message") or {}).get("content", "")).strip()
    except Exception:
        return None
    words = answer.replace('"', " ").replace(".", " ").split()
    honoured = bool(answer) and len(words) <= 4 and "ready" in answer.lower()
    try:
        from core.model_calibration import record_for, save
        if record_for(model) is None:
            save([{"model": model, "installed_as": model, "measured_by": "runtime",
                   "thinking_control": {"think_false_leaks": not honoured}}])
    except Exception as error:
        logger.debug(f"thinking calibration not saved for {model}: {error}")
    logger.info(f"{model}: think=false {'honoured' if honoured else 'IGNORED - reasoning leaks'} "
                f"(measured once; {'answers directly' if honoured else 'it will be allowed to think'})")
    return honoured


def _think_flag(host: str, model: str, think: Optional[bool]) -> bool:
    """The `think` value to send to a thinking model."""
    if think:
        return True
    from core.model_calibration import think_false_honoured
    try:
        honoured = think_false_honoured(model)
    except Exception:
        honoured = None
    if honoured is None:
        honoured = _calibrated_here.get(model)
    if honoured is None:
        if not _lifecycle_enabled():
            # Under pytest: the behaviour the suite was written against, with
            # no hidden calibration calls to count against its fake servers.
            return False
        honoured = _measure_think_false(host, model)
        if honoured is not None:
            _calibrated_here[model] = honoured
    # Unmeasurable: think - slower, but never the reasoning as the answer.
    return not honoured if honoured is not None else True


def _keep_alive() -> str:
    try:
        from core.model_lifecycle import keep_alive
        return keep_alive()
    except Exception:
        return "5m"


def _runtime_options() -> dict:
    """Context window and residency from config.ini [models]."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(ROOT / "config.ini", encoding="utf-8")
    except configparser.Error:
        return {}
    options = {}
    num_ctx = cfg.get("models", "num_ctx", fallback="").strip()
    if num_ctx.isdigit():
        options["num_ctx"] = int(num_ctx)
    return options


class OllamaClient:
    def __init__(self, host: Optional[str] = None, timeout: float = 120.0):
        self.timeout = timeout
        self._available: Optional[bool] = None
        self._available_checked_at: float = 0.0
        self._availability_ttl: float = 4.0  # seconds - short cache, not permanent
        self._models: Optional[List[str]] = None
        self._models_listed_at: float = 0.0
        self._models_ttl: float = 4.0
        self._host = (host or _load_host()).rstrip("/")
        # Why the most recent embed() returned None, for diagnostics.
        self.last_embed_error = ""

    @property
    def host(self) -> str:
        return self._host

    @host.setter
    def host(self, value: str):
        """Pointing at a different runtime invalidates everything known about
        the old one. Without this, a cached model list or availability flag
        from one host silently answers questions about another."""
        new = (value or "").rstrip("/")
        if new != getattr(self, "_host", None):
            self._available = None
            self._available_checked_at = 0.0
            self._models = None
            self._models_listed_at = 0.0
        self._host = new

    def is_available(self, force: bool = False) -> bool:
        if (not force and self._available is not None
                and (_time.time() - self._available_checked_at) < self._availability_ttl):
            return self._available
        try:
            r = _get(f"{self.host}/api/tags", timeout=3.0)
            self._available = r.status_code == 200
        except Exception as e:
            logger.warning(f"Ollama not reachable at {self.host}: {e}")
            self._available = False
        # Stamped after the call - a check that took longer than the TTL must
        # not store an entry that is already stale.
        self._available_checked_at = _time.time()
        return self._available

    def list_models(self, force: bool = False) -> List[str]:
        """The models installed on this machine, cached for a few seconds.

        The cache is not an optimisation for its own sake. Resolving a model
        for one task calls this once, and startup resolves seven slots plus
        the capability probes - so an uncached client made a dozen HTTP
        requests for a list that cannot change in that time. On localhost
        those are invisible; against a CONFIGURED remote host that is not
        answering, each one costs the full 5-second timeout and the interface
        sits blank for the best part of a minute.

        The window is deliberately short. `ollama pull` during a session is
        picked up within seconds, and `force=True` skips the cache for code
        that has just changed what is installed.
        """
        if (not force and self._models is not None
                and (_time.time() - self._models_listed_at) < self._models_ttl):
            return list(self._models)
        try:
            r = _get(f"{self.host}/api/tags", timeout=5.0)
            r.raise_for_status()
            data = r.json()
            self._models = [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            # Cache the empty result too: a host that is not answering will
            # not answer the next nine calls either, and retrying each one is
            # what makes an unreachable host feel like a hung application.
            self._models = []
        # Stamped AFTER the call, not before. A request that takes longer than
        # the TTL would otherwise produce an entry that is already expired the
        # moment it is stored - which is exactly the slow case the cache is
        # for, so the cache would help only when it was not needed.
        self._models_listed_at = _time.time()
        return list(self._models)

    @staticmethod
    def _model_matches(installed: str, preferred: str) -> bool:
        """Does an installed model satisfy a preference?

        Ollama names an untagged pull `<model>:latest`, so `ollama pull bge-m3`
        installs `bge-m3:latest`. The original matcher accepted only `==` and
        the `-`/`_` variant suffixes, so an embedding model that was present
        and working reported as 'no model available for task embedding'. That
        single missing case is why RAG looked broken on a machine that had
        everything installed.

        Matching stays strict about the model FAMILY - 'qwen2.5:7b' must never
        match 'qwen2.5:3b' - while accepting the tag forms Ollama actually
        produces.
        """
        if installed == preferred:
            return True
        # A preference without a tag matches any tag of the same model:
        # 'bge-m3' matches 'bge-m3:latest' and 'bge-m3:567m'.
        if ":" not in preferred and installed.startswith(preferred + ":"):
            return True
        # A tagged preference matches quantisation/instruct variants of that
        # exact tag: 'qwen2.5:7b' matches 'qwen2.5:7b-instruct-q4_0'.
        if installed.startswith(preferred + "-") or installed.startswith(preferred + "_"):
            return True
        # ...and the ':latest' form of a tagged preference.
        if installed == preferred + ":latest":
            return True
        return False

    def pick_available_model(self, preferred: List[str]) -> Optional[str]:
        """Return the first preferred model that's actually pulled, or None so
        callers can say 'please pull this model' rather than silently
        substituting a model the user did not ask for."""
        installed = self.list_models()
        if not installed:
            return None
        for candidate in preferred:
            for model in installed:
                if self._model_matches(model, candidate):
                    return model
        return None

    def chat(self, model: str, messages: List[dict], retries: int = 2, timeout: Optional[float] = None,
             task_type: Optional[str] = None, purpose: Optional[str] = None,
             think: Optional[bool] = None, json_format: bool = False) -> str:
        """Non-streaming chat completion with retry on transient failure.
        `timeout` overrides self.timeout for this call - long-form generation
        (e.g. writing an assignment) needs more time than a quick chat reply,
        but should NOT also multiply that wait by retrying 2-3 more times.
        `task_type`/`purpose` are optional - passing them makes this call
        visible in the dashboard's live model-activity display.

        `think`: for reasoning-capable families (Qwen3, DeepSeek-R1) thinking
        is switched OFF only for a model measured to honour that
        (core/model_calibration.py); otherwise the model thinks and only its
        answer is returned - the trace comes back in a separate field. Any
        <think> trace that still appears inline is stripped."""
        from core.model_activity import start_activity, finish_activity
        entry = start_activity(model, task_type or "general", purpose or "Chat request")
        refusal = _integrity_refusal(model)
        if refusal:
            logger.error(f"integrity: {refusal}")
            finish_activity(entry, "refused")
            return f"{MODEL_FAILURE_PREFIX}: {refusal}]"

        payload = {"model": model, "messages": messages, "stream": False,
                   "keep_alive": _keep_alive()}
        if is_thinking_model(model):
            # think=false is sent only to a model MEASURED to honour it. Some
            # builds ignore it (this machine's qwen3:4b), and then their whole
            # reasoning arrives AS the answer. A model never measured is
            # measured once, on first use (_measure_think_false).
            payload["think"] = _think_flag(self.host, model, think)
        if json_format:
            payload["format"] = "json"
        options = _runtime_options()
        if options:
            payload["options"] = options
        room_ok, room_reason = _prepare_model(model, self.host)

        try:
            from core.assistant_state import State, assistant_state
            thinking_scope = assistant_state.activity(State.THINKING, model)
            thinking_scope.__enter__()
        except Exception:
            thinking_scope = None

        last_err = None
        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            for attempt in range(retries + 1):
                try:
                    r = _post(f"{self.host}/api/chat", json=payload,
                                   timeout=effective_timeout)
                    if r.status_code == 400 and "think" in payload and "think" in r.text.lower():
                        # An older runtime or a model build without thinking
                        # support: retry once without the field.
                        payload.pop("think", None)
                        r = _post(f"{self.host}/api/chat", json=payload,
                                       timeout=effective_timeout)
                    r.raise_for_status()
                    content = strip_thinking(r.json().get("message", {}).get("content", ""))
                    finish_activity(entry, "done")
                    return content
                except httpx.HTTPStatusError as e:
                    last_err = e
                    if e.response.status_code == 404:
                        finish_activity(entry, "model not found")
                        return f"[Model '{model}' not found. Try: ollama pull {model}]"
                    # The runtime answered: the MODEL failed (typically it could
                    # not be loaded - out of memory, a crashed runner). Saying
                    # "is ollama serve running?" here sent people to fix the
                    # wrong thing, and was then shown as if it were an answer.
                    detail = _error_text(e.response)
                    if not room_ok and room_reason:
                        detail += f" ({room_reason})"
                    logger.error(f"model {model} failed: {detail}")
                    finish_activity(entry, "failed")
                    return f"{MODEL_FAILURE_PREFIX}: '{model}' - {detail}]"
                except Exception as e:
                    last_err = e
                    logger.warning(f"Ollama chat attempt {attempt + 1} failed: {e}")
                    if attempt < retries:
                        _time.sleep(0.5 * (attempt + 1))
        finally:
            if thinking_scope is not None:
                thinking_scope.__exit__(None, None, None)
        logger.error(f"Ollama chat failed after retries: {last_err}")
        finish_activity(entry, "failed")
        return "[ZEUS could not reach the local AI model. Is 'ollama serve' running?]"

    def chat_stream(self, model: str, messages: List[dict]) -> Generator[str, None, None]:
        """Yields response chunks as they arrive."""
        refusal = _integrity_refusal(model)
        if refusal:
            logger.error(f"integrity: {refusal}")
            yield f"{MODEL_FAILURE_PREFIX}: {refusal}]"
            return
        try:
            with httpx.stream(
                "POST", f"{self.host}/api/chat",
                json={"model": model, "messages": messages, "stream": True},
                timeout=self.timeout,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    import json as _json
                    try:
                        chunk = _json.loads(line)
                    except ValueError:
                        continue
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if chunk.get("done"):
                        break
        except Exception as e:
            logger.error(f"Ollama stream failed: {e}")
            yield "[ZEUS could not reach the local AI model. Is 'ollama serve' running?]"

    def embed(self, model: str, text: str, timeout: float = 30.0,
              purpose: Optional[str] = None) -> Optional[List[float]]:
        """Get an embedding vector for text via Ollama's /api/embeddings endpoint.
        Returns None on failure (model not pulled, Ollama down, etc.) rather
        than raising, so RAG indexing can skip a chunk and continue instead
        of aborting the whole operation."""
        from core.model_activity import start_activity, finish_activity
        entry = start_activity(model, "embedding", purpose or "Embedding text")
        self.last_embed_error = ""
        refusal = _integrity_refusal(model)
        if refusal:
            self.last_embed_error = refusal
            finish_activity(entry, "refused")
            return None
        _prepare_model(model, self.host)
        try:
            r = _post(
                f"{self.host}/api/embeddings",
                json={"model": model, "prompt": text, "keep_alive": _keep_alive()},
                timeout=timeout,
            )
            if r.status_code >= 400:
                # Read Ollama's reason instead of an empty body: an HTTP 500
                # here is a model that could not LOAD, not a bad vector.
                self.last_embed_error = f"HTTP {r.status_code}: {_error_text(r)}"
                logger.warning(f"Ollama embed failed (model={model}): {self.last_embed_error}")
                finish_activity(entry, "failed")
                return None
            embedding = r.json().get("embedding")
            ok, reason = validate_vector(embedding)
            if not ok:
                self.last_embed_error = reason
                logger.warning(f"Ollama embed returned an unusable vector (model={model}): {reason}")
                finish_activity(entry, "empty result")
                return None
            finish_activity(entry, "done")
            return [float(v) for v in embedding]
        except Exception as e:
            self.last_embed_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Ollama embed failed (model={model}): {e}")
            finish_activity(entry, "failed")
            return None

    def chat_vision(self, model: str, prompt: str, image_base64: str, timeout: float = 90.0,
                     purpose: Optional[str] = None, fallback: bool = True) -> str:
        """Send an image + prompt to a vision-capable model. Returns a clear
        error string (not an exception) if the vision model isn't pulled -
        this is a common first-run gap and callers need to surface it.

        If the model is installed but FAILS at run time (it could not load -
        e.g. not enough memory for an 8B vision model beside everything else)
        the next smaller installed, trusted vision model is tried once, and
        the substitution is logged."""
        result = self._chat_vision_once(model, prompt, image_base64, timeout, purpose)
        if not (fallback and result.startswith(MODEL_FAILURE_PREFIX)):
            return result
        for alternative in self._smaller_vision_models(model):
            logger.warning(f"vision: {model} failed ({result[len(MODEL_FAILURE_PREFIX):][:120]}) - "
                           f"trying the smaller {alternative}")
            second = self._chat_vision_once(alternative, prompt, image_base64, timeout, purpose)
            if not second.startswith("["):
                logger.info(f"vision: answered by fallback model {alternative} instead of {model}")
                return second
        return result

    def _smaller_vision_models(self, model: str) -> List[str]:
        """Installed, trusted vision models from the router's vision list that
        are smaller than `model`, largest first."""
        try:
            from core.model_lifecycle import model_size_gb
            from core.model_router import DEFAULT_PREFERENCES
            from security.integrity import model_integrity
        except Exception:
            return []
        installed = self.list_models()
        size = model_size_gb(model, self.host) or float("inf")
        found = []
        for preferred in DEFAULT_PREFERENCES.get("vision", []):
            for name in installed:
                if (self._model_matches(name, preferred) and name != model and name not in found
                        and model_integrity.may_use(name)):
                    candidate_size = model_size_gb(name, self.host)
                    if 0 < candidate_size < size:
                        found.append(name)
        return sorted(found, key=lambda n: -model_size_gb(n, self.host))[:2]

    def _chat_vision_once(self, model: str, prompt: str, image_base64: str, timeout: float,
                          purpose: Optional[str]) -> str:
        from core.model_activity import start_activity, finish_activity
        entry = start_activity(model, "vision", purpose or "Analyzing image")
        refusal = _integrity_refusal(model)
        if refusal:
            finish_activity(entry, "refused")
            return f"{MODEL_FAILURE_PREFIX}: vision model {refusal}]"
        room_ok, room_reason = _prepare_model(model, self.host)
        try:
            # qwen3-vl is a THINKING model that ignores think=false on Ollama
            # 0.34: the reasoning trace comes back in `thinking` and only the
            # answer in `response`. No token cap is set, because a capped call
            # spends the whole budget thinking and returns an empty answer -
            # which is exactly how setup came to report it "NOT WORKING".
            r = _post(
                f"{self.host}/api/generate",
                json={"model": model, "prompt": prompt, "images": [image_base64],
                      "stream": False, "keep_alive": _keep_alive()},
                timeout=timeout,
            )
            if r.status_code == 404:
                finish_activity(entry, "model not found")
                return f"[Vision model '{model}' not found. Try: ollama pull {model}]"
            if r.status_code >= 400:
                detail = _error_text(r)
                if not room_ok and room_reason:
                    detail += f" ({room_reason})"
                finish_activity(entry, "failed")
                return f"{MODEL_FAILURE_PREFIX}: vision model '{model}' - {detail}]"
            body = r.json()
            result = strip_thinking(body.get("response", ""))
            if not result.strip():
                why = ("the token budget ran out while it was still reasoning"
                       if body.get("done_reason") == "length" else "it returned no answer")
                finish_activity(entry, "empty result")
                return f"{MODEL_FAILURE_PREFIX}: vision model '{model}' - {why}]"
            finish_activity(entry, "done")
            return result
        except Exception as e:
            logger.warning(f"Ollama vision call failed: {e}")
            finish_activity(entry, "failed")
            return f"[Could not reach vision model '{model}': {e}]"


ollama_client = OllamaClient()
