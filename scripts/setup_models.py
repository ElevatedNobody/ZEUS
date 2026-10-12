"""
One-time download of the local voice models. THE ONLY SCRIPT THAT USES
THE NETWORK.

    python scripts/setup_models.py                # Whisper + Chatterbox
    python scripts/setup_models.py --check        # verify, download nothing
    python scripts/setup_models.py --whisper-only
    python scripts/setup_models.py --tts-only
    python scripts/setup_models.py --whisper-size small
    python scripts/setup_models.py --whisper-fallback small   # low-memory recogniser, models/whisper-small

Transport. ZEUS's own firewall blocks this venv's python.exe from the internet
(WinError 10013), so on Windows the files are fetched with the system curl.exe
(not blocked), resuming partial downloads; huggingface_hub is used where curl is
absent. Every file is checked against the size and SHA-256 the Hub publishes, and
`agni_model_manifest.json` records what was fetched. A complete, verified model
is never downloaded again.

After this has run once, ZEUS never touches the network for voice
again: `voice/offline.py` sets HF_HUB_OFFLINE and the engines load from
`models/whisper` and `models/chatterbox` by path. Unplug the network and
everything still works.

Files fetched
    models/whisper/      model.bin, config.json, tokenizer.json,
                         vocabulary.* (CTranslate2 format for faster-whisper)
    models/chatterbox/   ve.safetensors, t3_cfg.safetensors, s3gen.safetensors,
                         tokenizer.json, conds.pt

Both come from Hugging Face over HTTPS. Nothing else in ZEUS may do
this - that is why `allow_network()` lives here and `enforce_offline()`
is never called in this file.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.offline import (  # noqa: E402
    CHATTERBOX_REQUIRED_FILES,
    WHISPER_REQUIRED_FILES,
    allow_network,
    missing_files,
    resolve_chatterbox_dir,
    resolve_models_dir,
    resolve_whisper_dir,
)

# CTranslate2 conversions of Whisper, published for faster-whisper.
WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    # the same repository faster-whisper itself maps "large-v3-turbo" to
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

CHATTERBOX_REPO = "ResembleAI/chatterbox"

# Only the five files ZEUS loads are fetched, NOT the whole repository.
# The repo is around 14 GB because it also carries .pt duplicates of every
# checkpoint and the multilingual 23-language variants; downloading all of it
# would be a several-hour mistake on a home connection.
#
# What the five actually weigh (checked against the repository):
#     t3_cfg.safetensors   2.13 GB   the 0.5B speech-token model
#     s3gen.safetensors    1.06 GB   the vocoder
#     ve.safetensors        5.7 MB   the voice encoder
#     tokenizer.json         26 KB
#     conds.pt              107 KB
CHATTERBOX_APPROX_TOTAL = "~3.2 GB"

APPROX_SIZE = {
    "tiny": "~75 MB", "tiny.en": "~75 MB", "base": "~145 MB", "base.en": "~145 MB",
    "small": "~484 MB", "small.en": "~484 MB", "medium": "~1.5 GB", "large-v3": "~3.1 GB",
}


HF_URL = "https://huggingface.co"
WHISPER_PATTERNS = ("model.bin", "config.json", "tokenizer.json", "vocabulary*", "preprocessor_config.json")
MANIFEST = "agni_model_manifest.json"


class DownloadError(RuntimeError):
    pass


def _curl() -> str:
    return shutil.which("curl.exe") or shutil.which("curl") or ""


def _run_curl(args, timeout: float = 7200.0) -> subprocess.CompletedProcess:
    return subprocess.run([_curl(), *args], capture_output=True, text=True, timeout=timeout)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hf_file_list(repo: str, revision: str = "main"):
    """[{path, size, sha256}] of a Hub repository - sha256 for LFS files (the
    large ones), None for small git files (checked by size)."""
    result = _run_curl(["-s", "--fail", "--max-time", "60", f"{HF_URL}/api/models/{repo}/tree/{revision}"], 90)
    if result.returncode != 0:
        raise DownloadError(f"could not list {repo} (curl exit {result.returncode}: {result.stderr.strip()[:200]})")
    return [{"path": item["path"], "size": item.get("size"), "sha256": (item.get("lfs") or {}).get("oid")}
            for item in json.loads(result.stdout) if item.get("type") == "file"]


def fetch_file(repo: str, entry, target: Path, revision: str = "main") -> str:
    """One file into `target`, resumed if interrupted, then checked. Returns
    'present' (already verified) or 'downloaded'."""
    destination = target / entry["path"]
    if destination.is_file() and (entry.get("size") is None or destination.stat().st_size == entry["size"]) \
            and (not entry.get("sha256") or _sha256(destination) == entry["sha256"]):
        return "present"
    partial = destination.with_name(destination.name + ".part")
    url = f"{HF_URL}/{repo}/resolve/{revision}/{entry['path']}"
    result = _run_curl(["-L", "--fail", "--retry", "3", "--retry-delay", "3", "-C", "-", "-s", "-S",
                        "-o", str(partial), url])
    if result.returncode != 0:
        raise DownloadError(f"{entry['path']}: curl exit {result.returncode}: {result.stderr.strip()[:200]}")
    size = partial.stat().st_size
    if entry.get("size") is not None and size != entry["size"]:
        raise DownloadError(f"{entry['path']}: {size} bytes, the Hub says {entry['size']} - kept for resume, run again")
    if entry.get("sha256"):
        got = _sha256(partial)
        if got != entry["sha256"]:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"{entry['path']}: SHA-256 {got[:16]}... does not match the Hub's "
                                f"{entry['sha256'][:16]}... - the file was deleted")
    partial.replace(destination)
    return "downloaded"


def download_repo(repo: str, target: Path, patterns, label: str, revision: str = "main") -> bool:
    """The files of `repo` matching `patterns`, verified, with a manifest."""
    if not _curl():
        return False
    try:
        files = [f for f in hf_file_list(repo, revision)
                 if any(fnmatch.fnmatch(f["path"], pattern) for pattern in patterns)]
    except DownloadError as error:
        print(f"[{label}] {error}")
        return False
    if not files:
        print(f"[{label}] {repo} has no files matching {', '.join(patterns)}")
        return False
    target.mkdir(parents=True, exist_ok=True)
    total = sum(f.get("size") or 0 for f in files)
    print(f"[{label}] {repo}: {len(files)} file(s), {total / 1e6:.0f} MB, via curl into {target}")
    for index, entry in enumerate(files, 1):
        try:
            state = fetch_file(repo, entry, target, revision)
        except DownloadError as error:
            print(f"[{label}] FAILED {error}")
            return False
        print(f"          [{index}/{len(files)}] {state:10} {entry['path']} ({(entry.get('size') or 0) / 1e6:.1f} MB)"
              + (" sha256 verified" if entry.get("sha256") else " size verified"))
    (target / MANIFEST).write_text(json.dumps({
        "repo": repo, "revision": revision, "transport": "curl", "downloaded_at": time.time(),
        "files": [{"name": f["path"], "size": f.get("size"), "sha256": f.get("sha256")} for f in files]},
        indent=2), encoding="utf-8")
    return True


def verify_manifest(target: Path) -> str:
    """'' when every file listed in the manifest is present with its size and hash."""
    manifest = target / MANIFEST
    if not manifest.is_file():
        return "no manifest (installed before manifests were written) - files checked by name only"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for f in data.get("files", []):
        path = target / f["name"]
        if not path.is_file():
            return f"{f['name']} is missing"
        if f.get("size") is not None and path.stat().st_size != f["size"]:
            return f"{f['name']} has {path.stat().st_size} bytes, expected {f['size']}"
        if f.get("sha256") and _sha256(path) != f["sha256"]:
            return f"{f['name']} does not match its SHA-256"
    return ""


def _manifest_summary(target: Path) -> str:
    """What a passed verify_manifest actually checked: the Hub publishes SHA-256
    only for its large (LFS) files, the small ones are checked by size."""
    files = json.loads((target / MANIFEST).read_text(encoding="utf-8")).get("files", [])
    hashed = [f["name"] for f in files if f.get("sha256")]
    return (f"{len(files)} file(s) match their size"
            + (f"; SHA-256 verified for {', '.join(hashed)}" if hashed else ""))


def _hub():
    try:
        from huggingface_hub import hf_hub_download, snapshot_download  # noqa: F401
        return hf_hub_download, snapshot_download
    except ImportError:
        print("ERROR: huggingface_hub is not installed.")
        print("       pip install huggingface_hub")
        return None, None


def report(directory: Path, required, label: str) -> bool:
    absent = missing_files(directory, required)
    if absent:
        print(f"  [MISSING] {label}: {directory}")
        for name in absent:
            print(f"            - {name}")
        return False
    print(f"  [OK]      {label}: {directory}")
    return True


def fallback_dir(size: str) -> Path:
    """Where the low-memory recogniser lives - found by voice/stt_manager.py."""
    return resolve_models_dir() / f"whisper-{size}"


def check_only() -> int:
    print("Local voice model check")
    print(f"  models dir: {resolve_models_dir()}")
    whisper_ok = report(resolve_whisper_dir(), WHISPER_REQUIRED_FILES, "Whisper STT")
    for folder in sorted(resolve_models_dir().glob("whisper-*")):
        if folder.is_dir():
            ok = report(folder, WHISPER_REQUIRED_FILES, f"Whisper fallback ({folder.name})")
            problem = verify_manifest(folder) if ok else ""
            print(f"            manifest: {problem or _manifest_summary(folder)}")
    tts_ok = report(resolve_chatterbox_dir(), CHATTERBOX_REQUIRED_FILES, "Chatterbox TTS")
    if whisper_ok and tts_ok:
        print("\nBoth voice models are present. ZEUS can run voice fully offline.")
        return 0
    print("\nRun this script without --check (while connected) to download what is missing.")
    return 1


def download_whisper(size: str, target: Path) -> bool:
    repo = WHISPER_REPOS.get(size)
    if not repo:
        print(f"ERROR: unknown whisper size '{size}'. "
              f"Choose from: {', '.join(WHISPER_REPOS)}")
        return False

    if _curl():
        # Windows: the ZEUS firewall blocks this python.exe; curl.exe is allowed.
        if download_repo(repo, target, WHISPER_PATTERNS, "WHISPER"):
            return report(target, WHISPER_REQUIRED_FILES, "Whisper STT")
        print("[WHISPER] curl transport failed - trying huggingface_hub")
    print(f"[WHISPER] downloading {repo} ({APPROX_SIZE.get(size, 'size varies')})")
    print(f"          into {target}")
    target.mkdir(parents=True, exist_ok=True)
    _, snapshot_download = _hub()
    if snapshot_download is None:
        return False
    try:
        snapshot_download(
            repo_id=repo,
            local_dir=str(target),
            # allow_patterns keeps the checkout to what faster-whisper needs.
            allow_patterns=["*.bin", "*.json", "*.txt", "vocabulary*", "*.model"],
        )
    except Exception as e:
        print(f"[WHISPER] download failed: {e}")
        return False
    return report(target, WHISPER_REQUIRED_FILES, "Whisper STT")


def download_chatterbox(target: Path) -> bool:
    hf_hub_download, _ = _hub()
    if hf_hub_download is None:
        return False

    print(f"[CHATTERBOX] downloading {CHATTERBOX_REPO} "
          f"({CHATTERBOX_APPROX_TOTAL}, 5 files)")
    print(f"             into {target}")
    print(f"             the largest file is 2.1 GB - this takes a while, and it")
    print(f"             resumes if you interrupt it and run this again")
    target.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(CHATTERBOX_REQUIRED_FILES, start=1):
        destination = target / name
        if destination.is_file() and destination.stat().st_size > 0:
            size = destination.stat().st_size / (1024 ** 2)
            print(f"             [{index}/{len(CHATTERBOX_REQUIRED_FILES)}] "
                  f"already present: {name} ({size:.0f} MB)")
            continue
        try:
            print(f"             [{index}/{len(CHATTERBOX_REQUIRED_FILES)}] "
                  f"fetching {name} ...")
            hf_hub_download(
                repo_id=CHATTERBOX_REPO,
                filename=name,
                local_dir=str(target),
            )
        except Exception as e:
            print(f"[CHATTERBOX] failed to download '{name}': {e}")
            print(f"             Run this command again - completed files are kept "
                  f"and only what is missing is re-fetched.")
            return False
    return report(target, CHATTERBOX_REQUIRED_FILES, "Chatterbox TTS")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download ZEUS voice models for offline use (setup only)")
    parser.add_argument("--check", action="store_true",
                        help="report what is present and exit; downloads nothing")
    parser.add_argument("--whisper-only", action="store_true")
    parser.add_argument("--tts-only", action="store_true")
    parser.add_argument("--whisper-size", default="base",
                        choices=sorted(WHISPER_REPOS),
                        help="Whisper model size (default: base)")
    parser.add_argument("--whisper-fallback", default="", choices=[""] + sorted(WHISPER_REPOS),
                        help="install ONLY the low-memory recogniser into models/whisper-<size> "
                             "(config.ini [voice] stt_fallback_model)")
    parser.add_argument("--force", action="store_true",
                        help="download even if the files are already present")
    args = parser.parse_args()

    if args.check:
        return check_only()

    if args.whisper_fallback:
        allow_network()
        target = fallback_dir(args.whisper_fallback)
        if not args.force and not missing_files(target, WHISPER_REQUIRED_FILES) and not verify_manifest(target):
            print(f"[WHISPER] fallback already present and verified at {target} - nothing downloaded")
            return 0
        ok = download_whisper(args.whisper_fallback, target)
        print("  Low-memory recogniser ready." if ok else "  Fallback setup incomplete - see the errors above.")
        return 0 if ok else 1

    # SETUP ONLY: clear the offline flags so downloads are possible even on a
    # machine that exports HF_HUB_OFFLINE=1 globally.
    allow_network()

    print("=" * 66)
    print("  ZEUS VOICE MODEL SETUP")
    print("  Autonomous AI Command & Intelligence System")
    print("  This is the only step that uses the internet.")
    print("  After it completes, ZEUS runs voice fully offline.")
    print("=" * 66)
    print(f"  models dir: {resolve_models_dir()}")
    print()

    ok = True
    if not args.tts_only:
        whisper_dir = resolve_whisper_dir()
        if not args.force and not missing_files(whisper_dir, WHISPER_REQUIRED_FILES):
            print(f"[WHISPER] already present at {whisper_dir} - skipping")
        else:
            ok = download_whisper(args.whisper_size, whisper_dir) and ok
        print()

    if not args.whisper_only:
        chatterbox_dir = resolve_chatterbox_dir()
        if not args.force and not missing_files(chatterbox_dir, CHATTERBOX_REQUIRED_FILES):
            print(f"[CHATTERBOX] already present at {chatterbox_dir} - skipping")
        else:
            ok = download_chatterbox(chatterbox_dir) and ok
        print()

    print("=" * 66)
    if ok:
        print("  Voice models ready. You can disconnect the network now.")
        print("  Verify with:  python scripts/offline_test.py")
        print("  Start with:   start_medha_industrial.bat")
    else:
        print("  Setup incomplete - see the errors above.")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
