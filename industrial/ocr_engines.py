"""
Phase B - layered local OCR.

Engine order is deliberate and never collapses to a single engine:

  1. PaddleOCR   - strongest on real documents; also provides layout/structure
                   (regions, tables, headings) via PP-Structure when installed.
  2. Tesseract   - reliable, lightweight, fully offline fallback. Kept as a
                   first-class engine, NOT replaced by PaddleOCR.
  3. EasyOCR     - the engine ZEUS already used for screen reading. Preserved
                   so existing behaviour is untouched; also acts as a last
                   resort here.

Every result carries the engine that produced it and a confidence value, so
downstream evidence can say how a number was read - a scanned thickness
reading with 0.55 confidence must never be presented like a native PDF value.

All engines run locally. Nothing here downloads a model at call time: if an
engine's models are not cached, the engine reports itself unusable and the
next one is tried.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("industrial.ocr")

# Engine instances are expensive to build (model load) - cache per process.
_ENGINE_CACHE: Dict[str, Any] = {}
_ENGINE_FAILED: Dict[str, str] = {}


@dataclass
class OCRLine:
    text: str
    confidence: float = 0.0
    bbox: Optional[List[float]] = None      # [x0, y0, x1, y1]


@dataclass
class OCRResult:
    text: str = ""
    lines: List[OCRLine] = field(default_factory=list)
    engine: str = "none"
    confidence: float = 0.0                  # mean line confidence, 0..1
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.text.strip()) and self.error is None

    def to_dict(self) -> dict:
        return {"engine": self.engine, "confidence": round(self.confidence, 4),
                "characters": len(self.text), "lines": len(self.lines), "error": self.error}


# --------------------------------------------------------------- availability

def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def find_tesseract_binary() -> Optional[str]:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def engine_available(name: str) -> bool:
    if name in _ENGINE_FAILED:
        return False
    if name == "tesseract":
        return _has("pytesseract") and find_tesseract_binary() is not None
    if name == "paddleocr":
        return _has("paddleocr") and _has("paddle")
    if name == "easyocr":
        return _has("easyocr")
    if name == "rapidocr":
        return _rapidocr_module() is not None and _has("onnxruntime")
    return False


def _rapidocr_module() -> Optional[str]:
    """Which RapidOCR distribution is installed. `rapidocr_onnxruntime` (1.x)
    supports Python <= 3.12; `rapidocr` (3.x) replaces it on 3.13 and later.
    Both ship their ONNX models inside the wheel and run offline."""
    if _has("rapidocr_onnxruntime"):
        return "rapidocr_onnxruntime"
    if _has("rapidocr"):
        return "rapidocr"
    return None


def available_engines() -> List[str]:
    return [n for n in DEFAULT_ORDER if engine_available(n)]


_IMPORT_OK: Dict[str, bool] = {}


def usable_engines() -> List[str]:
    """Engines that are installed AND import cleanly on this machine.

    `available_engines()` only checks that a package is present. A package can
    be present and still unusable - on the audit machine EasyOCR was installed
    but its torchvision dependency failed to import - so readiness reporting
    uses this, which actually imports each engine once."""
    modules = {"paddleocr": "paddleocr", "rapidocr": _rapidocr_module() or "rapidocr_onnxruntime",
               "tesseract": "pytesseract", "easyocr": "easyocr"}
    usable = []
    for name in available_engines():
        if name not in _IMPORT_OK:
            try:
                importlib.import_module(modules[name])
                _IMPORT_OK[name] = True
            except Exception as error:
                _IMPORT_OK[name] = False
                _ENGINE_FAILED[name] = f"import failed: {error}"
        if _IMPORT_OK[name]:
            usable.append(name)
    return usable


# ------------------------------------------------------------- preprocessing

def preprocess_for_ocr(image, aggressive: bool = False):
    """Light, deterministic preprocessing that helps every engine on scanned
    industrial paperwork: greyscale, mild denoise, adaptive threshold. Falls
    back to the untouched image if OpenCV is unavailable."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return image
    try:
        from PIL import Image
        arr = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        if aggressive:
            gray = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
            gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                         cv2.THRESH_BINARY, 31, 15)
        else:
            gray = cv2.bilateralFilter(gray, 5, 50, 50)
        return Image.fromarray(gray)
    except Exception as e:
        logger.debug(f"OCR preprocessing skipped: {e}")
        return image


# ------------------------------------------------------------------ engines

def _run_tesseract(image, lang: str = "eng") -> OCRResult:
    try:
        import pytesseract
        exe = find_tesseract_binary()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        data = pytesseract.image_to_data(image, lang=lang,
                                         output_type=pytesseract.Output.DICT)
    except Exception as e:
        _ENGINE_FAILED["tesseract"] = str(e)
        return OCRResult(engine="tesseract", error=str(e))

    lines: List[OCRLine] = []
    grouped: Dict[tuple, List[int]] = {}
    for i, word in enumerate(data.get("text", [])):
        if not str(word).strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        grouped.setdefault(key, []).append(i)

    for key in sorted(grouped):
        idxs = grouped[key]
        words, confs = [], []
        x0 = y0 = 10 ** 9
        x1 = y1 = 0
        for i in idxs:
            words.append(str(data["text"][i]).strip())
            try:
                c = float(data["conf"][i])
            except (TypeError, ValueError):
                c = -1.0
            if c >= 0:
                confs.append(c / 100.0)
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x + w), max(y1, y + h)
        text = " ".join(words).strip()
        if text:
            lines.append(OCRLine(text=text,
                                 confidence=sum(confs) / len(confs) if confs else 0.0,
                                 bbox=[float(x0), float(y0), float(x1), float(y1)]))

    full = "\n".join(l.text for l in lines)
    mean = sum(l.confidence for l in lines) / len(lines) if lines else 0.0
    return OCRResult(text=full, lines=lines, engine="tesseract", confidence=mean)


def _get_paddle(lang: str = "en"):
    key = f"paddle:{lang}"
    if key in _ENGINE_CACHE:
        return _ENGINE_CACHE[key]
    from paddleocr import PaddleOCR
    # show_log/use_angle_cls keyword names differ between PaddleOCR majors -
    # build with the widest set the installed version accepts.
    for kwargs in ({"lang": lang, "use_angle_cls": True, "show_log": False},
                   {"lang": lang, "use_angle_cls": True},
                   {"lang": lang}):
        try:
            _ENGINE_CACHE[key] = PaddleOCR(**kwargs)
            return _ENGINE_CACHE[key]
        except TypeError:
            continue
    _ENGINE_CACHE[key] = PaddleOCR()
    return _ENGINE_CACHE[key]


def _run_paddleocr(image, lang: str = "en") -> OCRResult:
    try:
        import numpy as np
        ocr = _get_paddle(lang)
        arr = np.array(image.convert("RGB"))
        try:
            raw = ocr.ocr(arr, cls=True)
        except TypeError:
            raw = ocr.ocr(arr)
    except Exception as e:
        _ENGINE_FAILED["paddleocr"] = str(e)
        logger.warning(f"PaddleOCR unavailable: {e}")
        return OCRResult(engine="paddleocr", error=str(e))

    lines: List[OCRLine] = []
    try:
        pages = raw if raw and isinstance(raw[0], list) else [raw]
        for page in pages:
            for item in page or []:
                # classic layout: [box, (text, score)]
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    box, payload = item
                    if isinstance(payload, (list, tuple)) and len(payload) == 2:
                        text, score = payload[0], float(payload[1])
                        xs = [p[0] for p in box]
                        ys = [p[1] for p in box]
                        lines.append(OCRLine(text=str(text).strip(), confidence=score,
                                             bbox=[min(xs), min(ys), max(xs), max(ys)]))
    except Exception as e:
        return OCRResult(engine="paddleocr", error=f"unexpected PaddleOCR output: {e}")

    full = "\n".join(l.text for l in lines if l.text)
    mean = sum(l.confidence for l in lines) / len(lines) if lines else 0.0
    return OCRResult(text=full, lines=lines, engine="paddleocr", confidence=mean)


def _run_easyocr(image) -> OCRResult:
    try:
        import numpy as np
        if "easyocr" not in _ENGINE_CACHE:
            # Reuse the reader the existing vision tool already caches when it
            # is loaded, otherwise build our own - never two readers at once.
            try:
                from core.builtin_tools import _get_ocr_reader
                _ENGINE_CACHE["easyocr"] = _get_ocr_reader()
            except Exception:
                import easyocr
                _ENGINE_CACHE["easyocr"] = easyocr.Reader(["en"], gpu=False, verbose=False,
                                                          download_enabled=False)
        reader = _ENGINE_CACHE["easyocr"]
        results = reader.readtext(np.array(image.convert("RGB")))
    except Exception as e:
        _ENGINE_FAILED["easyocr"] = str(e)
        return OCRResult(engine="easyocr", error=str(e))

    lines = []
    for bbox, text, conf in results:
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        lines.append(OCRLine(text=str(text).strip(), confidence=float(conf),
                             bbox=[min(xs), min(ys), max(xs), max(ys)]))
    full = "\n".join(l.text for l in lines if l.text)
    mean = sum(l.confidence for l in lines) / len(lines) if lines else 0.0
    return OCRResult(text=full, lines=lines, engine="easyocr", confidence=mean)


def _run_rapidocr(image) -> OCRResult:
    """RapidOCR: PP-OCR detection + recognition models executed by ONNX Runtime.
    The models ship inside the wheel, so this works offline from the first run
    and needs no torch."""
    try:
        import numpy as np
        if "rapidocr" not in _ENGINE_CACHE:
            if _rapidocr_module() == "rapidocr_onnxruntime":
                from rapidocr_onnxruntime import RapidOCR
            else:
                from rapidocr import RapidOCR
            _ENGINE_CACHE["rapidocr"] = RapidOCR()
        engine = _ENGINE_CACHE["rapidocr"]
        output = engine(np.array(image.convert("RGB")))
        if isinstance(output, tuple):                   # 1.x: (results, elapsed)
            results = output[0]
        else:                                           # 3.x: RapidOCROutput
            boxes = output.boxes if output.boxes is not None else []
            results = list(zip([b.tolist() for b in boxes], output.txts or (),
                               output.scores or ()))
    except Exception as e:
        _ENGINE_FAILED["rapidocr"] = str(e)
        return OCRResult(engine="rapidocr", error=str(e))
    lines = []
    for item in results or []:
        box, text, conf = item[0], item[1], item[2]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(OCRLine(text=str(text).strip(), confidence=float(conf),
                             bbox=[min(xs), min(ys), max(xs), max(ys)]))
    lines.sort(key=lambda l: (round(l.bbox[1] / 12), l.bbox[0]))
    full = "\n".join(l.text for l in lines if l.text)
    mean = sum(l.confidence for l in lines) / len(lines) if lines else 0.0
    return OCRResult(text=full, lines=lines, engine="rapidocr", confidence=mean)


_RUNNERS = {"paddleocr": _run_paddleocr, "rapidocr": _run_rapidocr,
            "tesseract": _run_tesseract, "easyocr": _run_easyocr}

# Default order: strongest document engine first, then RapidOCR (installed by
# default, offline, no torch), then the other fallbacks.
DEFAULT_ORDER = ("paddleocr", "rapidocr", "tesseract", "easyocr")

# PaddleOCR/EasyOCR use ISO-639-1 ('en'), Tesseract uses ISO-639-2 ('eng').
_TESSERACT_LANG = {"en": "eng", "hi": "hin", "fr": "fra", "de": "deu", "es": "spa",
                   "it": "ita", "pt": "por", "ru": "rus", "ja": "jpn", "ch": "chi_sim",
                   "zh": "chi_sim", "ar": "ara"}


def run_ocr(image, order: Optional[List[str]] = None, lang: str = "en",
            min_chars: int = 12, preprocess: bool = True) -> OCRResult:
    """Run OCR through the layered engine order and return the first result
    that produces usable text. Later engines are tried when an earlier one is
    unavailable, errors, or returns almost nothing (a common symptom of a
    low-contrast scan that a different engine handles better)."""
    order = list(order or DEFAULT_ORDER)
    prepared = preprocess_for_ocr(image) if preprocess else image

    attempts: List[OCRResult] = []
    for name in order:
        if not engine_available(name):
            continue
        runner = _RUNNERS[name]
        if name == "tesseract":
            result = runner(prepared, _TESSERACT_LANG.get(lang, lang))
        elif name == "paddleocr":
            result = runner(prepared, lang)
        else:
            result = runner(prepared)
        attempts.append(result)
        if result.ok and len(result.text.strip()) >= min_chars:
            return result

    if attempts:
        best = max(attempts, key=lambda r: (len(r.text.strip()), r.confidence))
        if best.text.strip():
            return best
        return best
    return OCRResult(engine="none", error="no OCR engine available "
                                          "(run STEP 1 : INSTALL - it installs RapidOCR)")


# --------------------------------------------------------- layout / structure

@dataclass
class LayoutRegion:
    region_type: str           # title|text|list|table|figure|header|footer
    bbox: List[float]
    text: str = ""
    confidence: float = 0.0
    order: int = 0


def paddle_structure_available() -> bool:
    if not engine_available("paddleocr"):
        return False
    try:
        import paddleocr  # noqa: F401
        return (hasattr(__import__("paddleocr"), "PPStructure")
                or hasattr(__import__("paddleocr"), "PPStructureV3"))
    except Exception:
        return False


def analyse_layout(image) -> List[LayoutRegion]:
    """Document structure analysis: page layout regions, headings, paragraphs,
    tables and figures. Uses PaddleOCR PP-Structure when it is installed, and
    otherwise falls back to a deterministic geometric analyser built from OCR
    line boxes (see heuristic_layout). Never fails hard."""
    if paddle_structure_available():
        try:
            return _paddle_structure(image)
        except Exception as e:
            logger.warning(f"PP-Structure failed, using heuristic layout: {e}")
    return heuristic_layout(image)


def _paddle_structure(image) -> List[LayoutRegion]:
    import numpy as np
    import paddleocr as _p

    key = "pp_structure"
    if key not in _ENGINE_CACHE:
        cls = getattr(_p, "PPStructure", None) or getattr(_p, "PPStructureV3", None)
        for kwargs in ({"show_log": False, "layout": True, "table": True, "ocr": True},
                       {"show_log": False}, {}):
            try:
                _ENGINE_CACHE[key] = cls(**kwargs)
                break
            except TypeError:
                continue
        else:  # pragma: no cover
            raise RuntimeError("could not construct PP-Structure")
    engine = _ENGINE_CACHE[key]

    arr = np.array(image.convert("RGB"))
    result = engine(arr)
    regions: List[LayoutRegion] = []
    for i, item in enumerate(result or []):
        rtype = str(item.get("type", "text")).lower()
        bbox = [float(v) for v in item.get("bbox", [0, 0, 0, 0])]
        text = ""
        res = item.get("res")
        if isinstance(res, list):
            parts = []
            for r in res:
                if isinstance(r, dict) and r.get("text"):
                    parts.append(str(r["text"]))
            text = "\n".join(parts)
        elif isinstance(res, dict):
            text = str(res.get("html") or res.get("text") or "")
        regions.append(LayoutRegion(region_type=rtype, bbox=bbox, text=text,
                                    confidence=float(item.get("score", 0.0) or 0.0), order=i))
    return regions


def heuristic_layout(image) -> List[LayoutRegion]:
    """Deterministic fallback layout analysis from OCR line geometry.

    Not a replacement for PP-Structure, but enough to keep the pipeline's
    contract on a machine where PaddleOCR is not installed: it groups OCR
    lines into blocks by vertical gaps, marks the top and bottom bands as
    header/footer, marks short high-and-bold-ish lines as headings, and
    marks blocks whose lines share strong column alignment as tables.
    """
    ocr = run_ocr(image, order=["tesseract", "paddleocr", "easyocr"])
    if not ocr.lines:
        return []

    height = getattr(image, "height", 0) or max((l.bbox[3] for l in ocr.lines if l.bbox), default=1)
    lines = [l for l in ocr.lines if l.bbox]
    lines.sort(key=lambda l: (l.bbox[1], l.bbox[0]))
    if not lines:
        return [LayoutRegion(region_type="text", bbox=[0, 0, 0, 0], text=ocr.text, order=0)]

    gaps = []
    for prev, cur in zip(lines, lines[1:]):
        gaps.append(max(0.0, cur.bbox[1] - prev.bbox[3]))
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
    split_gap = max(median_gap * 2.0, 8.0)

    blocks: List[List[OCRLine]] = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:]):
        if (cur.bbox[1] - prev.bbox[3]) > split_gap:
            blocks.append([cur])
        else:
            blocks[-1].append(cur)

    regions: List[LayoutRegion] = []
    for order, block in enumerate(blocks):
        x0 = min(l.bbox[0] for l in block)
        y0 = min(l.bbox[1] for l in block)
        x1 = max(l.bbox[2] for l in block)
        y1 = max(l.bbox[3] for l in block)
        text = "\n".join(l.text for l in block)
        conf = sum(l.confidence for l in block) / len(block)

        rtype = "text"
        if height and y1 < height * 0.07:
            rtype = "header"
        elif height and y0 > height * 0.93:
            rtype = "footer"
        elif len(block) == 1 and len(text) <= 80 and (
                text.isupper() or text.rstrip().endswith(":") or _looks_like_heading(text)):
            rtype = "title"
        elif _looks_like_table(block):
            rtype = "table"
        elif _looks_like_list(block):
            rtype = "list"
        regions.append(LayoutRegion(region_type=rtype, bbox=[x0, y0, x1, y1], text=text,
                                    confidence=conf, order=order))
    return regions


def _looks_like_heading(text: str) -> bool:
    import re
    t = text.strip()
    if len(t.split()) > 12:
        return False
    return bool(re.match(r"^(?:\d+(?:\.\d+)*[.)]?\s+|[A-Z][A-Z0-9 /&\-]{3,}$)", t))


def _looks_like_list(block: List[OCRLine]) -> bool:
    import re
    if len(block) < 2:
        return False
    marks = sum(1 for l in block if re.match(r"^\s*(?:[-*\u2022]|\(?\d{1,2}[.)])\s+", l.text))
    return marks >= max(2, len(block) // 2)


def _looks_like_table(block: List[OCRLine]) -> bool:
    """Rows of a table share left-edge alignment across several x positions and
    contain multiple whitespace-separated columns."""
    if len(block) < 3:
        return False
    multi_col = sum(1 for l in block if len(l.text.split()) >= 3
                    and any(sep in l.text for sep in ("  ", "\t", "|")))
    numeric_rows = sum(1 for l in block
                       if sum(ch.isdigit() for ch in l.text) >= max(3, len(l.text) * 0.2))
    return multi_col >= len(block) * 0.6 or numeric_rows >= len(block) * 0.7


def engine_report() -> dict:
    """Status block used by the audit, offline test and startup BAT."""
    tess = find_tesseract_binary()
    return {
        "available": available_engines(),
        "order": list(DEFAULT_ORDER),
        "paddleocr": {"installed": _has("paddleocr"), "backend": _has("paddle"),
                      "structure": paddle_structure_available()},
        "tesseract": {"installed": _has("pytesseract"), "binary": tess},
        "easyocr": {"installed": _has("easyocr")},
        "rapidocr": {"installed": _rapidocr_module() is not None,
                     "package": _rapidocr_module()},
        "usable": usable_engines(),
        "failed_this_session": dict(_ENGINE_FAILED),
        "layout_analyser": "PP-Structure" if paddle_structure_available() else "heuristic (geometric)",
    }


def tesseract_languages() -> List[str]:
    exe = find_tesseract_binary()
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=15)
        return [ln.strip() for ln in (r.stdout or "").splitlines()[1:] if ln.strip()]
    except Exception:
        return []
