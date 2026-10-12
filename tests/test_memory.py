import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use an isolated test DB so this doesn't touch the real one.
os.environ.setdefault("MEDHA_TEST_MODE", "1")
from memory import database
database.DB_PATH = Path("/tmp/medha_test_memory.db")
if database.DB_PATH.exists():
    database.DB_PATH.unlink()

from memory.memory_manager import MemoryManager


def test_add_and_get_context():
    mm = MemoryManager()
    mm.add_message("user", "hello medha")
    mm.add_message("assistant", "hello sir")
    context = mm.get_context(limit=5)
    assert len(context) >= 2
    assert context[-2]["content"] == "hello medha"
    assert context[-1]["content"] == "hello sir"


def test_remember_extracts_fact():
    mm = MemoryManager()
    mm.add_message("user", "remember that I prefer dark mode")
    facts = mm.all_facts()
    assert any("dark mode" in v for v in facts.values())


def test_recall_and_forget():
    mm = MemoryManager()
    mm.remember("favorite_color", "blue")
    assert mm.recall("favorite_color") == "blue"
    mm.forget("favorite_color")
    assert mm.recall("favorite_color") == ""


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
