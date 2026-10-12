"""
The previous interface is gone, and the rules it was tested for still hold in
the ZEUS interface.

"No sign of the previous UI": its folders, modules and assets are removed and
nothing imports them. The measured fixes its tests pinned (no endless
animation, polling that never overlaps and rests while hidden, a voice line
that never claims a GPU it has not got) are checked here against ui/medha.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZEUS = ROOT / "ui" / "medha"

PREVIOUS = ["ui/web", "ui/access.py", "ui/access_motion.py", "ui/command_center.py", "ui/dashboard.py",
            "ui/panels.py", "ui/widgets.py", "ui/hud_widgets.py", "ui/backdrop.py", "ui/void_wave.py",
            "ui/theme.py", "ui/approval_dialog.py", "ui/resources/backdrop.jpg"]
PREVIOUS_MODULES = ("access", "access_motion", "command_center", "dashboard", "panels", "widgets",
                    "hud_widgets", "backdrop", "void_wave", "theme", "approval_dialog")


def _sources():
    for path in ROOT.rglob("*.py"):
        if any(part in (".venv", "venv", "__pycache__", "release", "dist", "build") for part in path.parts):
            continue
        yield path


def test_the_previous_interface_files_are_gone():
    present = [p for p in PREVIOUS if (ROOT / p).exists()]
    assert not present, f"previous interface still on disk: {present}"


def test_nothing_imports_the_previous_interface():
    pattern = re.compile(r"(from\s+ui\s+import\s+(%s)\b|(from|import)\s+ui\.(%s)\b)"
                         % ("|".join(PREVIOUS_MODULES), "|".join(PREVIOUS_MODULES)))
    offenders = []
    for path in _sources():
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_the_window_serves_only_the_medha_interface():
    from ui import agni_shell
    assert agni_shell.WEB_ROOT == ZEUS and (ZEUS / "index.html").is_file()
    assert "ui/web" not in (ROOT / "ui" / "agni_shell.py").read_text(encoding="utf-8").replace("\\", "/")


def test_the_old_start_flags_do_not_bring_back_an_old_window():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    for gone in ("ui.dashboard", "ui.command_center", "ui.access", "prompt_login"):
        assert gone not in source, gone


# ------------------------------------------------ rules carried over to ZEUS --

_TRANSIENT = (".awake", ".scanning", ".active", ".spin", ".loading", ".busy", ".live")


def test_nothing_animates_forever_unless_something_is_happening():
    """An endless CSS animation kept Qt WebEngine repainting at full refresh
    rate forever (measured 2026-09-24). An infinite animation is allowed only
    on a state that ends: a spinner, a loading bar, a live scan, the active
    step of a running analysis, or the sign-in scene while somebody is there."""
    offenders = []
    for css in (ZEUS / "css").glob("*.css"):
        text = re.sub(r"/\*.*?\*/", "", css.read_text(encoding="utf-8"), flags=re.S)
        text = re.sub(r"@keyframes[^{]+\{(?:[^{}]*\{[^}]*\})*[^}]*\}", "", text)
        for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", text):
            if "infinite" in body and "animation" in body:
                if not any(state in selector for state in _TRANSIENT):
                    offenders.append(f"{css.name}: {selector.strip()}")
    assert not offenders, offenders


def test_the_sign_in_scene_settles_when_nobody_is_there():
    auth = (ZEUS / "js" / "auth.js").read_text(encoding="utf-8")
    assert 'root.classList.remove("awake")' in auth and "SETTLE_MS" in auth
    assert "clearTimeout(root._settle)" in auth, "the settle timer must not outlive the screen"
    assert "M.wave.wake(SETTLE_MS)" in auth, "the wave moves only while somebody is at the screen"
    assert "M.wave.sleep()" in auth, "the sign-in wave must not keep running into the workspace"


def test_the_voice_wave_moves_only_while_something_happens():
    """The wave is the design's centrepiece and the old wave's measured faults
    must not come back: per-frame stepping (4x fast on 240 Hz), a second loop
    on every show, a per-frame shadow blur, and running while idle or hidden."""
    wave = (ZEUS / "js" / "voicewave.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", wave, flags=re.S)
    assert "time += 0.016" not in code and "clock += dt * speedK" in code, "advance by elapsed time"
    assert "if (rafId || !wanted(performance.now())) return;" in code, "one loop at most"
    assert "shadowBlur" not in code, "no per-frame blur"
    assert "document.hidden" in code and "reduced" in code
    active = re.search(r"const ACTIVE = new Set\(\[([^\]]*)\]\)", code).group(1)
    assert '"IDLE"' not in active and '"LISTENING"' in active and '"SPEAKING"' in active
    assert "ACTIVE.has(state) || now < awakeUntil || now < surgeUntil" in code


def test_the_wave_follows_the_real_microphone_not_the_page():
    """The page has no microphone: capture is done by the Python voice stack,
    and the wave is driven by the level it reports."""
    wave = (ZEUS / "js" / "voicewave.js").read_text(encoding="utf-8")
    shell = (ZEUS / "js" / "shell.js").read_text(encoding="utf-8")
    assert "getUserMedia" not in wave + shell and "AudioContext" not in wave + shell
    assert "M.wave.level(level)" in shell and 'M.on("level"' in shell


def test_status_polling_never_overlaps_and_rests_while_hidden():
    shell = (ZEUS / "js" / "shell.js").read_text(encoding="utf-8")
    assert "if (statusBusy || document.hidden) return;" in shell
    assert "statusBusy = false" in shell


def test_the_voice_line_never_says_gpu_without_a_cuda_device():
    """'GPU' means the loaded provider reported CUDA - not that the machine
    has a graphics chip, and never from configuration."""
    from ui.agni_shell import voice_summary_from

    def report(device, available=True):
        return {"state": "ready", "output": {"provider": "chatterbox", "available": available,
                                             "evidence": {"device": device}},
                "input": {"provider": "whisper", "available": True}}

    assert voice_summary_from(report("cuda"))["device"] == "GPU"
    assert voice_summary_from(report("cpu"))["device"] == "CPU"
    assert voice_summary_from(report("mps"))["device"] == "MPS"
    assert voice_summary_from(report(""))["device"] is None
    unavailable = voice_summary_from(report("cuda", available=False))
    assert unavailable["can_speak"] is False and unavailable["device"] is None
    shell = (ZEUS / "js" / "shell.js").read_text(encoding="utf-8")
    assert "v.device" in shell and "GPU" not in shell, "the page names the device only from the backend"


def test_speech_state_reaches_the_page_through_the_bridge_signal():
    """The provider reports state from the speaking thread; touching Qt from
    there corrupts it, so the page is told through a queued signal."""
    source = (ROOT / "ui" / "agni_shell.py").read_text(encoding="utf-8")
    assert "provider.on_state_change(self._on_speech)" in source
    assert "self._deliver_event.emit(kind, json.dumps(payload, default=str))" in source


def test_no_previous_brand_or_asset_names_in_the_new_interface():
    names = [p.name for p in ZEUS.rglob("*")]
    for old in ("agni.css", "wave.js", "views.js", "app.js", "backdrop.jpg", "agni.src.css"):
        assert old not in names, old
    assert "medha.ico" in names
