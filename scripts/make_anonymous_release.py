"""
A copy of the release that says nothing about who built it or on what.

    python scripts/make_anonymous_release.py [--name BOT_1] [--out <zip>]

The same files as scripts/make_release.py, and then:

  * the zip and its top folder are called --name (default BOT_1);
  * people's names (the local accounts, the Windows user), this PC's name,
    its folder paths and its hardware model are replaced by neutral words -
    the curated table below for the known phrases, a generic replacement for
    anything else discovered on the machine;
  * the author fields inside the sample Office files are blanked;
  * the finished zip is scanned - every text, every file name, the inside of
    every Office file - for all of those identifiers, and it is DELETED if a
    single one remains. A zip that is not clean is never left behind.

Nothing in the working folder changes; only the copy inside the zip. Vendored
third-party code is copied byte for byte (third_party/hermes is hash-checked
at load), and is scanned like everything else.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import os
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from make_release import included  # noqa: E402

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".ini", ".bat", ".cmd", ".ps1", ".js",
                 ".css", ".html", ".csv", ".toml", ".yml", ".yaml", ".cfg", ".xml", ".sha256"}
OFFICE_SUFFIXES = {".docx", ".xlsx", ".pptx"}
UNTOUCHED_PREFIX = "third_party/"
# This tool lists the very identifiers it removes, so it stays on the
# owner's machine and never goes into the anonymous copy.
NOT_IN_ANONYMOUS = {"scripts/make_anonymous_release.py"}

# Account names that are roles, not people - never treated as identifiers.
GENERIC_ACCOUNTS = {"admin", "administrator", "operator", "viewer", "engineer", "root", "user",
                    "guest", "test", "demo", "e2e.operator"}


# ------------------------------------------------------------ the phrases --

def phrase_table(name: str) -> List[Tuple[str, str]]:
    """Known identifying phrases in this code base -> neutral wording.
    Ordered: the specific phrasing first, the bare identifier last."""
    return [
        # people (test fixtures and the owner's accounts)
        (r"dhruv@1007", "owner@local"),
        (r"dhruv\.sharma", "operator.one"),
        (r"Dhruv1", "Operator1"),
        # "opsuser", not "operator": a test creates "operator" as a SECOND
        # account next to this one, and the two must stay different names
        (r"\bDHRUV\b", "OPSUSER"),
        (r"\bDhruv\b", "Opsuser"),
        (r"\bdhruv\b", "opsuser"),
        # a surname that may be the owner's (the demo staff "Priya Sharma" /
        # "R. Sharma" become "Priya Sen" / "R. Sen" - consistently, so tests
        # that look them up still agree with the demo data; same initial, so
        # the avatar initials "PS" still hold)
        (r"SHARMA", "SEN"),
        (r"Sharma", "Sen"),
        (r"sharma", "sen"),
        # this laptop, as described in the docs
        (r"ROG Strix, Core Ultra 9 275HX, 15\.4 GB RAM,", "a 16 GB RAM laptop,"),
        (r"RTX 5060 Laptop 8 GB", "8 GB RTX 50-series laptop GPU"),
        (r"\(ROG Strix, Core 9, RTX 5060 8 GB, 16 GB\)",
         "(a 16 GB laptop with an 8 GB RTX 50-series GPU)"),
        (r"ASUS ROG Strix G16 · Intel Core \(Ultra\) 9 ·", "a 16 GB Windows laptop ·"),
        (r"NVIDIA RTX 5060 Laptop GPU \(8 GB\)", "8 GB NVIDIA RTX 50-series laptop GPU"),
        (r"my laptop is an ROG Strix", "my laptop has a grey lid"),
        (r"the ROG Strix this was repaired on", "the PC this was repaired on"),
        (r"\bon the ROG\b", "on the target PC"),
        (r"\bthe ROG\b", "the target PC"),
        (r"RTX 5060 / 4060 / 3070 laptop class", "8 GB laptop GPU class (RTX 50/40/30-series)"),
        (r"on the RTX 5060 / 15\.4 GB laptop", "on a 16 GB laptop with an RTX 50-series GPU"),
        (r"GeForce RTX 5060 Laptop GPU", "GeForce RTX 5070 Laptop GPU"),   # a test input
        (r"\bRTX_5060\b", "RTX_50_SERIES"),
        (r"an RTX 5060 Laptop GPU", "an RTX 50-series laptop GPU"),
        (r"the RTX 5060 GPU", "an RTX 50-series GPU"),
        (r"the RTX 5060 laptop", "an RTX 50-series laptop"),
        (r"RTX 5060 laptop", "RTX 50-series laptop"),
        (r"on the RTX 5060\b", "on an RTX 50-series GPU"),
        (r"for the RTX 5060\b", "for an RTX 50-series GPU"),
        (r"the RTX 5060\b", "an RTX 50-series GPU"),
        (r"RTX 5060 \(8 GB VRAM\)", "RTX 50-series GPU (8 GB VRAM)"),
        (r"\bRTX 5060\b", "RTX 50-series"),
        (r"Intel Core Ultra 9", "Intel Core 9-class CPU"),
        (r"15\.4 GB laptop", "16 GB laptop"),
        (r"15\.4 GB", "16 GB"),
        # this laptop's folders
        (r"Desktop\\ZEUS\\AGNI_ROG_Strix_2026-09-24_v3\\ZEUS",
         "<the folder you unzipped into>\\\\" + name),
        (r"AGNI_ROG_Strix_2026-09-24_v3", name),
        # anything left
        (r"\bROG\b", "PC"),
        (r"\bStrix\b", "laptop"),
        (r"\b275HX\b", "laptop CPU"),
        (r"Core Ultra", "Core"),
    ]


# ------------------------------------------------ what identifies this PC --

def _registry(path: str, value: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return str(winreg.QueryValueEx(key, value)[0])
    except Exception:
        return ""


def _gpu_names() -> List[str]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _account_names() -> List[str]:
    database = ROOT / "database" / "medha.db"
    if not database.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            return [row[0] for row in conn.execute("SELECT username FROM users") if row[0]]
    except Exception:
        return []


def machine_identifiers() -> Dict[str, List[str]]:
    """Every string on THIS machine that would point back to it or its owner."""
    people = set()
    for account in _account_names():
        if account.lower() in GENERIC_ACCOUNTS:
            continue
        people.add(account)
        stem = re.match(r"[A-Za-z]{3,}", account)       # "dhruv" from "dhruv@1007"
        if stem and stem.group(0).lower() not in GENERIC_ACCOUNTS:
            people.add(stem.group(0))
    for candidate in (os.environ.get("USERNAME", ""), getpass.getuser()):
        if candidate and candidate.lower() not in GENERIC_ACCOUNTS:
            people.add(candidate)
    machine = set()
    computer = os.environ.get("COMPUTERNAME", "")
    if computer:
        machine.add(computer)
    bios = r"HARDWARE\DESCRIPTION\System\BIOS"
    for value in ("SystemFamily", "SystemProductName", "BaseBoardProduct"):
        text = _registry(bios, value)
        # 4+ characters: a 3-character model part like "G16" is also a
        # spreadsheet cell address (found in the sample workbooks). ROG and
        # Strix are in the fixed list anyway.
        for token in re.findall(r"[A-Za-z0-9]{4,}", text):
            if re.search(r"\d", token) or token.isupper() or token.istitle():
                if token.lower() not in {"asus", "laptop", "system", "product", "name", "default"}:
                    machine.add(token)
    cpu = _registry(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
    machine.update(re.findall(r"\b\d{3,5}[A-Z]{1,3}\b", cpu))          # e.g. 275HX
    for gpu in _gpu_names():
        machine.update(re.findall(r"RTX\s*\d{4}", gpu))
    paths = {str(Path.home()), str(ROOT), ROOT.parent.name}
    return {"people": sorted(people, key=len, reverse=True),
            "machine": sorted(machine, key=len, reverse=True),
            "paths": sorted(p for p in paths if p), }


def fixed_identifiers() -> List[str]:
    """Checked in every build, whatever the machine it runs on."""
    return [r"nirbhay", r"dhruv", r"sharma", r"dhruvdnn", r"\brog\b", r"strix", r"275hx",
            r"core ultra", r"rtx\s*5060", r"15\.4 ?gb", r"desktop[\\/]+jarvis",
            r"agni_rog_strix"]


# ----------------------------------------------------------- the rewrite --

def scrub_text(text: str, table: List[Tuple[str, str]], ids: Dict[str, List[str]]) -> str:
    for pattern, replacement in table:
        text = re.sub(pattern, replacement, text)
    for path in ids["paths"]:
        text = re.sub(re.escape(path), "<path>", text, flags=re.IGNORECASE)
        text = re.sub(re.escape(path.replace("\\", "\\\\")), "<path>", text, flags=re.IGNORECASE)
        text = re.sub(re.escape(path.replace("\\", "/")), "<path>", text, flags=re.IGNORECASE)
    for person in ids["people"]:
        text = re.sub(rf"(?<![A-Za-z]){re.escape(person)}(?![A-Za-z])", "operator", text,
                      flags=re.IGNORECASE)
    for token in ids["machine"]:
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", "PC", text)
    return text


def scrub_office(data: bytes) -> bytes:
    """The same document with its author fields emptied."""
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename in ("docProps/core.xml", "docProps/app.xml"):
                text = content.decode("utf-8")
                for tag in ("dc:creator", "cp:lastModifiedBy", "Company", "Manager"):
                    text = re.sub(rf"(<{tag}[^>]*>)[^<]*(</{tag}>)", r"\1\2", text)
                content = text.encode("utf-8")
            target.writestr(info, content, compress_type=info.compress_type)
    return out.getvalue()


# ------------------------------------------------------------ the check --

def leftovers(zip_path: Path, ids: Dict[str, List[str]]) -> List[str]:
    """Every identifier still present anywhere in the zip. Empty = clean."""
    checks = [re.compile(p, re.IGNORECASE) for p in fixed_identifiers()]
    checks += [re.compile(rf"(?<![A-Za-z]){re.escape(p)}(?![A-Za-z])", re.IGNORECASE)
               for p in ids["people"]]
    checks += [re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])") for t in ids["machine"]]
    checks += [re.compile(re.escape(p), re.IGNORECASE) for p in ids["paths"]]
    found = []

    def look(where: str, text: str):
        for check in checks:
            hit = check.search(text)
            if hit:
                found.append(f"{where}: '{hit.group(0)}'")

    with zipfile.ZipFile(zip_path) as bundle:
        for entry in bundle.namelist():
            look(f"file name {entry}", entry)
            data = bundle.read(entry)
            if Path(entry).suffix.lower() in OFFICE_SUFFIXES:
                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                    for part in inner.namelist():
                        look(f"{entry} :: {part}", inner.read(part).decode("utf-8", "ignore"))
            else:
                look(entry, data.decode("utf-8", "ignore"))
                look(entry + " (bytes)", data.decode("latin-1"))
    return found


# ------------------------------------------------------------ the build --

def build(out: Path, name: str) -> dict:
    ids = machine_identifiers()
    table = phrase_table(name)
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p.relative_to(ROOT))
                   and p.relative_to(ROOT).as_posix() not in NOT_IN_ANONYMOUS)
    manifest, changed = [], []
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            if not relative.startswith(UNTOUCHED_PREFIX):
                suffix = path.suffix.lower()
                if suffix in TEXT_SUFFIXES:
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text = None
                    if text is not None:
                        clean = scrub_text(text, table, ids)
                        if clean != text:
                            changed.append(relative)
                            data = clean.encode("utf-8")
                elif suffix in OFFICE_SUFFIXES:
                    data = scrub_office(data)
            manifest.append(f"{hashlib.sha256(data).hexdigest()}  {relative}")
            info = zipfile.ZipInfo(f"{name}/{relative}", date_time=time.localtime(path.stat().st_mtime)[:6])
            bundle.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
        header = (f"# {name} release built {time.strftime('%Y-%m-%d')} - {len(files)} files\n"
                  f"# verify:  certutil -hashfile <file> SHA256\n")
        bundle.writestr(f"{name}/MANIFEST.sha256", header + "\n".join(manifest) + "\n")
    problems = leftovers(out, ids)
    if problems:
        out.unlink()
        raise SystemExit("NOT CLEAN - the zip was deleted. Still present:\n  " + "\n  ".join(problems[:40]))
    return {"files": len(files), "rewritten": changed, "zip_bytes": out.stat().st_size,
            "path": str(out), "checked_people": len(ids["people"]),
            "checked_machine_tokens": len(ids["machine"]), "checked_paths": len(ids["paths"])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="BOT_1")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", args.name) or "BOT_1"
    out = Path(args.out) if args.out else ROOT.parent / f"{name}.zip"
    result = build(out, name)
    print(f"{result['files']} files, {len(result['rewritten'])} rewritten, "
          f"{result['zip_bytes'] / 2**20:.1f} MB -> {result['path']}")
    print(f"clean: no name, account, user, computer, path or hardware identifier found "
          f"({result['checked_people']} people/accounts, {result['checked_machine_tokens']} "
          f"hardware tokens, {result['checked_paths']} paths checked, plus the fixed list)")
    for relative in result["rewritten"]:
        print(f"  rewritten: {relative}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
