"""
Will the voice models fit alongside everything else on this machine?

The arithmetic that matters on a 16 GB laptop with no dedicated GPU:

    Chatterbox   ~3.3 GB resident   t3_cfg 2.13 GB + s3gen 1.06 GB, float32
    Whisper base ~0.2 GB resident   int8 CTranslate2
    a 7B chat model at q4  ~4.5 GB  held by Ollama, in a separate process
    Windows + the ZEUS UI  ~3 GB

That totals around 11 GB of the 16, which fits - but only just, and only if
nothing else large is open. Past that point Windows starts paging, and a
paging machine does not run a 0.5B model slowly, it stops responding.

So this module measures what is ACTUALLY free before loading a voice model,
and says plainly when there is not enough. Refusing to load with a clear
reason is a good outcome; loading anyway and taking the machine down with it
is not.

Nothing here is a hard gate by default - the user can always override it,
because they know what else they have open and this module does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from core.logger import get_logger

logger = get_logger("voice.memory")

# Measured from the checkpoint sizes, plus room for activations. float32 on
# CPU, which is what torch uses there - fp16 on x86 has no native compute
# path and runs slower, so it buys memory at the cost of speed.
CHATTERBOX_GB = 3.6
WHISPER_GB = {"tiny": 0.1, "base": 0.25, "small": 0.7, "medium": 1.8, "large-v3": 3.4}

# Below this much free after loading, Windows is paging and the machine will
# feel broken rather than slow.
HEADROOM_GB = 1.5


@dataclass
class MemoryVerdict:
    ok: bool
    available_gb: float
    needed_gb: float
    detail: str

    def line(self) -> str:
        return (f"{'FITS' if self.ok else 'NOT ENOUGH MEMORY'} — {self.detail}")


def available_gb() -> Optional[float]:
    """Free physical memory, or None if it cannot be measured."""
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception as error:
        logger.debug(f"could not read memory: {error}")
        return None


def check(provider: str, whisper_size: str = "base") -> MemoryVerdict:
    """Is there room for this provider right now?

    `provider` is 'chatterbox' or 'whisper'. An unmeasurable machine gets the
    benefit of the doubt: a missing psutil must not stop voice from working.
    """
    needed = (CHATTERBOX_GB if provider == "chatterbox"
              else WHISPER_GB.get(whisper_size, 0.3))
    free = available_gb()
    if free is None:
        return MemoryVerdict(True, 0.0, needed,
                             "memory could not be measured, proceeding")

    if free >= needed + HEADROOM_GB:
        return MemoryVerdict(
            True, free, needed,
            f"{free:.1f} GB free, {provider} needs about {needed:.1f} GB")

    return MemoryVerdict(
        False, free, needed,
        f"only {free:.1f} GB free and {provider} needs about {needed:.1f} GB "
        f"plus {HEADROOM_GB:.1f} GB headroom - close some applications, or "
        f"unload the chat model first")


def free_the_chat_model() -> Tuple[bool, str]:
    """Ask Ollama to unload its resident model, freeing several GB.

    This is the one lever that reliably makes room on a 16 GB machine: a 7B
    model at q4 is about 4.5 GB, and Ollama keeps it resident for five minutes
    after the last request by default. Sending keep_alive=0 releases it now.

    The model reloads by itself on the next question, which costs a few
    seconds - much cheaper than paging.
    """
    try:
        from core.model_router import model_router
        from core.ollama_client import _post, ollama_client

        if not ollama_client.is_available():
            return False, "the model runtime is not running, nothing to unload"
        model = model_router.route("default")
        _post(f"{ollama_client.host}/api/generate",
              json={"model": model, "prompt": "", "keep_alive": 0},
              timeout=15.0)
        logger.info(f"asked Ollama to unload {model} to make room for voice")
        return True, f"unloaded {model}"
    except Exception as error:
        return False, f"could not unload the chat model: {error}"
