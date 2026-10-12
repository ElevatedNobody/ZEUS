"""
The microphone must stop when the speaker stops.

Regression for 2026-09-25: the microphone button recorded a FIXED 5 s for
every request (VoiceManager.listen_once passed 5.0, so [voice] listen_seconds
was never used): short questions waited out the full five seconds and longer
ones were cut off.
"""

from __future__ import annotations

from voice.speech_to_text import EndOfSpeech

BLOCK = 0.05          # 50 ms blocks, as captured


def _run(levels, wait=4.0, longest=15.0):
    detector = EndOfSpeech(BLOCK, wait, max_seconds=longest)
    for index, rms in enumerate(levels):
        if detector.feed(rms):
            return round((index + 1) * BLOCK, 2), detector.heard
    return None, detector.heard


def _seconds(rms, seconds):
    return [rms] * int(round(seconds / BLOCK))


def test_a_short_question_ends_soon_after_the_speaker_stops():
    levels = _seconds(0.002, 0.5) + _seconds(0.08, 1.5) + _seconds(0.003, 3.0)
    stopped, heard = _run(levels)
    assert heard
    assert stopped is not None and 2.6 <= stopped <= 2.8, stopped   # 0.5 + 1.5 + ~0.7


def test_nobody_speaking_still_ends_at_listen_seconds():
    stopped, heard = _run(_seconds(0.002, 10.0))
    assert not heard and stopped == 4.0


def test_a_long_question_is_not_cut_off_at_five_seconds():
    levels = _seconds(0.002, 0.3) + _seconds(0.07, 9.0) + _seconds(0.002, 2.0)
    stopped, heard = _run(levels)
    assert heard and stopped is not None and stopped > 9.0


def test_short_pauses_between_words_do_not_end_the_request():
    speech = []
    for _ in range(4):
        speech += _seconds(0.07, 0.8) + _seconds(0.003, 0.4)       # 0.4 s pauses
    stopped, _ = _run(_seconds(0.002, 0.3) + speech + _seconds(0.003, 2.0))
    assert stopped is not None and stopped >= 0.3 + 4 * 1.2


def test_endless_speech_stops_at_the_maximum():
    stopped, heard = _run(_seconds(0.002, 0.3) + _seconds(0.07, 30.0), longest=15.0)
    assert heard and stopped == 15.0


def test_a_noisy_room_is_not_mistaken_for_a_voice():
    """A plant floor at RMS 0.02 is above the fixed speech level; judged
    against its own noise floor it is not speech, so capture ends at the
    usual wait instead of running to the maximum."""
    stopped, heard = _run(_seconds(0.02, 20.0))
    assert not heard and stopped == 4.0
    levels = _seconds(0.02, 0.5) + _seconds(0.12, 1.5) + _seconds(0.02, 3.0)
    stopped, heard = _run(levels)
    assert heard and stopped is not None and stopped < 3.0


def test_a_single_click_is_not_the_start_of_speech():
    levels = _seconds(0.002, 1.0) + [0.2] + _seconds(0.002, 5.0)
    stopped, heard = _run(levels)
    assert not heard and stopped == 4.0


def test_the_microphone_button_no_longer_forces_five_seconds(monkeypatch):
    from voice import providers
    received = []

    class Whisper(providers.WhisperInputProvider):
        def __init__(self):
            pass

        def listen_once(self, duration=None):
            received.append(duration)
            return "heard"

    manager = providers.VoiceManager()
    manager.input = Whisper()
    assert manager.listen_once() == "heard"
    assert received == [None], "the Whisper capture must be allowed to end on silence"
