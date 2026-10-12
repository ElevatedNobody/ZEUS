"""
Vernacular site text -> a normalised English reading, with the original kept.

Site notes in India are rarely clean English. A foreman writes "P17 khudai
poori ho gayi, PCC kal se", a supervisor sends "पियर P17: सीमेंट की आपूर्ति
नहीं हुई, ढलाई रुकी हुई है", a DPR says "exc compl, conc WIP 60%". Existing
project tools expect a person to retype that into a form.

This module reads it deterministically first:

  * scripts are detected per Unicode block (Devanagari, Bengali, Gurmukhi,
    Gujarati, Odia, Tamil, Telugu, Kannada, Malayalam, Latin);
  * native digits (०-९, ০-৯, ௦-௯ ...) become ASCII digits;
  * a construction lexicon maps regional words, transliterated Hindi
    ("Hinglish") and site shorthand to an English reading, longest phrase
    first, each mapping recorded as a term with its category;
  * the result says how much of the text it understood (coverage) - so a
    caller knows when a local multilingual model should be asked as well.

The original text is never replaced: `normalize()` returns it untouched next
to the reading, and the capture pipeline stores both.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

SCRIPT_RANGES = [
    ("Devanagari", 0x0900, 0x097F), ("Bengali", 0x0980, 0x09FF), ("Gurmukhi", 0x0A00, 0x0A7F),
    ("Gujarati", 0x0A80, 0x0AFF), ("Odia", 0x0B00, 0x0B7F), ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F), ("Kannada", 0x0C80, 0x0CFF), ("Malayalam", 0x0D00, 0x0D7F),
]
SCRIPT_LANGUAGE = {"Devanagari": "hi", "Bengali": "bn", "Gurmukhi": "pa", "Gujarati": "gu", "Odia": "or",
                   "Tamil": "ta", "Telugu": "te", "Kannada": "kn", "Malayalam": "ml", "Latin": "en"}

# (surface, English reading, category). Categories: element, work, status, cause,
# material, resource, time, quantity, connector. Longest phrase wins.
_LEXICON: List[Tuple[str, str, str]] = [
    # ---------------------------------------------------------- Hindi (Devanagari)
    ("खुदाई", "excavation", "work"), ("खोदाई", "excavation", "work"), ("ढलाई", "concreting", "work"),
    ("कंक्रीटिंग", "concreting", "work"), ("कंक्रीट", "concrete", "material"), ("सरिया", "rebar", "material"),
    ("सरिये", "rebar", "material"), ("शटरिंग", "shuttering", "work"), ("बांधाई", "rebar tying", "work"),
    ("पियर", "pier", "element"), ("पिअर", "pier", "element"), ("पाइल", "pile", "element"), ("गर्डर", "girder", "element"),
    ("स्लैब", "slab", "element"), ("नींव", "foundation", "element"), ("फाउंडेशन", "foundation", "element"),
    ("पुल", "bridge", "element"), ("पुलिया", "culvert", "element"), ("सड़क", "road", "element"), ("सडक", "road", "element"),
    ("नाली", "drain", "element"), ("दीवार", "wall", "element"), ("कैप", "cap", "element"),
    ("मिट्टी", "earth", "material"), ("भराई", "filling", "work"), ("बिछाई", "laying", "work"), ("क्योरिंग", "curing", "work"),
    ("तराई", "curing", "work"), ("सीमेंट", "cement", "material"), ("रेत", "sand", "material"), ("बालू", "sand", "material"),
    ("गिट्टी", "aggregate", "material"), ("ईंट", "brick", "material"), ("मजदूर", "labour", "resource"),
    ("मज़दूर", "labour", "resource"), ("मशीन", "machine", "resource"), ("क्रेन", "crane", "resource"),
    ("पूरी हो गई", "completed", "status"), ("पूरी हो गयी", "completed", "status"), ("पूरा हो गया", "completed", "status"),
    ("पूर्ण हो गया", "completed", "status"), ("पूर्ण", "complete", "status"), ("पूरा", "complete", "status"),
    ("पूरी", "complete", "status"), ("हो गया", "done", "status"), ("हो गई", "done", "status"), ("हो गयी", "done", "status"),
    ("शुरू हो गया", "started", "status"), ("शुरू हो गई", "started", "status"), ("शुरू हो गयी", "started", "status"),
    ("चालू हो गया", "started", "status"), ("चालू हो गई", "started", "status"),
    ("शुरू हुआ", "started", "status"), ("शुरू हुई", "started", "status"), ("शुरू", "started", "status"),
    ("चालू", "in progress", "status"), ("जारी", "in progress", "status"), ("बाकी", "remaining", "status"),
    ("बाक़ी", "remaining", "status"), ("रुकी हुई है", "is stopped", "status"), ("रुका हुआ है", "is stopped", "status"),
    ("रुकी", "stopped", "status"), ("रुका", "stopped", "status"), ("बंद", "stopped", "status"),
    ("देरी", "delay", "status"), ("विलंब", "delay", "status"), ("देर", "delay", "status"),
    ("नहीं हुई", "not received", "status"), ("नहीं हुआ", "not done", "status"), ("नहीं आया", "not received", "status"),
    ("नहीं आई", "not received", "status"), ("नहीं पहुंचा", "not received", "status"), ("नहीं", "not", "connector"),
    ("कमी", "shortage", "cause"), ("आपूर्ति", "supply", "cause"), ("सप्लाई", "supply", "cause"),
    ("बारिश", "rain", "cause"), ("वर्षा", "rain", "cause"), ("बाढ़", "flood", "cause"), ("अनुमति", "permission", "cause"),
    ("बिजली", "power", "cause"), ("खराब", "breakdown", "cause"), ("हड़ताल", "strike", "cause"),
    ("अपेक्षित", "expected", "time"), ("उम्मीद", "expected", "time"), ("तक", "by", "time"), ("से", "from", "connector"),
    ("आज", "today", "time"), ("कल से", "from tomorrow", "time"), ("दिन", "days", "time"),
    ("प्रतिशत", "percent", "quantity"), ("घन मीटर", "m3", "quantity"), ("मीटर", "m", "quantity"),
    ("की", "of", "connector"), ("का", "of", "connector"), ("के", "of", "connector"), ("है", "is", "connector"),
    ("और", "and", "connector"), ("में", "in", "connector"), ("पर", "at", "connector"), ("काम", "work", "work"),
    # Marathi (Devanagari)
    ("झाले", "done", "status"), ("झाली", "done", "status"), ("सुरू", "started", "status"), ("थांबले", "stopped", "status"),
    ("पाऊस", "rain", "cause"), ("आहे", "is", "connector"),
    # Tamil
    ("முடிந்தது", "completed", "status"), ("தொடங்கியது", "started", "status"), ("தாமதம்", "delay", "status"),
    ("மழை", "rain", "cause"), ("சிமெண்ட்", "cement", "material"), ("கான்கிரீட்", "concrete", "material"),
    ("தூண்", "pier", "element"), ("பாலம்", "bridge", "element"), ("நிறுத்தப்பட்டது", "stopped", "status"),
    # Telugu
    ("పూర్తి", "complete", "status"), ("ప్రారంభం", "started", "status"), ("ఆలస్యం", "delay", "status"),
    ("వర్షం", "rain", "cause"), ("సిమెంట్", "cement", "material"), ("నిలిచిపోయింది", "stopped", "status"),
    # Bengali
    ("সম্পূর্ণ", "complete", "status"), ("শুরু", "started", "status"), ("দেরি", "delay", "status"),
    ("বৃষ্টি", "rain", "cause"), ("সিমেন্ট", "cement", "material"), ("বন্ধ", "stopped", "status"),
    # Gujarati
    ("પૂર્ણ", "complete", "status"), ("શરૂ", "started", "status"), ("વરસાદ", "rain", "cause"),
    ("સિમેન્ટ", "cement", "material"), ("વિલંબ", "delay", "status"),
    # Kannada / Malayalam / Punjabi / Odia
    ("ಪೂರ್ಣ", "complete", "status"), ("ಮಳೆ", "rain", "cause"), ("പൂർത്തിയായി", "completed", "status"),
    ("മഴ", "rain", "cause"), ("ਪੂਰਾ", "complete", "status"), ("ਮੀਂਹ", "rain", "cause"), ("ସମ୍ପୂର୍ଣ୍ଣ", "complete", "status"),

    # ---------------------------------------------- transliterated Hindi (Latin)
    ("khudai", "excavation", "work"), ("khudaai", "excavation", "work"), ("khodai", "excavation", "work"),
    ("dhalai", "concreting", "work"), ("dhalaai", "concreting", "work"), ("dhalaayi", "concreting", "work"),
    ("sariya", "rebar", "material"), ("saria", "rebar", "material"), ("bharai", "filling", "work"),
    ("tarai", "curing", "work"), ("mitti", "earth", "material"), ("gitti", "aggregate", "material"),
    ("reti", "sand", "material"), ("mazdoor", "labour", "resource"), ("majdoor", "labour", "resource"),
    ("poori ho gayi", "completed", "status"), ("puri ho gayi", "completed", "status"), ("poora ho gaya", "completed", "status"),
    ("pura ho gaya", "completed", "status"), ("poori ho gai", "completed", "status"), ("puri ho gai", "completed", "status"),
    # "shuru ho gayi" is STARTED, not done: the phrase is matched before "ho gayi"
    ("shuru ho gayi", "started", "status"), ("shuru ho gaya", "started", "status"), ("shuru ho gai", "started", "status"),
    ("shuru hua", "started", "status"), ("shuru hui", "started", "status"), ("chalu ho gaya", "started", "status"),
    ("chalu ho gayi", "started", "status"), ("chalu hai", "in progress", "status"), ("jari hai", "in progress", "status"),
    ("ho gaya", "done", "status"), ("ho gayi", "done", "status"), ("ho gai", "done", "status"),
    ("poora", "complete", "status"), ("pura", "complete", "status"), ("poori", "complete", "status"),
    ("puri", "complete", "status"), ("shuru", "started", "status"), ("chalu", "in progress", "status"),
    ("baaki", "remaining", "status"), ("baki", "remaining", "status"), ("ruka hua", "stopped", "status"),
    ("ruki hui", "stopped", "status"), ("ruka", "stopped", "status"), ("ruki", "stopped", "status"),
    ("band hai", "is stopped", "status"), ("deri", "delay", "status"), ("nahi aaya", "not received", "status"),
    ("nahi aayi", "not received", "status"), ("nahi aaya hai", "not received", "status"), ("nahi pahuncha", "not received", "status"),
    ("nahi hua", "not done", "status"), ("nahi", "not", "connector"), ("nahin", "not", "connector"),
    ("barish", "rain", "cause"), ("baarish", "rain", "cause"), ("kami", "shortage", "cause"),
    ("maal", "material", "material"), ("kal se", "from tomorrow", "time"), ("aaj", "today", "time"),
    ("tak", "by", "time"), ("umeed", "expected", "time"), ("kaam", "work", "work"), ("hai", "is", "connector"),
    ("ki", "of", "connector"), ("ka", "of", "connector"), ("aur", "and", "connector"), ("se", "from", "connector"),

    # ------------------------------------------------------------ site shorthand
    ("exc.", "excavation", "work"), ("exc", "excavation", "work"), ("excvn", "excavation", "work"),
    ("excavn", "excavation", "work"), ("pcc", "plain cement concrete (PCC)", "work"),
    ("rcc", "reinforced cement concrete (RCC)", "work"), ("conc.", "concreting", "work"), ("conc", "concreting", "work"),
    ("concg", "concreting", "work"), ("reinf", "reinforcement", "work"), ("shutt", "shuttering", "work"),
    ("compl.", "complete", "status"), ("compl", "complete", "status"), ("cmpl", "complete", "status"),
    ("comp.", "complete", "status"), ("wip", "in progress", "status"), ("u/p", "in progress", "status"),
    ("u/c", "under construction", "status"), ("cum", "m3", "quantity"), ("cu.m", "m3", "quantity"),
    ("cu m", "m3", "quantity"), ("m³", "m3", "quantity"), ("rmt", "running metres", "quantity"),
    ("nos", "numbers", "quantity"), ("mtrl", "material", "material"), ("matl", "material", "material"),
    ("eqpt", "equipment", "resource"), ("abut", "abutment", "element"), ("ch.", "chainage", "element"),
    ("gsb", "granular sub-base (GSB)", "work"), ("wmm", "wet mix macadam (WMM)", "work"),
    ("dbm", "dense bituminous macadam (DBM)", "work"), ("dlc", "dry lean concrete (DLC)", "work"),
    ("pqc", "pavement quality concrete (PQC)", "work"), ("ogl", "original ground level", "element"),
    ("eot", "extension of time", "cause"), ("rfi", "request for information", "cause"),
    ("ncr", "non-conformance report", "cause"), ("dpr", "daily progress report", "work"),
    ("approx", "approximately", "quantity"), ("qty", "quantity", "quantity"),
]

_NATIVE_DIGITS = re.compile(r"[०-९০-৯੦-੯૦-૯୦-୯"
                            r"௦-௯౦-౯೦-೯൦-൯]")
_HINGLISH_MARKERS = {"hai", "ho", "gaya", "gayi", "gai", "nahi", "nahin", "kal", "aaj", "poora", "pura", "poori",
                     "baaki", "baki", "khudai", "dhalai", "shuru", "chalu", "tak", "se", "ki", "ka", "mein"}
_WORD = re.compile(r"[^\W_]+(?:[.'/][^\W_]+)*\.?", re.UNICODE)


def _is_latin(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def detect_scripts(text: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for ch in text or "":
        code = ord(ch)
        if _is_latin(ch):
            counts["Latin"] = counts.get("Latin", 0) + 1
            continue
        for name, low, high in SCRIPT_RANGES:
            if low <= code <= high:
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


def ascii_digits(text: str) -> str:
    return _NATIVE_DIGITS.sub(lambda m: str(unicodedata.digit(m.group(0))), text or "")


@dataclass
class Normalized:
    original: str
    normalized: str
    language: str                                  # en | hi | hi-Latn (Hinglish) | ta | ... | mixed
    scripts: Dict[str, int] = field(default_factory=dict)
    terms: List[Dict[str, str]] = field(default_factory=list)
    coverage: float = 1.0                          # share of non-Latin / vernacular words understood
    vernacular: bool = False
    method: str = "lexicon"
    model: str = ""
    translation: str = ""                          # a model's English reading (inference), when one was asked
    confidence: float = 1.0

    def as_dict(self) -> Dict:
        return asdict(self)


def _month_entries() -> List[Tuple[str, str, str]]:
    """Hindi and transliterated month names read as English months, so the
    date reader sees '11 September' in '11 सितंबर तक'."""
    import calendar
    from jarvis.dates import MONTHS
    english = {i: calendar.month_name[i] for i in range(1, 13)}
    return [(name, english[number], "time") for name, number in MONTHS.items()
            if not name.isascii() or name in ("janvari", "farvari", "aprail", "julai", "agast", "sitambar",
                                               "sitmbar", "aktubar", "navambar", "disambar")]


def _compile_lexicon():
    entries = sorted(_LEXICON + _month_entries(), key=lambda item: len(item[0]), reverse=True)
    latin, native = [], []
    for surface, reading, category in entries:
        if surface.isascii():
            pattern = r"(?<![A-Za-z0-9])" + re.escape(surface) + r"(?![A-Za-z0-9])"
            latin.append((re.compile(pattern, re.IGNORECASE), surface, reading, category))
        else:
            # Indic words: bounded by anything that is not a letter or combining mark
            # (the danda "।" / "॥" is punctuation, so it is a boundary)
            letter = r"[\wऀ-ॣ०-෿]"
            pattern = rf"(?<!{letter})" + re.escape(surface) + rf"(?!{letter})"
            native.append((re.compile(pattern), surface, reading, category))
    return native, latin


_NATIVE_LEXICON, _LATIN_LEXICON = _compile_lexicon()


def _language(scripts: Dict[str, int], text: str) -> Tuple[str, bool]:
    indic = {k: v for k, v in scripts.items() if k != "Latin"}
    latin = scripts.get("Latin", 0)
    if indic:
        main = max(indic, key=indic.get)
        code = SCRIPT_LANGUAGE.get(main, "und")
        if main == "Devanagari" and re.search(r"झाले|झाली|आहे|थांबले", text):
            code = "mr"
        if latin and latin > sum(indic.values()) * 0.8:
            return "mixed", True
        return code, True
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", text or "")}
    if len(words & _HINGLISH_MARKERS) >= 2:
        return "hi-Latn", True
    return "en", False


def normalize(text: str) -> Normalized:
    """Deterministic reading of site text. Never loses the original."""
    original = text or ""
    working = ascii_digits(original)
    scripts = detect_scripts(original)
    language, vernacular = _language(scripts, original)
    terms: List[Dict[str, str]] = []
    placeholders: List[str] = []

    def substitute(pattern, surface, reading, category, value):
        def repl(match):
            terms.append({"surface": match.group(0), "reading": reading, "category": category})
            placeholders.append(reading)
            return f"\u0001{len(placeholders) - 1}\u0001"
        return pattern.sub(repl, value)

    for pattern, surface, reading, category in _NATIVE_LEXICON:
        working = substitute(pattern, surface, reading, category, working)
    if vernacular or language == "en":
        # Hinglish words only when the text is Hinglish/vernacular; shorthand always.
        for pattern, surface, reading, category in _LATIN_LEXICON:
            is_shorthand = category in ("quantity",) or surface in _SHORTHAND
            if language == "en" and not is_shorthand:
                continue
            working = substitute(pattern, surface, reading, category, working)
    reading = re.sub(r"\u0001(\d+)\u0001", lambda m: placeholders[int(m.group(1))], working)
    reading = re.sub(r"\s+([,.;:!?])", r"\1", re.sub(r"[ \t]+", " ", reading)).strip()

    # How much of the vernacular text was understood: native-script words left
    # untranslated in the reading are what the lexicon did not know.
    native_words = [w for w in _WORD.findall(original) if any(not ch.isascii() for ch in w)]
    unknown = [w for w in _WORD.findall(reading) if any(not ch.isascii() for ch in w)]
    coverage = 1.0 if not native_words else max(0.0, 1.0 - len(unknown) / max(1, len(native_words)))
    if language == "hi-Latn":
        latin_words = re.findall(r"[A-Za-z]+", original)
        coverage = min(1.0, 0.5 + 0.5 * (len(terms) / max(1, len(latin_words) * 0.4)))
    confidence = round(0.55 + 0.45 * coverage, 2) if vernacular else 0.95
    return Normalized(original=original, normalized=reading, language=language, scripts=scripts, terms=terms,
                      coverage=round(coverage, 2), vernacular=vernacular, confidence=confidence)


_SHORTHAND = {s for s, _r, c in _LEXICON if s.isascii() and c in ("work", "status", "element", "quantity",
                                                                    "material", "resource", "cause")
              and s in ("exc.", "exc", "excvn", "excavn", "pcc", "rcc", "conc.", "conc", "concg", "reinf", "shutt",
                        "compl.", "compl", "cmpl", "comp.", "wip", "u/p", "u/c", "cum", "cu.m", "cu m", "m³", "rmt",
                        "nos", "mtrl", "matl", "eqpt", "abut", "ch.", "gsb", "wmm", "dbm", "dlc", "pqc", "ogl",
                        "eot", "rfi", "ncr", "dpr", "approx", "qty")}


def model_translate(result: Normalized, timeout: float = 120.0) -> Normalized:
    """Ask the local multilingual model for an English reading when the lexicon
    did not understand everything. The model's text is stored as `translation`
    (an inference); `normalized` and `original` are left as they were."""
    if not result.vernacular:
        return result
    from jarvis import models
    route = models.route("vernacular")
    if not route.get("usable"):
        result.method = "lexicon"
        return result
    from core.ollama_client import ollama_client
    from security.injection import SYSTEM_RULE, neutralise
    prompt = ("Translate this construction site note into plain English. Keep every number, date, code "
              "(like P17) and unit exactly as written. Do not add anything that is not in the note. "
              "Reply with the English text only.\n\n<untrusted_document>\n"
              f"{neutralise(result.original)}\n</untrusted_document>")
    reply = ollama_client.chat(route["model"], [{"role": "system", "content": SYSTEM_RULE},
                                                {"role": "user", "content": prompt}],
                               retries=0, timeout=timeout, task_type="multilingual",
                               purpose="ZEUS vernacular site note")
    if reply and not reply.startswith("[") and _numbers_preserved(result.original, reply):
        result.translation = reply.strip()
        result.method = "lexicon+model"
        result.model = route["model"]
    return result


def _numbers_preserved(original: str, translation: str) -> bool:
    """A translation that lost or invented a number is not trusted."""
    source = set(re.findall(r"\d+(?:\.\d+)?", ascii_digits(original)))
    target = set(re.findall(r"\d+(?:\.\d+)?", translation))
    return source <= target | {n.lstrip("0") or "0" for n in target} and len(target - source) <= 2
