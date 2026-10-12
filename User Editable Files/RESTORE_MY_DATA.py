"""
Bring your data across from an older ZEUS / ZEUS / ZEUS / ZEUS folder.

    python "User Editable Files/RESTORE_MY_DATA.py" "C:\\Users\\you\\Desktop\\OLD_FOLDER"
    python "User Editable Files/RESTORE_MY_DATA.py" "C:\\...\\OLD_FOLDER" --check   report only
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / "core").is_dir() else HERE

BAR = "=" * 70

ITEMS: List[Tuple[str, str, List[str]]] = [
    ("database/medha.db",
     "your account, history, notes, vault and indexed documents",
     ["database/jarvis.db"]),
    ("database/jarvis_projects.db",
     "your ZEUS projects, schedules, events and evidence",
     []),
    ("database/crudex_net.db",
     "your ZEUS network sync state",
     []),
    ("database/vault", "your encrypted vault files", []),
    ("database/industrial_assets", "images extracted from your indexed documents", []),
    ("database/knowledge_fabric.db", "your company knowledge base, entity corrections and analyses", []),
    ("database/knowledge_fabric_archives", "company document archives you added (unpacked)", []),
    ("database/.audit_key", "the key that signs your audit trail and trusted models", []),
    ("database/memory", "your long-term memory (Hermes / native stores)", []),
    ("database/model_status.json", "the verified state and calibration of your models", []),
    ("config/trusted_models.json", "the signed list of trusted model digests", []),
    ("logs/audit_chain.jsonl", "your tamper-evident audit history", []),
    ("logs/audit.log", "your audit log", []),
    ("config.ini", "your settings", []),
    ("knowledge_sources", "documents you added to the knowledge base", []),
    ("knowledge/industrial_history", "the indexed industrial corpus", []),
    ("models/whisper", "the Whisper listening model (145 MB - 1.6 GB)", []),
    ("models/insightface", "the face login models (~330 MB)", []),
    ("models/chatterbox", "the Chatterbox speaking model (~3.2 GB)",
     ["models/voice/chatterbox/model", "models/voice/f5tts/model"]),
    ("models/voice/profiles", "your voice profile and reference recording", []),
    ("models/piper", "Piper voices, if you installed any", []),
    ("models/vosk", "Vosk models, if you installed any", []),
]

NEVER = {".py", ".pyc", ".pyd", ".dll", ".exe", ".bat"}
NEVER_DIRS = {"__pycache__", ".git", ".pytest_cache", "env", "venv", ".venv",
              "node_modules", "site-packages"}


def say(text: str = ""):
    print(text, flush=True)


def human(size: int) -> str:
    for unit, limit in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= limit:
            return f"{size / limit:.1f} {unit}"
    return f"{size} B"


def folder_size(path: Path) -> Tuple[int, int]:
    files, total = 0, 0
    for child in path.rglob("*"):
        if any(part in NEVER_DIRS for part in child.parts):
            continue
        if child.is_file() and child.suffix.lower() not in NEVER:
            try:
                total += child.stat().st_size
                files += 1
            except OSError:
                pass
    return files, total


def find_source(old_root: Path, rel: str, aliases: List[str]) -> Optional[Path]:
    for candidate in [rel] + aliases:
        path = old_root / candidate
        if path.exists():
            return path
    return None


def copy_one(src: Path, dst: Path, force: bool, check_only: bool) -> str:
    if src.is_file():
        if src.suffix.lower() in NEVER:
            return "skipped (executable / code file)"
        size = human(src.stat().st_size)
        if dst.exists() and not force:
            return f"kept existing ({size} in old folder; pass --force to replace)"
        if check_only:
            return f"would copy ({size})"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        for wal_ext in ("-wal", "-shm"):
            wal = src.with_name(src.name + wal_ext)
            if wal.is_file():
                shutil.copy2(wal, dst.with_name(dst.name + wal_ext))
        return f"copied ({size})" if dst.is_file() else "FAILED - not on disk after copy"

    if src.is_dir():
        count, total = folder_size(src)
        if count == 0:
            return "empty in old folder - nothing to copy"
        if check_only:
            return f"would copy ({count} file(s), {human(total)})"
        copied = 0
        for child in src.rglob("*"):
            rel_child = child.relative_to(src)
            if any(part in NEVER_DIRS for part in rel_child.parts):
                continue
            if child.is_file():
                if child.suffix.lower() in NEVER:
                    continue
                target = dst / rel_child
                if target.exists() and not force:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
                copied += 1
        return f"copied {copied} of {count} file(s) ({human(total)})"

    return "not found"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_folder", nargs="?", help="Path to your old ZEUS / ZEUS / ZEUS / ZEUS folder")
    parser.add_argument("--check", action="store_true", help="Report what would be copied without changing anything")
    parser.add_argument("--force", action="store_true", help="Overwrite files that already exist in this folder")
    args = parser.parse_args(argv)

    say(BAR)
    say("  ZEUS - Restore Data & Models From an Earlier Folder")
    say(BAR)
    old_arg = args.old_folder
    if not old_arg:
        old_arg = input("\nPath to your old folder: ").strip().strip('"')
    if not old_arg:
        say("No folder given.")
        return 1

    old_root = Path(old_arg).expanduser().resolve()
    if not old_root.is_dir():
        say(f"\n[ERROR] Not a folder: {old_root}")
        return 1
    if old_root == ROOT:
        say("\n[ERROR] That is this same folder.")
        return 1

    say(f"\n  From: {old_root}")
    say(f"  To:   {ROOT}")
    if args.check:
        say("  Mode: --check (nothing will be written)\n")
    else:
        say()

    found_any = False
    for rel, description, aliases in ITEMS:
        src = find_source(old_root, rel, aliases)
        if src is None:
            continue
        found_any = True
        outcome = copy_one(src, ROOT / rel, force=args.force, check_only=args.check)
        say(f"  {rel:<36} {outcome}")
        say(f"      ({description})")

    if not found_any:
        say("  Nothing recognisable was found in that folder.")
        return 1

    say("\n" + BAR)
    say("  Done.")
    say(BAR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
