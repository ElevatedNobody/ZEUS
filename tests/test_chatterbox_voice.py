"""
The Chatterbox voice: the promises it makes, checked.

This replaces tests/test_f5_voice.py. F5-TTS was removed because it declared
`torchcodec`, which loads FFmpeg shared libraries at import; Windows ships no
FFmpeg, so a 1.4 GB install completed and then died on a missing DLL. Most of
these tests exist to stop that shape of failure coming back in a new costume:

    * a probe must never claim READY - only a real load can
    * a reported success must be backed by real audio, not by a file listing
    * the pins must be pins (checked by VERSION, not by "does it import")
    * the weights must live in one place, not two
    * the runtime must not be able to reach the network

None of these need Chatterbox, torch or a GPU. They read the source and the
installer's own declarations, which is what makes them runnable in CI on a
machine that will never synthesise a word.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BRIDGE = ROOT / "voice" / "chatterbox_bridge.py"
ENGINE = ROOT / "scripts" / "setup_voice_engine.py"
ORCHESTRATOR = ROOT / "scripts" / "setup_voice.py"


def code_only(path: Path) -> str:
    """The source with every comment and docstring removed.

    These tests assert that certain names do NOT appear. Without this, the
    comment explaining why torchcodec is not installed fails the test that
    checks torchcodec is not installed - which would train the next person to
    delete the explanation rather than keep the guarantee.

    ast.unparse is what does it: parsing and re-emitting drops comments
    entirely, and the docstrings are removed from the tree first. What comes
    back is every string literal that is actually a VALUE, and nothing that is
    only prose about the code.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            # A function whose entire body is a docstring still needs a body.
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def normalised(path: Path) -> str:
    """Source with runs of whitespace collapsed, so a sentence that a line
    break happens to split is still findable."""
    return " ".join(path.read_text(encoding="utf-8").split())


def _load(path: Path, name: str):
    """Import a script by path without running it as __main__."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    return _load(ENGINE, "setup_voice_engine_under_test")


# ======================================================= F5 is really gone ==

def test_no_f5_files_remain():
    for orphan in ("voice/f5_bridge.py", "scripts/setup_f5_voice.py",
                   "setup_f5_voice.bat", "tests/test_f5_voice.py"):
        assert not (ROOT / orphan).exists(), f"{orphan} should have been removed"


def test_nothing_still_calls_the_f5_installer():
    """A dangling reference to a deleted installer is a runtime failure that
    only appears when somebody runs the thing that references it."""
    for name in ("scripts/install_everything.py", "scripts/setup_voice.py",
                 "INSTALL.bat", "SETUP_VOICE.bat", "voice/providers.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "setup_f5_voice" not in source, f"{name} still calls setup_f5_voice"
        assert "f5_bridge" not in source, f"{name} still calls f5_bridge"


def test_torchcodec_is_never_installed():
    """The dependency that broke the previous engine. torchcodec loads FFmpeg
    shared libraries at import, and Windows ships no FFmpeg.

    The installer MENTIONS torchcodec a lot - it uninstalls it if some other
    package drags it in, and its diagnostics recognise the DLL error by name.
    Those are defences. What must never happen is torchcodec appearing in
    something being INSTALLED, so that is what this checks.
    """
    import ast

    for name in ("scripts/setup_voice_engine.py", "scripts/setup_voice.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            words = [element.value for element in node.elts
                     if isinstance(element, ast.Constant)
                     and isinstance(element.value, str)]
            if "install" not in words:
                continue
            assert not any("torchcodec" in word for word in words), (
                f"{name} pip-installs torchcodec: {words}")

    for name in ("requirements.txt", "requirements-voice.txt"):
        path = ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                assert "torchcodec" not in stripped, f"{name}: {stripped}"


def test_the_installer_removes_torchcodec_if_something_drags_it_in(engine):
    """Chatterbox does not require it, but a transitive dependency could. The
    installer checks after the fact rather than trusting the resolver."""
    source = ENGINE.read_text(encoding="utf-8")
    assert '"uninstall", "-y", "torchcodec"' in source, (
        "nothing removes torchcodec if a dependency pulls it in")


# ============================================== the pins are actually pins ==

def test_torch_is_pinned_to_what_chatterbox_requires(engine):
    """chatterbox-tts declares torch==2.6.0 as a hard equality. Installing
    anything else and then installing chatterbox simply moves torch back, so a
    different value here is not a preference - it is a broken install."""
    assert engine.TORCH_VERSION == "2.6.0"


def test_chatterbox_version_is_pinned(engine):
    """Unpinned, a future release with different requirements can change the
    torch underneath a working install. That is exactly how F5-TTS broke."""
    assert engine.CHATTERBOX_VERSION
    parts = engine.CHATTERBOX_VERSION.split(".")
    assert len(parts) >= 2 and all(p.isdigit() for p in parts), (
        "the version must be exact, not a range")


def test_the_installer_checks_the_version_not_just_the_import(engine):
    """`installed_in_env("torch")` answers "does torch import", which is not
    the question a pin asks. A torch 2.14 already in the environment sailed
    past a step whose entire purpose was pinning 2.6."""
    assert hasattr(engine, "installed_version")
    source = ENGINE.read_text(encoding="utf-8")
    assert "installed_version(" in source
    install_torch = source[source.index("def install_torch("):
                            source.index("def install_chatterbox(")]
    assert "installed_version" in install_torch, (
        "install_torch decides whether to act from whether torch imports, "
        "which cannot detect a wrong version")
    assert "--force-reinstall" in install_torch, (
        "a pin that only holds upwards is not a pin")


def test_the_cpu_build_is_chosen_when_there_is_no_gpu(engine):
    index, why = engine.torch_index({"gpu": None})
    assert index.endswith("/cpu")
    assert "CPU" in why


def test_an_unreadable_driver_does_not_get_a_guessed_cuda_build(engine):
    """A GPU whose driver version cannot be read gets the CPU build. Guessing
    risks a torch that imports and then fails at the first kernel launch."""
    index, why = engine.torch_index({"gpu": "NVIDIA Something", "driver": ""})
    assert index.endswith("/cpu")
    assert "driver" in why.lower()


def test_an_explicit_cpu_flag_wins_over_detection(engine):
    index, _ = engine.torch_index({"gpu": "NVIDIA RTX 4090", "driver": "555.1"},
                                  force="cpu")
    assert index.endswith("/cpu")


def test_an_explicit_cuda_flag_wins_over_detection(engine):
    index, _ = engine.torch_index({"gpu": None}, force="cuda")
    assert "cu" in index.rsplit("/", 1)[-1]


# ============================================ one copy of the model weights ==

def test_the_installer_and_the_runtime_agree_where_the_model_lives(engine):
    """Two directories holding the same 3.2 GB is a bug whose only symptom is
    a full disk. The installer must write where the runtime reads."""
    from voice.offline import resolve_chatterbox_dir
    assert Path(engine.MODEL_DIR).resolve() == resolve_chatterbox_dir().resolve()


def test_the_provider_reads_the_same_directory():
    from voice.offline import resolve_chatterbox_dir
    from voice.providers import ChatterboxBridgeProvider
    assert (ChatterboxBridgeProvider.model_dir().resolve()
            == resolve_chatterbox_dir().resolve())


def test_the_environment_is_not_inside_the_model_directory():
    """Uninstalling the environment must not take the weights with it, and
    re-downloading the weights must not rebuild the environment."""
    from voice.providers import ChatterboxBridgeProvider
    env = ChatterboxBridgeProvider.home().resolve()
    model = ChatterboxBridgeProvider.model_dir().resolve()
    assert env != model
    assert model not in env.parents


def test_only_the_five_files_chatterbox_loads_are_fetched(engine):
    """The repository carries other revisions and languages. Fetching all of
    it would add gigabytes nobody asked for."""
    from voice.offline import CHATTERBOX_REQUIRED_FILES
    fetched = {name for name, _, _ in engine.MODEL_FILES}
    assert fetched == set(CHATTERBOX_REQUIRED_FILES), (
        "the installer downloads a different set of files than the loader "
        "requires - one of the two lists is wrong")


def test_the_declared_download_sizes_are_plausible(engine):
    """The size estimates drive the progress display and the 'do you have room'
    warning. A zero or a wildly wrong figure makes both lie."""
    total = sum(size for _, _, size in engine.MODEL_FILES)
    assert 2.5e9 < total < 5e9, f"{total / 1e9:.1f} GB is not the Chatterbox model"
    for name, _, size in engine.MODEL_FILES:
        assert size > 0, f"{name} has no declared size"


# ================================================ the bridge stays isolated ==

def test_the_bridge_imports_nothing_from_medha():
    """It runs in an interpreter where core, ui and industrial are not
    importable at all. One `from core...` makes every utterance fail."""
    source = BRIDGE.read_text(encoding="utf-8")
    for forbidden in ("from core", "import core", "from ui", "from industrial",
                      "from memory", "from voice.", "from auth"):
        assert forbidden not in source, f"the bridge imports ZEUS: {forbidden}"


def test_the_bridge_never_downloads_a_model():
    """`from_pretrained` reaches Hugging Face on a cache miss, which would
    break the offline guarantee at the moment somebody is relying on it."""
    source = code_only(BRIDGE)
    assert "from_local" in source
    assert "from_pretrained" not in source, (
        "from_pretrained can download at runtime; from_local cannot")


def test_offline_flags_are_set_above_the_heavy_imports():
    """huggingface_hub decides its offline behaviour AT IMPORT TIME. Setting
    the flags after importing torch or chatterbox sets them too late."""
    source = BRIDGE.read_text(encoding="utf-8")
    flag = source.index("HF_HUB_OFFLINE")
    for heavy in ("import torch", "import chatterbox", "from chatterbox"):
        position = source.find(heavy)
        if position != -1:
            assert flag < position, f"HF_HUB_OFFLINE is set after `{heavy}`"


def test_the_bridge_keeps_stdout_clean_for_the_protocol():
    """torch and transformers print banners and progress bars. One stray
    character on stdout makes the JSON reply unparseable."""
    source = BRIDGE.read_text(encoding="utf-8")
    assert "_REAL_STDOUT = sys.stdout" in source
    assert "sys.stdout = sys.stderr" in source
    assert source.index("_REAL_STDOUT = sys.stdout") < \
        source.index("sys.stdout = sys.stderr"), (
        "the real stdout must be captured before it is redirected")
    assert "_REAL_STDOUT.write" in source, "the reply must go to the real stdout"


def test_the_bridge_opens_no_network_service():
    """--serve means 'keep the model loaded and read stdin', not 'listen on a
    port'. A voice service on a network interface is an exposure nobody asked
    for on an air-gapped plant machine."""
    import ast

    source = code_only(BRIDGE).lower()
    for forbidden in ("socket", "bind(", "0.0.0.0", "flask", "fastapi",
                      "uvicorn", "http.server", "listen("):
        assert forbidden not in source, (
            f"the bridge opens or imports {forbidden}")

    # gradio must not be IMPORTED. It is named once, to switch its telemetry
    # off in case chatterbox imports it - which is the opposite of a problem.
    imported = set()
    for node in ast.walk(ast.parse(BRIDGE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("gradio", "flask", "fastapi", "uvicorn", "socket",
                      "requests", "httpx", "urllib"):
        assert forbidden not in imported, f"the bridge imports {forbidden}"


def test_the_bridge_disables_telemetry_for_anything_it_pulls_in():
    """chatterbox depends on gradio. If some code path imports it, it must
    not phone home from a plant machine."""
    source = BRIDGE.read_text(encoding="utf-8")
    assert "GRADIO_ANALYTICS_ENABLED" in source
    assert "HF_HUB_DISABLE_TELEMETRY" in source


def test_a_missing_reference_clip_is_not_a_reason_to_go_silent():
    """Chatterbox ships a default speaker in conds.pt. An absent recording
    means 'use the built-in voice', not 'fail'."""
    source = BRIDGE.read_text(encoding="utf-8")
    assert 'reference = ""' in source


# =========================================== the honesty invariant, again ==

def test_probe_can_never_report_ready():
    """probe() looks at the filesystem. Files being present is not a model
    that loads, so the only state a probe may reach is NOT_PROBED. This is
    the invariant that keeps 'Voice Ready' from being a directory listing."""
    from voice.providers import ChatterboxBridgeProvider, ProviderState
    source = ENGINE.read_text(encoding="utf-8")   # touch, so the file is read
    assert source
    probe_source = (ROOT / "voice" / "providers.py").read_text(encoding="utf-8")
    start = probe_source.index("class ChatterboxBridgeProvider")
    end = probe_source.index("def activate", start)
    body = probe_source[start:end]
    assert "ProviderState.READY" not in body, (
        "a filesystem probe claims READY - only a real load may do that")

    report = ChatterboxBridgeProvider().probe()
    assert report.state is not ProviderState.READY
    assert report.detail, "a probe that is not READY must say why"


def test_an_uninstalled_voice_reports_installable_not_broken(tmp_path,
                                                             monkeypatch):
    """'Not installed yet' and 'installed and broken' need different words,
    because they need different actions from the reader."""
    from voice import providers
    from voice.providers import ChatterboxBridgeProvider, ProviderState
    monkeypatch.setattr(providers, "resolve_models_dir", lambda: tmp_path)
    report = ChatterboxBridgeProvider().probe()
    assert report.state is ProviderState.INSTALLABLE
    assert "SETUP_VOICE.bat" in report.detail, (
        "the detail must name the thing that fixes it")


def test_the_provider_order_puts_chatterbox_first():
    from voice.providers import OUTPUT_ORDER
    assert OUTPUT_ORDER[0] == "chatterbox"
    assert "system" in OUTPUT_ORDER, "there must be a last-resort fallback"


def test_the_request_matches_what_the_bridge_reads():
    """The provider used to send F5's vocab_file, nfe and reference_text. The
    bridge ignored them, so config.ini could be changed all day with no
    audible effect and nothing to say why."""
    provider = (ROOT / "voice" / "providers.py").read_text(encoding="utf-8")
    start = provider.index("        request = {")
    end = provider.index("}", provider.index("\"device\"", start))
    request_block = provider[start:end]
    bridge = BRIDGE.read_text(encoding="utf-8")
    for key in ("text", "output", "model_dir", "reference", "device"):
        assert f'"{key}"' in request_block, f"the provider does not send {key}"
        assert f'"{key}"' in bridge, f"the bridge does not read {key}"
    for dead in ("vocab_file", "nfe", "reference_text", "trim_silence"):
        assert dead not in request_block, (
            f"{dead} is an F5 key the Chatterbox bridge ignores")


# ============================================== verification means audio ==

def test_verification_requires_real_audio(engine):
    """The final report says READY only after a sentence has been synthesised
    into a file with real duration. Anything less is a claim, not a check."""
    source = ENGINE.read_text(encoding="typing" and "utf-8")
    verify = source[source.index("def verify("):source.index("def _diagnose(")]
    assert "wave" in verify or "getnframes" in verify or "seconds" in verify, (
        "verify() does not look at the audio it claims was produced")
    assert '"synthesis"' in source, "there is no synthesis check to report"


def test_a_too_short_clip_is_rejected(engine, tmp_path):
    """A synthesiser that writes a 40-byte header counts as a failure. The
    previous engine's reference step accepted one and reported success."""
    silent = tmp_path / "tiny.wav"
    with wave.open(str(silent), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 2400)          # 0.1 seconds
    assert engine._usable_wav(silent) is False


def test_a_long_enough_clip_is_accepted(engine, tmp_path):
    good = tmp_path / "good.wav"
    with wave.open(str(good), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x01\x00" * 24000 * 3)     # 3 seconds
    assert engine._usable_wav(good) is True


def test_an_unreadable_file_is_not_usable(engine, tmp_path):
    junk = tmp_path / "notaudio.wav"
    junk.write_bytes(b"this is not a wav file at all")
    assert engine._usable_wav(junk) is False


def test_a_missing_file_is_not_usable(engine, tmp_path):
    assert engine._usable_wav(tmp_path / "absent.wav") is False


# ================================================ the installer's contract ==

def test_the_reference_clip_is_optional(engine):
    """Chatterbox has a built-in speaker. Treating a missing clip as fatal is
    what stranded the previous engine after a completed multi-gigabyte
    download."""
    source = ENGINE.read_text(encoding="utf-8")
    profile = source[source.index("def write_profile("):
                     source.index("def _make_reference(")]
    assert "return True" in profile
    assert "Console.warn" in profile, (
        "a missing reference clip must warn, not fail")
    assert "built-in speaker" in profile


def test_the_installer_does_not_ship_a_real_persons_voice(engine):
    """Cloning a voice without authorisation is not something this project
    does. The generated reference is synthetic, and the note says so."""
    source = normalised(ENGINE)
    assert "authorised" in source or "authorized" in source
    assert "does NOT ship a recording of a real person" in source, (
        "the installer no longer says it does not clone a real person")


def test_uninstall_touches_only_the_voice(engine):
    source = ENGINE.read_text(encoding="utf-8")
    body = source[source.index("def do_uninstall("):source.index("def do_install(")]
    assert "ENV_DIR" in body and "MODEL_DIR" in body
    assert "ROOT" not in body.replace("ROOT / ", ""), (
        "uninstall must not reach outside the voice directories")
    assert "REMOVE" in body, "a destructive step must be confirmed"


def test_python_range_matches_what_chatterbox_publishes_wheels_for(engine):
    assert engine.MIN_PYTHON >= (3, 10), (
        "chatterbox-tts requires Python 3.10 or newer")
    assert engine.MAX_PYTHON <= (3, 13)


def test_the_orchestrator_delegates_rather_than_duplicating():
    """One place for the model URLs, one place for the venv logic. Two copies
    means two places to fix a bug and one of them always gets missed."""
    assert "setup_voice_engine.py" in ORCHESTRATOR.read_text(encoding="utf-8")
    assert "setup_models.py" in ORCHESTRATOR.read_text(encoding="utf-8")
    source = code_only(ORCHESTRATOR)
    for duplicated in ("ResembleAI/chatterbox", "Systran/faster-whisper",
                       "download.pytorch.org", "torch==2.6.0"):
        assert duplicated not in source, (
            f"{duplicated} is hard-coded in the orchestrator as well as in the "
            f"script that owns it")


def test_the_orchestrator_installs_chatterbox_outside_medhas_environment():
    """The whole reason the engine is a separate script. If setup_voice.py
    ever pip-installs chatterbox or torch into the running interpreter, the
    numpy conflict is back."""
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    start = source.index("STT_PACKAGES = [")
    end = source.index("]", source.index("Whisper", start))
    packages = source[start:end]
    for forbidden in ("torch", "chatterbox", "transformers", "diffusers"):
        assert forbidden not in packages, (
            f"{forbidden} is installed into ZEUS's own environment")


def test_pip_is_invoked_through_the_running_interpreter():
    """Bare `pip` belongs to whichever Python is first on PATH. On a laptop
    with two Pythons that is how packages end up installed for an interpreter
    ZEUS is not using - which cost an afternoon."""
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    assert 'sys.executable, "-m", "pip"' in source
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "say(" in stripped:
            continue
        assert not stripped.startswith('"pip'), f"bare pip: {stripped}"


def test_check_mode_changes_nothing():
    """--check is what a nervous operator runs first. It must not install."""
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    main = source[source.index("def main("):]
    check = main.index("args.check")
    install = main.index("install_listening(")
    assert check < install, "--check is handled after the install begins"
    assert "return report_check()" in main


# ======================================================= the name and tagline ==

def test_the_tagline_is_defined_once():
    from core import branding
    assert branding.APP_NAME == "ZEUS"
    assert branding.APP_TAGLINE == "Autonomous AI Command & Intelligence System"
    assert branding.APP_TAGLINE in branding.banner()
    assert branding.APP_NAME in branding.banner()


def test_the_voice_scripts_show_the_name_with_its_tagline():
    for name in ("scripts/setup_voice.py", "scripts/setup_voice_engine.py",
                 "SETUP_VOICE.bat"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "branding" in source or "Autonomous AI Command & Intelligence System" in source, (
            f"{name} writes the product name without saying what it is")


# The four places ZEUS may still be named, and why. Anywhere else is a
# leftover: a path that no longer exists, or a label nobody renamed.
MIGRATION_FILES = {
    "memory/database.py",        # carries database/jarvis.db forward
    "RESTORE_MY_DATA.py",        # copies it out of the old folder
    "START_HERE.md",             # tells the operator where to find it
    "VOICE_SETUP.md",            # explains why F5-TTS was replaced
    "README.md",                 # notes that the database is carried forward
    "database/README.txt",       # only in the package: explains the rename
    "tests/test_chatterbox_voice.py",
}


def test_nothing_is_still_called_jarvis():
    """The rename has to be complete, not mostly complete. A leftover
    `jarvis.db` or `start_jarvis.bat` is a path that no longer exists, and a
    leftover window title is a product with two names.

    Four files name it on purpose: the ones that carry an existing
    installation's data forward, and the ones that explain the change to the
    person reading. Those are listed, so adding a fifth is a decision rather
    than an accident.
    """
    # logs/ and config/industrial_environment.json are GENERATED on the
    # machine that runs ZEUS. They record the absolute path they were written
    # from, so on the developer's machine they legitimately contain the old
    # directory name - and shipping either of them would hand the operator a
    # stale snapshot of somebody else's filesystem.
    skip = {"__pycache__", ".git", ".pytest_cache", "env", "venv", "logs",
            "node_modules"}
    generated = {"config/industrial_environment.json"}
    suffixes = {".py", ".bat", ".ini", ".md", ".txt", ".json"}

    offenders = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in skip for part in relative.parts):
            continue
        if relative.as_posix() in generated or not path.is_file():
            continue
        if "jarvis" in path.name.lower():
            offenders.append(f"{relative} (filename)")
        if path.suffix.lower() not in suffixes:
            continue
        if relative.as_posix() in MIGRATION_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "jarvis" in text.lower():
            offenders.append(f"{relative} (contents)")
    assert not offenders, "still ZEUS: " + ", ".join(sorted(offenders)[:10])


def test_the_files_that_still_say_jarvis_are_only_about_the_rename():
    """The allowlist above is not a licence to leave the old name lying
    around. Every mention in those files has to be ABOUT the migration or the
    history - not a live path, a window title or a command."""
    live = ("start_jarvis", "jarvis.log", "JARVIS_MODELS_DIR", "JARVIS_TTS",
            "JARVIS_STT", "setup_jarvis", "import jarvis")
    for name in sorted(MIGRATION_FILES):
        path = ROOT / name
        # This file is where the forbidden list is written down, so of course
        # it contains every entry.
        if not path.is_file() or path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in live:
            assert forbidden not in text, (
                f"{name} still uses the live name `{forbidden}`")


def test_the_migration_script_exists_and_handles_the_rename():
    """START_HERE.md tells the operator to run this. A documented command that
    does not exist is the worst possible first impression."""
    script = ROOT / "RESTORE_MY_DATA.py"
    assert script.is_file(), "START_HERE.md tells the operator to run a missing script"
    source = script.read_text(encoding="utf-8")
    assert "database/jarvis.db" in source, (
        "the migration script does not look for the old database name")
    assert "database/medha.db" in source


def test_the_migration_script_never_modifies_the_old_folder():
    """It is the operator's safety net. If it moved or deleted anything, a
    failed migration would have nowhere to go back to."""
    import ast

    source = (ROOT / "RESTORE_MY_DATA.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    for destructive in ("move", "unlink", "rename", "replace"):
        assert destructive not in called, (
            f"the migration script calls {destructive}() - it must only copy")
    assert "copy2" in called or "copytree" in called


def test_the_migration_script_does_not_copy_code():
    """Importing an old copy of the application into a new one is how you end
    up running yesterday's bugs against today's database."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "restore_under_test", ROOT / "RESTORE_MY_DATA.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert ".py" in module.NEVER
    assert "__pycache__" in module.NEVER_DIRS
    assert "site-packages" in module.NEVER_DIRS
    for target, _, _ in module.ITEMS:
        assert not target.endswith(".py"), f"{target} is code, not data"


def test_the_migration_script_carries_the_account_across(tmp_path):
    """The whole point, run for real: an old folder with jarvis.db, a new one
    without, and the account present afterwards under the new name."""
    import importlib.util
    import shutil

    old = tmp_path / "MEDHA_OLD"
    (old / "database").mkdir(parents=True)
    (old / "main.py").write_text("print('old')", encoding="utf-8")
    (old / "database" / "jarvis.db").write_bytes(b"SQLite format 3\x00" + b"x" * 400)

    new = tmp_path / "ZEUS"
    new.mkdir()
    shutil.copy2(ROOT / "RESTORE_MY_DATA.py", new / "RESTORE_MY_DATA.py")

    spec = importlib.util.spec_from_file_location(
        "restore_run", new / "RESTORE_MY_DATA.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = module.find_source(old, "database/medha.db", ["database/jarvis.db"])
    assert source is not None and source.name == "jarvis.db"

    copied, detail = module.bring(source, new / "database" / "medha.db",
                                  force=False)
    assert copied, detail
    assert (new / "database" / "medha.db").is_file()
    assert (old / "database" / "jarvis.db").is_file(), (
        "the old database was moved, not copied")


def test_the_old_database_is_carried_forward_not_abandoned(tmp_path,
                                                          monkeypatch):
    """An installation that predates the rename holds the operator's account
    in database/jarvis.db. Pointing at the new name and finding nothing would
    have shown them a first-run 'create your account' screen with their real
    account sitting on disk beside it."""
    import importlib
    from memory import database

    directory = tmp_path / "database"
    directory.mkdir()
    legacy = directory / database.LEGACY_DB_NAME
    legacy.write_bytes(b"SQLite format 3\x00" + b"\x00" * 80)
    (directory / (database.LEGACY_DB_NAME + "-wal")).write_bytes(b"wal")

    # The data folder now comes from core.paths (AGNI_DATA_DIR, default
    # <install>/database), so point that at the old installation's folder.
    monkeypatch.setenv("AGNI_DATA_DIR", str(directory))
    resolved = database._resolve_db_path()

    assert resolved.name == database.DB_NAME
    assert resolved.is_file(), "the old database was not carried forward"
    assert not legacy.exists(), "the old file should have been renamed, not copied"
    assert (directory / (database.DB_NAME + "-wal")).is_file(), (
        "the write-ahead log was left behind, which discards uncommitted rows")
    assert importlib  # keep the import meaningful to a reader


def test_a_fresh_installation_gets_the_new_name(tmp_path, monkeypatch):
    from memory import database
    (tmp_path / "database").mkdir()
    monkeypatch.setattr(database, "ROOT", tmp_path)
    assert database._resolve_db_path().name == database.DB_NAME


def test_the_new_database_wins_when_both_exist(tmp_path, monkeypatch):
    """Two databases cannot be reconciled automatically. Picking rows from
    each would lose data in a way nobody could see."""
    from memory import database
    directory = tmp_path / "database"
    directory.mkdir()
    (directory / database.LEGACY_DB_NAME).write_bytes(b"old")
    (directory / database.DB_NAME).write_bytes(b"new")
    monkeypatch.setattr(database, "ROOT", tmp_path)

    resolved = database._resolve_db_path()
    assert resolved.name == database.DB_NAME
    assert (directory / database.LEGACY_DB_NAME).read_bytes() == b"old", (
        "the old database must be left untouched, not merged or deleted")


# ============================================== the generated deliverables ==

def test_generated_documents_carry_the_name_and_the_tagline():
    """A deck that leaves the building is the most-seen surface the product
    has. The name without its expansion tells a reader nothing."""
    for name in ("industrial/deck_builder.py", "industrial/report_builder.py",
                 "industrial/workbook_builder.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "BRAND_TAGLINE" in source, f"{name} does not show the tagline"
        assert "from core.branding import" in source, (
            f"{name} should not retype the tagline")


def test_the_config_documents_the_chatterbox_dials():
    config = (ROOT / "config.ini").read_text(encoding="utf-8")
    for key in ("tts_exaggeration", "tts_cfg_weight", "tts_temperature"):
        assert key in config
    assert "F5" not in config, "config.ini still describes the removed engine"


def test_the_config_dials_reach_the_synthesiser(tmp_path, monkeypatch):
    """These three keys are documented in config.ini. A documented key that
    does nothing is worse than no key: it costs an hour to discover."""
    from voice import offline, providers

    profile_dir = tmp_path / "voice" / "profiles" / "medha"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.json").write_text(json.dumps({
        "name": "medha", "exaggeration": 0.5, "cfg_weight": 0.5,
        "temperature": 0.8}), encoding="utf-8")

    monkeypatch.setattr(providers, "resolve_models_dir", lambda: tmp_path)
    monkeypatch.setattr(offline, "_config_cache", {
        "voice_profile": "medha", "tts_exaggeration": "0.85",
        "tts_cfg_weight": "0.25", "tts_temperature": "1.1"})

    loaded = providers.ChatterboxBridgeProvider()._load_profile()
    assert loaded["exaggeration"] == 0.85
    assert loaded["cfg_weight"] == 0.25
    assert loaded["temperature"] == 1.1


def test_an_empty_config_dial_does_not_erase_the_profile_value(tmp_path,
                                                              monkeypatch):
    """config.ini ships these keys EMPTY, meaning 'use the profile'. An empty
    value must not overwrite 0.5 with None and crash the bridge on float()."""
    from voice import offline, providers

    profile_dir = tmp_path / "voice" / "profiles" / "medha"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.json").write_text(json.dumps({
        "name": "medha", "exaggeration": 0.5}), encoding="utf-8")

    monkeypatch.setattr(providers, "resolve_models_dir", lambda: tmp_path)
    monkeypatch.setattr(offline, "_config_cache", {
        "voice_profile": "medha", "tts_exaggeration": "",
        "tts_cfg_weight": "", "tts_temperature": ""})

    loaded = providers.ChatterboxBridgeProvider()._load_profile()
    assert loaded["exaggeration"] == 0.5
    assert loaded.get("cfg_weight") is None or isinstance(
        loaded.get("cfg_weight"), float)


# ================================================ what rendering found ==
# These come from converting a deck and a report to PDF and LOOKING at them.
# No structural assertion catches text that overflows its box: the file is
# valid, the shape count is right, and the words are all present - one of them
# is just printed on top of the thing below it.

def test_a_long_kpi_value_shrinks_instead_of_wrapping():
    """"Maintenance" at 23pt is 1.94 inches wide in a 1.93-inch card. It wrapped
    after "Maintenanc" and put the "e" over the note underneath, which is the
    line that says what the number means."""
    from industrial.deck_builder import _value_size

    card = 1.931          # the real KPI card width on a five-card row
    assert _value_size("2.32", card) == 23.0, "a number should not be shrunk"
    assert _value_size("Maintenance", card) < 23.0
    assert _value_size("Decommissioned", card) < _value_size("Maintenance", card)
    assert _value_size("", card) == 23.0
    assert _value_size("Maintenance", card) >= 12.0, "it must stay readable"


def test_the_report_shrinks_a_long_kpi_value_too():
    """The same value, the same defect, a different file format. Word has no
    shrink-to-fit for a run, so the size is chosen before it is written."""
    from industrial.report_builder import _value_points

    assert _value_points("2.32", 5) == 18.0
    assert _value_points("Maintenance", 5) < 18.0
    assert _value_points("Decommissioned", 5) >= 9.0


def test_the_deck_and_the_report_agree_about_width():
    """One analysis must look like one family of documents. If the two
    estimates drift apart, the same KPI is set at two visibly different sizes
    on the cover of the report and the first slide of the deck."""
    from industrial import deck_builder, report_builder
    assert (deck_builder._BOLD_WIDTH_FACTOR
            == report_builder._BOLD_WIDTH_FACTOR)


def test_the_brand_lockup_survives_a_rename():
    """The title slide carried "J A R V I S" - letter-spaced, so a
    project-wide search for "ZEUS" could not find it. It went out on the
    front page of every generated deck.

    This looks for the spaced form specifically, in every file that draws
    anything, so the next rename cannot leave the same fossil behind.
    """
    import re

    spaced = re.compile(r"[Jj]\W{0,3}[Aa]\W{0,3}[Rr]\W{0,3}[Vv]\W{0,3}[Ii]\W{0,3}[Ss]")
    # The interface is ui/medha (the previous Qt windows were removed 2026-09-27).
    drawn = ["industrial/deck_builder.py", "industrial/report_builder.py", "industrial/workbook_builder.py",
             "ui/agni_shell.py"] + sorted(str(p.relative_to(ROOT)).replace("\\", "/")
                                          for p in (ROOT / "ui" / "medha").rglob("*")
                                          if p.suffix in (".js", ".css", ".html"))
    for name in drawn:
        text = (ROOT / name).read_text(encoding="utf-8")
        found = spaced.search(text)
        assert not found, f"{name} still spells out the old name: {found.group(0)!r}"


def test_the_deck_titles_its_provenance_page_honestly():
    """With no decision to put, the last slide is the provenance page. It was
    headed "Decision - what is being asked for?" over a list of source tables,
    which asks a question the slide never answers."""
    source = (ROOT / "industrial" / "deck_builder.py").read_text(encoding="utf-8")
    ask = source[source.index("def ask_slide("):source.index("def _footer(")
                 if "def _footer(" in source[source.index("def ask_slide("):]
                 else len(source)]
    assert "Provenance" in ask, "the provenance page still calls itself Decision"
    assert "decision_request" in ask
