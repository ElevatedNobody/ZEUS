"""
Phase B - one-time setup of the local industrial document stack.

    python scripts/setup_industrial_env.py

This is the ONLY script in the industrial workbench that uses the network, and
only while you run it. It installs Python packages into the environment ZEUS
runs in and, optionally, pre-caches the PaddleOCR models so that the machine
can afterwards be disconnected permanently. Nothing else in ZEUS ever
downloads anything.

It installs only what is missing. Anything already working is left alone.

Options
    --check            report what is missing and exit without installing
    --minimal          install only the required stack (skip PaddleOCR/EasyOCR)
    --with-paddle      also install PaddleOCR + paddlepaddle (large download)
    --prefetch-paddle  run PaddleOCR once so its models are cached locally
    --download-wheels DIR
                       download every wheel into DIR for an air-gapped machine
    --offline-wheels DIR
                       install from a wheel directory with no network at all
    --yes              do not ask for confirmation

Tesseract note (Windows)
    The Tesseract OCR ENGINE is a native binary, not a pip package. Install it
    from the UB Mannheim build (tesseract-ocr-w64-setup-*.exe), tick "Add to
    PATH", and this script will detect it. pytesseract alone is not enough.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industrial import ROOT as PROJECT_ROOT, ensure_directories  # noqa: E402
from industrial.environment import audit, summarise  # noqa: E402

REQUIREMENTS = (PROJECT_ROOT / "config" / "requirements-industrial.txt"
                if (PROJECT_ROOT / "config" / "requirements-industrial.txt").is_file()
                else PROJECT_ROOT / "requirements-industrial.txt")

REQUIRED = [
    ("fitz", "PyMuPDF>=1.24.0", "PDF text, tables, page rendering, embedded images"),
    ("PIL", "Pillow>=10.3.0", "image handling"),
    ("numpy", "numpy>=1.26.0", "vector maths for retrieval"),
    ("cv2", "opencv-python-headless>=4.9.0", "OCR pre-processing"),
    ("pytesseract", "pytesseract>=0.3.10", "Tesseract python binding"),
    ("docx", "python-docx>=1.1.0", "DOCX read/write"),
    ("openpyxl", "openpyxl>=3.1.2", "XLSX read/write"),
    ("pptx", "python-pptx>=0.6.23", "PPTX read/write"),
    ("pandas", "pandas>=2.2.0", "CSV/tabular handling"),
    ("pypdf", "pypdf>=4.2.0", "PDF fallback reader"),
]

OPTIONAL = [
    ("paddleocr", "paddleocr>=2.7.0", "document OCR + layout/structure analysis"),
    ("paddle", "paddlepaddle>=2.6.0", "PaddleOCR backend (large)"),
    ("unstructured", "unstructured>=0.14.0", "local partitioning for HTML/EML/RTF/ODT"),
]


def has_module(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def pip(*args, check: bool = False) -> int:
    cmd = [sys.executable, "-m", "pip", *args]
    print("  >", " ".join(cmd))
    return subprocess.run(cmd, check=check).returncode


def missing(entries) -> list:
    return [(mod, spec, why) for mod, spec, why in entries if not has_module(mod)]


def tesseract_guidance() -> str:
    from industrial.ocr_engines import find_tesseract_binary
    exe = find_tesseract_binary()
    if exe:
        return f"  [OK]   Tesseract engine found: {exe}"
    if sys.platform == "win32":
        return ("  [TODO] Tesseract ENGINE not found. Install the UB Mannheim build:\n"
                "         https://github.com/UB-Mannheim/tesseract/wiki\n"
                "         Tick 'Add to PATH' during installation, then re-run this script.\n"
                "         (pytesseract is only the python binding - it needs the .exe.)")
    return ("  [TODO] Tesseract engine not found. On Debian/Ubuntu: "
            "sudo apt-get install tesseract-ocr")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the industrial document stack")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--minimal", action="store_true")
    parser.add_argument("--with-paddle", action="store_true")
    parser.add_argument("--prefetch-paddle", action="store_true")
    parser.add_argument("--download-wheels", default="")
    parser.add_argument("--offline-wheels", default="")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    ensure_directories()
    print("=" * 68)
    print("  ZEUS INDUSTRIAL ENVIRONMENT SETUP")
    print("  Autonomous AI Command & Intelligence System")
    print("=" * 68)
    print(f"  Python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"  Project     : {PROJECT_ROOT}")
    print()

    need_required = missing(REQUIRED)
    need_optional = missing(OPTIONAL)

    print("[SCAN] required stack")
    for mod, spec, why in REQUIRED:
        state = "OK     " if has_module(mod) else "MISSING"
        print(f"  [{state}] {spec:<34} {why}")
    print("[SCAN] optional stack")
    for mod, spec, why in OPTIONAL:
        state = "OK     " if has_module(mod) else "MISSING"
        print(f"  [{state}] {spec:<34} {why}")
    print("[SCAN] OCR engine binary")
    print(tesseract_guidance())
    print()

    # ------------------------------------------------------- wheel builder
    if args.download_wheels:
        target = Path(args.download_wheels).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        print(f"[WHEELS] downloading every wheel into {target} for offline install")
        specs = [s for _, s, _ in REQUIRED]
        if args.with_paddle:
            specs += [s for _, s, _ in OPTIONAL]
        pip("download", "-d", str(target), *specs)
        print(f"[WHEELS] done. On the air-gapped machine run:\n"
              f"         python scripts/setup_industrial_env.py --offline-wheels {target}")
        return 0

    if args.check:
        report = audit(write=True)
        print(summarise(report))
        return 0 if report["environment_status"] != "NOT_READY" else 1

    to_install = [spec for _, spec, _ in need_required]
    if not args.minimal and args.with_paddle:
        to_install += [spec for _, spec, _ in need_optional if "paddle" in spec]

    if not to_install:
        print("[DEPENDENCIES] nothing to install - the required stack is already present")
    else:
        print("[DEPENDENCIES] will install:")
        for spec in to_install:
            print(f"    {spec}")
        if not args.yes:
            answer = input("Proceed? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted - nothing was installed.")
                return 1
        if args.offline_wheels:
            wheels = Path(args.offline_wheels).expanduser()
            print(f"[DEPENDENCIES] installing from local wheels at {wheels} (no network)")
            pip("install", "--no-index", "--find-links", str(wheels), *to_install)
        else:
            pip("install", "--upgrade", "pip")
            pip("install", *to_install)

    # ------------------------------------------------ paddle model prefetch
    if args.prefetch_paddle:
        if not (has_module("paddleocr") and has_module("paddle")):
            print("[PADDLE] paddleocr/paddlepaddle are not installed - skipping prefetch")
        else:
            print("[PADDLE] running PaddleOCR once so its models are cached locally.")
            print("         This is the LAST step that needs the network. After it "
                  "completes, the machine can be disconnected permanently.")
            try:
                from PIL import Image, ImageDraw
                from industrial import ocr_engines
                img = Image.new("RGB", (400, 120), "white")
                ImageDraw.Draw(img).text((20, 40), "INSPECTION P-101A", fill="black")
                result = ocr_engines.run_ocr(img, order=["paddleocr"])
                print(f"[PADDLE] models cached; test read: '{result.text.strip()[:60]}'")
            except Exception as e:
                print(f"[PADDLE] prefetch failed: {e}")

    # ----------------------------------------------------------- re-audit
    print()
    print("[VERIFY] re-auditing the environment")
    report = audit(write=True)
    print(summarise(report))
    print()
    print("Next steps:")
    print("  1. Start Ollama:            ollama serve")
    print("  2. Ingest knowledge:        python scripts/ingest_industrial_knowledge.py")
    print("  3. Verify offline:          python scripts/offline_test.py")
    print("  4. Start ZEUS:            start_medha_industrial.bat")
    return 0 if report["environment_status"] != "NOT_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
