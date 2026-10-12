"""
The failure-mode matrix: what ZEUS does when something is wrong.

Every case here is a machine this will actually run on - a laptop with the
model runtime stopped, a fresh install with no weights, a config file someone
edited by hand, a scanned PDF that came out empty. The requirement is the
same in all of them: say what is wrong, in a sentence that names the fix, and
keep running whatever can still run.

The two failures this suite exists to prevent are the opposite of each other:
claiming to work when nothing did, and refusing to start over something
optional.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import capabilities as cap  # noqa: E402
from core.ollama_client import ollama_client  # noqa: E402

DOWN = "http://127.0.0.1:1"          # nothing listens here


@pytest.fixture
def runtime_down():
    original = ollama_client.host
    ollama_client.host = DOWN
    ollama_client._available = None
    ollama_client._available_checked_at = 0.0
    cap.embedding_provider._model = None
    yield
    ollama_client.host = original
    ollama_client._available = None
    ollama_client._available_checked_at = 0.0
    cap.embedding_provider._model = None


# ------------------------------------------------- the runtime is down --

def test_a_down_runtime_names_the_command_that_fixes_it(runtime_down):
    report = cap.embedding_provider.check()
    assert report.health is cap.Health.RUNTIME_DOWN
    assert "ollama serve" in report.detail


def test_the_capability_report_still_answers_with_the_runtime_down(runtime_down):
    """The diagnostic tool has to work in exactly the situation it is for."""
    from core.tool_registry import registry
    result = registry.execute("capability_report")
    assert result.success, result.error
    assert "not reachable" in result.output["local_runtime"]
    assert result.output["capabilities"], "no capabilities were reported"


def test_reranking_survives_the_runtime_being_down(runtime_down):
    """Scoring is local Python. Losing the model runtime must not lose it."""
    report = cap.rerank_provider.check()
    assert report.ok
    assert report.evidence["probe_ranked_correctly"] is True


def test_startup_continues_when_the_runtime_is_down(runtime_down):
    """Model discovery reports and hands over; it does not stop the sequence."""
    import main
    discovered = main.discover_local_models()
    assert discovered["runtime"] is False
    assert discovered["models"] == []
    assert main.check_capabilities(), "capability readiness produced nothing"


# ------------------------------------------------------ models missing --

def test_a_missing_embedding_model_says_what_to_pull():
    from tests.fake_ollama import FakeOllama
    original = ollama_client.host
    with FakeOllama(models=["qwen2.5:3b-instruct-q4_K_M"]) as server:
        ollama_client.host = server.host
        ollama_client._available = None
        cap.embedding_provider._model = None
        report = cap.embedding_provider.check()
        assert report.health is cap.Health.MODEL_MISSING
        assert "ollama pull" in report.detail
    ollama_client.host = original
    ollama_client._available = None
    cap.embedding_provider._model = None


def test_a_missing_voice_model_leaves_a_working_application():
    """Voice is optional. Missing weights are a state, never a startup failure."""
    from voice.providers import ProviderState, VoiceState, VoiceManager
    manager = VoiceManager()
    reports = manager.probe()
    assert reports, "no providers were considered"
    for report in reports.values():
        assert report.requires_download is False
        assert report.detail, f"{report.name} gave a state with no reason"
        # A probe never claims READY: only a real load can, and probing does
        # not load. Anything else here would be a status nobody verified.
        assert report.state is not ProviderState.READY
    state = manager.start()
    assert state in (VoiceState.AVAILABLE, VoiceState.DEGRADED,
                     VoiceState.DISABLED, VoiceState.ERROR)
    # Whatever happened, speaking is a no-op rather than an exception.
    manager.speak("this must not raise")
    manager.close()


def test_a_missing_reranker_degrades_rather_than_disappears():
    report = cap.rerank_provider.check()
    assert report.ok, "reranking became unavailable instead of degrading"
    if not report.evidence.get("cross_encoder"):
        assert report.health is cap.Health.DEGRADED


# ---------------------------------------------------------- low memory --

def test_the_router_steps_down_when_memory_is_short():
    """16 GB with a browser open is the normal case, not the edge case."""
    from core.orchestration.contracts import Complexity, TaskKind
    from core.orchestration.router import capability_router

    with patch("core.orchestration.router.available_ram_gb", return_value=2.0):
        constrained = capability_router.route_for(TaskKind.REASONING,
                                                  Complexity.HARD)
    with patch("core.orchestration.router.available_ram_gb", return_value=32.0):
        roomy = capability_router.route_for(TaskKind.REASONING, Complexity.HARD)

    assert constrained.estimated_ram_gb <= roomy.estimated_ram_gb, (
        f"with 2 GB free the router chose {constrained.model} "
        f"({constrained.estimated_ram_gb} GB), no smaller than the "
        f"{roomy.estimated_ram_gb} GB choice it makes with 32 GB")
    assert constrained.reason, "the router made a choice it cannot explain"


def test_a_model_choice_always_explains_itself():
    from core.orchestration.contracts import Complexity, TaskKind
    from core.orchestration.router import capability_router
    for kind in (TaskKind.REASONING, TaskKind.CHAT, TaskKind.CODE):
        for complexity in (Complexity.SIMPLE, Complexity.HARD):
            choice = capability_router.route_for(kind, complexity)
            assert choice.reason, f"no reason recorded for {kind}/{complexity}"


# -------------------------------------------------------- broken config --

def test_a_broken_config_file_does_not_stop_startup(tmp_path, monkeypatch):
    """A config someone edited by hand must not be the reason ZEUS will not
    open. Defaults are the documented behaviour."""
    from voice import offline

    broken = tmp_path / "config.ini"
    broken.write_text("[voice\nenabled = this is not ini at all\n", encoding="utf-8")
    monkeypatch.setattr(offline, "CONFIG_PATH", broken)
    monkeypatch.setattr(offline, "_config_cache", None, raising=False)
    values = offline.voice_config(refresh=True)
    assert isinstance(values, dict), "a broken config raised instead of defaulting"


def test_a_malformed_config_does_not_break_capability_resolution(tmp_path, monkeypatch):
    """config.ini is read for an optional embedding-model override. A file
    that will not parse must mean "no override", not an exception on the RAG
    path - the boundary swallows it so callers never have to."""
    import core.capabilities as capabilities_module

    broken = tmp_path / "config.ini"
    broken.write_text("[models\nembedding_model == = broken\n", encoding="utf-8")
    real_file = capabilities_module.__file__
    monkeypatch.setattr(capabilities_module, "__file__",
                        str(tmp_path / "capabilities.py"))
    try:
        assert capabilities_module._config_value("models", "embedding_model") == ""
    finally:
        monkeypatch.setattr(capabilities_module, "__file__", real_file)


# ------------------------------------------------------- bad documents --

def _index(folder: Path):
    from core.tool_registry import registry
    return registry.execute("index_folder", path=str(folder))


def test_an_empty_document_is_skipped_not_fatal(tmp_path):
    from tests.fake_ollama import FakeOllama
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    (tmp_path / "real.txt").write_text(
        "Relief valve PSV-101 set pressure is 12.5 barg per the datasheet.",
        encoding="utf-8")
    original = ollama_client.host
    with FakeOllama() as server:
        ollama_client.host = server.host
        ollama_client._available = None
        cap.embedding_provider._model = None
        outcome = _index(tmp_path)
    ollama_client.host = original
    ollama_client._available = None
    cap.embedding_provider._model = None

    assert outcome.success, outcome.error
    assert outcome.output["chunks_indexed"] > 0, "one empty file stopped the index"


def test_a_binary_file_named_txt_does_not_stop_the_index(tmp_path):
    from tests.fake_ollama import FakeOllama
    (tmp_path / "corrupt.txt").write_bytes(bytes(range(256)) * 40)
    (tmp_path / "real.txt").write_text(
        "The exchanger shall be hydrotested before return to service.",
        encoding="utf-8")
    original = ollama_client.host
    with FakeOllama() as server:
        ollama_client.host = server.host
        ollama_client._available = None
        cap.embedding_provider._model = None
        outcome = _index(tmp_path)
    ollama_client.host = original
    ollama_client._available = None
    cap.embedding_provider._model = None
    assert outcome.success, outcome.error
    assert outcome.output["chunks_indexed"] > 0


def test_a_very_long_document_is_chunked_rather_than_truncated(tmp_path):
    """A 200-page procedure must not be silently cut to its first page."""
    from tests.fake_ollama import FakeOllama
    paragraph = ("Section {n}. The inspection record shall state the thickness "
                 "reading, the instrument used and the technician's "
                 "certification number.\n")
    long_text = "".join(paragraph.format(n=i) for i in range(1, 1200))
    (tmp_path / "procedure.txt").write_text(long_text, encoding="utf-8")

    original = ollama_client.host
    with FakeOllama() as server:
        ollama_client.host = server.host
        ollama_client._available = None
        cap.embedding_provider._model = None
        outcome = _index(tmp_path)
    ollama_client.host = original
    ollama_client._available = None
    cap.embedding_provider._model = None

    assert outcome.success, outcome.error
    assert outcome.output["chunks_indexed"] > 1, \
        "a long document produced a single chunk - it was truncated, not chunked"


def test_indexing_an_empty_folder_says_so_plainly(tmp_path):
    from tests.fake_ollama import FakeOllama
    original = ollama_client.host
    with FakeOllama() as server:
        ollama_client.host = server.host
        ollama_client._available = None
        cap.embedding_provider._model = None
        outcome = _index(tmp_path)
    ollama_client.host = original
    ollama_client._available = None
    cap.embedding_provider._model = None
    assert outcome.success
    assert outcome.output["files_indexed"] == 0


def test_indexing_a_path_that_is_not_a_folder_is_refused(tmp_path):
    from core.tool_registry import registry
    target = tmp_path / "a_file.txt"
    target.write_text("hello", encoding="utf-8")
    outcome = registry.execute("index_folder", path=str(target))
    text = str(outcome.output or outcome.error).lower()
    assert "not a valid directory" in text or "blocked" in text


# --------------------------------------------------- bad credentials --

def test_a_wrong_password_is_refused_and_says_nothing_useful_to_an_attacker():
    from auth.auth_manager import AuthManager
    from auth.session_manager import SessionManager
    from memory import database

    db = Path(tempfile.gettempdir()) / "medha_failure_modes_auth.db"
    database.DB_PATH = db
    database.set_active_user(None)
    if db.exists():
        db.unlink()
    database.init_db()

    auth = AuthManager()
    sessions = SessionManager(auth)
    auth.create_account("operator", "IndustrialAI2026")

    result = sessions.login("operator", "wrong-password")
    assert not result.ok
    assert "IndustrialAI2026" not in result.message
    assert "hash" not in result.message.lower()
    # And an account that does not exist must not be distinguishable.
    missing = sessions.login("nobody-at-all", "wrong-password")
    assert not missing.ok


# ------------------------------------------------------- sovereignty --

def test_no_cloud_ai_client_is_a_dependency():
    """A cloud SDK in requirements.txt is a sovereignty risk even when nothing
    imports it: it is one careless line away from being used, and it is
    installed on every machine that runs the setup."""
    requirements = (Path(__file__).resolve().parent.parent
                    / "requirements.txt").read_text(encoding="utf-8")
    lines = [line.strip().lower() for line in requirements.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    packages = {line.split(">=")[0].split("==")[0].split(";")[0].strip()
                for line in lines}
    for banned in ("openai", "anthropic", "google-cloud-speech", "boto3",
                   "azure-cognitiveservices-speech", "elevenlabs", "gtts",
                   "deepgram-sdk", "assemblyai", "cohere"):
        assert banned not in packages, f"cloud AI dependency in requirements: {banned}"


def test_the_heavy_voice_stack_is_not_a_required_dependency():
    """PART 1: starting ZEUS must not depend on a multi-gigabyte download."""
    root = Path(__file__).resolve().parent.parent
    required = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    optional = (root / "requirements-voice.txt")
    assert optional.exists(), "the optional voice stack has no requirements file"
    for heavy in ("torch", "torchaudio", "chatterbox-tts", "faster-whisper"):
        assert heavy not in required, \
            f"'{heavy}' is a required dependency; it belongs in requirements-voice.txt"
        assert heavy in optional.read_text(encoding="utf-8").lower()


def test_the_16b_coder_is_never_the_first_choice():
    """PART 7: on 16 GB with integrated graphics a 16B model swaps."""
    from core.model_router import DEFAULT_PREFERENCES
    for slot, models in DEFAULT_PREFERENCES.items():
        if not models:
            continue
        assert "deepseek-coder-v2:16b" not in models[0], \
            f"the 16B coder is the default for slot '{slot}'"


# --------------------------------------------- an unreachable remote host --

def test_a_slow_host_does_not_produce_an_already_stale_cache():
    """The cache is stamped AFTER the call, not before.

    Stamping before means a request that takes longer than the TTL stores an
    entry that has already expired - so the cache helps only when it was not
    needed. Against a configured-but-unreachable host that turned every model
    lookup into a fresh 5-second timeout, and startup resolves several.
    """
    from core.ollama_client import OllamaClient

    client = OllamaClient(host="http://127.0.0.1:1")
    client._models_ttl = 0.05            # shorter than the call will take

    def slow_failure(*args, **kwargs):
        time.sleep(0.2)                   # longer than the TTL
        raise OSError("no route to host")

    with patch("core.ollama_client.httpx.get", side_effect=slow_failure):
        client.list_models()
        started = time.time()
        client.list_models()              # must be served from the cache
        second = time.time() - started

    assert second < 0.1, (
        f"the second lookup took {second:.2f}s - the cache entry was already "
        f"stale when it was written")


def test_pointing_at_a_different_host_forgets_the_old_one():
    """A cached model list from one runtime must never answer a question
    about another."""
    from core.ollama_client import OllamaClient

    client = OllamaClient(host="http://127.0.0.1:1")
    client._models = ["ghost-model:latest"]
    client._models_listed_at = time.time()
    client._available = True

    client.host = "http://127.0.0.1:2"
    assert client._models is None, "a stale model list survived a host change"
    assert client._available is None


def test_startup_counts_do_not_make_one_request_per_slot():
    """The Command Center reads its counts on a background thread, but it
    still has to finish. Resolving seven slots must not mean seven round
    trips to a host that is not answering."""
    from core.ollama_client import OllamaClient
    import core.ollama_client as module

    calls = []

    def count_and_fail(*args, **kwargs):
        calls.append(1)
        raise OSError("unreachable")

    client = OllamaClient(host="http://127.0.0.1:1")
    with patch.object(module.httpx, "get", side_effect=count_and_fail):
        for _ in range(8):
            client.list_models()
    assert len(calls) == 1, (
        f"eight lookups made {len(calls)} requests; they should share one "
        f"cached answer")
