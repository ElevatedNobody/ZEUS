"""
Build a clean, shareable ZEUS release zip from this folder.

    python scripts/make_release.py [--out PATH]

What goes in: the application code, the operator BAT files, configuration,
vendored third-party source (InsightFace, Hermes, wf), the WFP helper, the
knowledge pack, docs, tests, and the latest diagnostic report.

What is left out, deliberately:
  * .venv/                   machine-specific; STEP 1 : INSTALL builds it
  * models/ weights          gigabytes; STEP 1 : INSTALL downloads them, or
                             RESTORE_MY_DATA.py copies them from this folder
  * database/ contents       YOUR private data - accounts, vault, memory, the
                             audit key; RESTORE_MY_DATA.py carries it across
  * logs/, backups/          this machine's history (audit history travels
                             with RESTORE_MY_DATA.py, not in a zip)
  * config/trusted_models.json, config/firewall_manifest.json,
    scripts/firewall/*.ps1   signed or generated for THIS folder and key
  * caches (__pycache__, .pytest_cache)

A MANIFEST.sha256 inside the zip lists the SHA-256 of every file in it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", "backups", "node_modules",
                 ".agni_test_tmp"}
# Folders whose CONTENTS stay behind (their README.txt is kept so the folder
# and its explanation exist in the release).
CONTENTS_EXCLUDED = {"models", "database", "logs"}
EXCLUDED_FILES = {"config/trusted_models.json", "config/firewall_manifest.json",
                  # generated on each machine, and full of that machine's paths
                  "AGNI_DIAGNOSTIC_REPORT.json", "AGNI_DIAGNOSTIC_REPORT.txt",
                  "config/industrial_environment.json"}
EXCLUDED_PREFIXES = ("scripts/firewall/",)
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".part", ".zip"}


def included(relative: Path) -> bool:
    parts = relative.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return False
    key = relative.as_posix()
    if key in EXCLUDED_FILES or key.startswith(EXCLUDED_PREFIXES):
        return False
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if parts[0] in CONTENTS_EXCLUDED:
        return len(parts) == 2 and parts[1].lower() == "readme.txt"
    return True


def build(out: Path) -> dict:
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p.relative_to(ROOT)))
    manifest, total = [], 0
    prefix = "ZEUS"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            total += len(data)
            manifest.append(f"{hashlib.sha256(data).hexdigest()}  {relative}")
            bundle.writestr(zipfile.ZipInfo.from_file(path, f"{prefix}/{relative}"), data,
                            compress_type=zipfile.ZIP_DEFLATED)
        header = (f"# ZEUS release built {time.strftime('%Y-%m-%d %H:%M:%S')} - {len(files)} files\n"
                  f"# verify:  certutil -hashfile <file> SHA256\n")
        bundle.writestr(f"{prefix}/MANIFEST.sha256", header + "\n".join(manifest) + "\n")
    return {"files": len(files), "bytes": total, "zip_bytes": out.stat().st_size, "path": str(out)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT.parent / f"AGNI_release_{time.strftime('%Y-%m-%d')}.zip"))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if out.is_relative_to(ROOT):
        print("the zip must be written outside the folder it packs")
        return 1
    result = build(out)
    print(f"{result['files']} files, {result['bytes'] / 1e6:.1f} MB -> {result['zip_bytes'] / 1e6:.1f} MB")
    print(result["path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
