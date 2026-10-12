"""
Turning written answers into something worth listening to.

The problem this solves
-----------------------
An LLM answer is written to be READ. It has headings, bullet lists, markdown
emphasis, bracketed citations, file paths and units written as symbols. Fed
straight to a speech model, that comes out as:

    "asterisk asterisk Findings colon asterisk asterisk hyphen Pressure colon
     four point eight two b-a-r hyphen Temperature colon one eight three
     degree-sign C"

which is why local TTS so often sounds like a machine reading a document. It
is not the voice model's fault. It is being given document text.

So this layer sits between the answer and the synthesiser and rewrites the
PRESENTATION - never the content.

The rule that governs everything here
-------------------------------------
**A number, unit, equipment tag, date or safety limit is never altered.**

"DC-101 pressure = 4.82 bar" becomes "DC-101 pressure is 4.82 bar." The tag
survives as a tag, the figure survives to the digit, and "bar" is expanded
only where expansion cannot change meaning. Nothing is rounded, nothing is
converted, nothing is summarised away. A voice layer that quietly turns 4.82
into "about five" on a pressure reading is a safety defect, not a UX
improvement, and there are tests here specifically for that.

What it does do:
  * strips markdown that is meaningless aloud
  * turns symbols into the words a person would actually say
  * expands units only when unambiguous
  * splits into sentences and groups them into speakable chunks
  * plans pauses between thoughts rather than after every comma
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from core.logger import get_logger

logger = get_logger("voice.speech")


# ------------------------------------------------------------ protected --
#
# Anything matching these is copied through untouched. They are extracted
# BEFORE any rewriting and put back afterwards, so no later rule can reach
# inside them. Order matters: the longest, most specific patterns first.
# ORDER MATTERS: longest and most specific first. `SOP-ME-014` has to be
# taken before the plain equipment-tag rule, or that rule matches `ME-014`
# inside it and leaves a mangled `SOP-<marker>` behind.
_PROTECTED = [
    # Document references: SOP-ME-014, WI-OPS-22
    re.compile(r"\b[A-Z]{2,5}-[A-Z]{2,4}-\d{1,4}\b"),
    # Standards: ISO 9001, API 610, ASME B31.3
    re.compile(r"\b(?:ISO|API|ASME|ASTM|IEC|BS|EN|NFPA)\s?[A-Z]?\d{2,5}(?:[.-]\d+)?\b"),
    # ISO dates and clock times, before the bare-number rule could split them
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    # Equipment tags: DC-101, P-204A, PSV-101, TI-1042
    re.compile(r"\b[A-Z]{1,4}-\d{1,5}[A-Z]?\b"),
    # Chemical formulas: H2S, CO2, NH3
    re.compile(r"\b[A-Z][a-z]?\d[A-Za-z0-9]*\b"),
]

# The marker carries NO DIGITS. An earlier version used "\x00{index}\x00",
# and the bare-number pattern then matched the index inside a marker that had
# already been placed - so restoring produced ` 0 ` where an equipment tag
# should have been. Encoding the index in letters removes the whole class of
# bug: nothing in this module matches a run of A-P between NULs.
_ALPHABET = "ABCDEFGHIJKLMNOP"


def _marker(index: int) -> str:
    encoded = ""
    while True:
        encoded = _ALPHABET[index % 16] + encoded
        index //= 16
        if index == 0:
            break
    return f"\x00{encoded}\x00"


def _protect(text: str) -> Tuple[str, List[str]]:
    """Replace every protected token with a marker nothing else can match."""
    kept: List[str] = []

    def take(match: re.Match) -> str:
        kept.append(match.group(0))
        return _marker(len(kept) - 1)

    for pattern in _PROTECTED:
        text = pattern.sub(take, text)
    return text, kept


def _restore(text: str, kept: List[str]) -> str:
    # Longest markers first, so \x00AB\x00 is not damaged by replacing
    # \x00A\x00 inside it.
    for index in sorted(range(len(kept)), key=lambda i: -len(_marker(i))):
        text = text.replace(_marker(index), kept[index])
    return text


# --------------------------------------------------------------- markdown --

_MARKDOWN_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"```[\s\S]*?```"), " "),          # fenced code: unspeakable
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),   # headings
    (re.compile(r"\*\*\*([^*]+)\*\*\*"), r"\1"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)"), r"\1"),
    (re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)"), r"\1"),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),        # bullets
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),      # numbered list markers
    (re.compile(r"^\s*[|+-]{3,}\s*$", re.M), " "),  # table rules
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),  # links: keep the words
    (re.compile(r"^\s*>\s?", re.M), ""),            # block quotes
]


# ------------------------------------------------------------------ units --
#
# Only expansions that CANNOT change meaning. Deliberately conservative:
#   * "bar" is left alone - it is already a word, and "bars" would be wrong
#   * "m" is NOT expanded - it could be metres, milli-, or minutes
#   * "in" is NOT expanded - it is a preposition far more often than inches
# Each entry must be read aloud identically to how an engineer would say it.
_UNITS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(?<=\d)\s?°\s?C\b"), " degrees Celsius"),
    (re.compile(r"(?<=\d)\s?°\s?F\b"), " degrees Fahrenheit"),
    (re.compile(r"(?<=\d)\s?°(?![CF])"), " degrees"),
    (re.compile(r"(?<=\d)\s?%"), " percent"),
    (re.compile(r"(?<=\d)\s?kPa\b"), " kilopascal"),
    (re.compile(r"(?<=\d)\s?MPa\b"), " megapascal"),
    (re.compile(r"(?<=\d)\s?barg\b"), " bar gauge"),
    (re.compile(r"(?<=\d)\s?psig\b"), " P S I gauge"),
    (re.compile(r"(?<=\d)\s?psi\b"), " P S I"),
    (re.compile(r"(?<=\d)\s?rpm\b"), " R P M"),
    (re.compile(r"(?<=\d)\s?mm/s\b"), " millimetres per second"),
    (re.compile(r"(?<=\d)\s?m/s\b"), " metres per second"),
    (re.compile(r"(?<=\d)\s?km/h\b"), " kilometres per hour"),
    (re.compile(r"(?<=\d)\s?kg\b"), " kilograms"),
    (re.compile(r"(?<=\d)\s?mm\b"), " millimetres"),
    (re.compile(r"(?<=\d)\s?cm\b"), " centimetres"),
    (re.compile(r"(?<=\d)\s?km\b"), " kilometres"),
    (re.compile(r"(?<=\d)\s?ms\b"), " milliseconds"),
    (re.compile(r"(?<=\d)\s?GB\b"), " gigabytes"),
    (re.compile(r"(?<=\d)\s?MB\b"), " megabytes"),
    (re.compile(r"(?<=\d)\s?kW\b"), " kilowatts"),
    (re.compile(r"(?<=\d)\s?kV\b"), " kilovolts"),
    (re.compile(r"(?<=\d)\s?Hz\b"), " hertz"),
]

# Symbols a speech model reads as their literal name, or not at all.
_SYMBOLS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\s*=\s*"), " is "),
    (re.compile(r"\s*≈\s*"), " approximately "),
    (re.compile(r"\s*≤\s*"), " no more than "),
    (re.compile(r"\s*≥\s*"), " at least "),
    (re.compile(r"\s*<\s*"), " less than "),
    (re.compile(r"\s*>\s*"), " greater than "),
    (re.compile(r"\s*→\s*"), " then "),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"\s*/\s*(?=[A-Za-z])"), " or "),   # "pass/fail" -> "pass or fail"
    (re.compile(r"\s*\.\.\.\s*"), ", "),
    (re.compile(r"\s*--+\s*"), ", "),
    (re.compile(r"\s*[•·]\s*"), ", "),
    (re.compile(r"\s*\|\s*"), ", "),                # table cells
]

# Abbreviations that read badly when spelled out letter by letter.
_ABBREVIATIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\be\.g\.", re.I), "for example"),
    (re.compile(r"\bi\.e\.", re.I), "that is"),
    (re.compile(r"\betc\.", re.I), "and so on"),
    (re.compile(r"\bvs\.?\b", re.I), "versus"),
    (re.compile(r"\bapprox\.", re.I), "approximately"),
    (re.compile(r"\bmax\.", re.I), "maximum"),
    (re.compile(r"\bmin\.(?=\s)", re.I), "minimum"),
    (re.compile(r"\bNo\.\s*(?=\d)"), "number "),
]


# ------------------------------------------------------------- chunking --

# Below this, a chunk is merged forward: a two-word fragment costs a whole
# synthesis call and comes out clipped.
MIN_CHUNK_CHARS = 60
# Above this, the model's autoregressive pass gets long enough to hurt
# time-to-first-sound, which is the thing that makes local TTS feel broken.
MAX_CHUNK_CHARS = 240

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


@dataclass
class SpeechChunk:
    """One unit of synthesis, with the silence that should follow it."""

    text: str
    pause_after: float = 0.25       # seconds
    index: int = 0
    total: int = 0

    def __str__(self) -> str:
        return self.text


@dataclass
class PreparedSpeech:
    """The result: what to say, in what order, with what pauses."""

    chunks: List[SpeechChunk] = field(default_factory=list)
    spoken_text: str = ""
    original_text: str = ""
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)


# ---------------------------------------------------------------- rules --

def strip_markup(text: str) -> str:
    """Remove formatting that means nothing aloud."""
    for pattern, replacement in _MARKDOWN_RULES:
        text = pattern.sub(replacement, text)
    return text


def say_symbols(text: str) -> str:
    """Symbols and units into the words an engineer would actually say."""
    for pattern, replacement in _UNITS:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _SYMBOLS:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _ABBREVIATIONS:
        text = pattern.sub(replacement, text)
    return text


def normalise(text: str) -> str:
    """The full written-to-spoken pass, with technical content protected.

    Protection happens FIRST and restoration LAST, so no rewriting rule can
    reach inside an equipment tag, a measurement or a date.
    """
    if not text or not text.strip():
        return ""

    text = strip_markup(text)

    # Protect COMPOUND tokens - tags, dates, references, formulas - but NOT
    # bare numbers, because the unit rules use a `(?<=\d)` lookbehind and need
    # to see the digit that precedes the unit. An earlier version protected
    # numbers first, which made every lookbehind fail: "6.8 mm/s" kept its
    # slash and came out as "six point eight millimetres OR seconds".
    #
    # Bare numbers do not need protection from these rules anyway - no unit,
    # symbol or abbreviation rule rewrites a digit.
    protected, kept = _protect(text)
    protected = say_symbols(protected)
    text = _restore(protected, kept)

    # Tidy the whitespace the rules above leave behind.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n{2,} *", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?])(?=[A-Za-z])", r"\1 ", text)
    return text.strip()


def split_sentences(text: str) -> List[str]:
    sentences: List[str] = []
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences.extend(s.strip() for s in _SENTENCE_END.split(paragraph) if s.strip())
    return sentences


def _split_long(sentence: str) -> List[str]:
    """Break one over-long sentence at clause boundaries.

    Written as a single forward pass rather than "split whatever is still too
    long, repeatedly". That loop does not terminate on text with no break
    characters at all - a pasted hash or a long URL - because the head stays
    one character over the cap forever and the same piece is examined again.
    Inside a generator on a daemon thread, that hangs silently.
    """
    remaining = sentence.strip()
    pieces: List[str] = []
    while len(remaining) > MAX_CHUNK_CHARS:
        window = remaining[:MAX_CHUNK_CHARS]
        cut = max(window.rfind("; "), window.rfind(", "), window.rfind(": "))
        if cut >= MIN_CHUNK_CHARS:
            cut += 1
        else:
            cut = window.rfind(" ")
        if cut < MIN_CHUNK_CHARS:
            cut = MAX_CHUNK_CHARS       # no usable break; cut inside the cap
        head, remaining = remaining[:cut].strip(), remaining[cut:].strip()
        if not head:
            break                        # nothing consumed - refuse to spin
        pieces.append(head)
    if remaining:
        pieces.append(remaining)
    return pieces


def plan_pause(chunk: str, is_last: bool) -> float:
    """How long to rest after this chunk.

    Pauses go between THOUGHTS, not after every comma. Over-pausing is what
    makes a voice sound like it is reading a list, which is the exact failure
    this layer exists to avoid.
    """
    if is_last:
        return 0.0
    ending = chunk.rstrip()[-1:] if chunk.strip() else ""
    if ending == "?":
        return 0.45          # a question wants an answer; leave room
    if ending == "!":
        return 0.40
    if ending == ".":
        return 0.35          # a completed thought
    if ending in ",;:":
        return 0.18          # mid-thought: barely a breath
    return 0.25


def _is_speakable(piece: str) -> bool:
    """Is there anything here a person could actually say?

    Markdown rules need content between their markers, so an unpaired `**`
    from a truncated model response is not matched and survives as literal
    text. A chunk of pure punctuation reaches the synthesiser and comes out as
    noise, or as the symbol read by name. Nothing without a letter or a digit
    is worth making a sound for.
    """
    return any(character.isalnum() for character in piece)


def chunk_for_speech(text: str) -> List[SpeechChunk]:
    """Group sentences into speakable units with planned pauses."""
    sentences = [s for s in split_sentences(text) if _is_speakable(s)]
    if not sentences:
        return []

    pieces: List[str] = []
    for sentence in sentences:
        pieces.extend(part for part in _split_long(sentence) if _is_speakable(part))

    merged: List[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) < MIN_CHUNK_CHARS and \
                len(current) + len(piece) + 1 <= MAX_CHUNK_CHARS:
            current = f"{current} {piece}"
        else:
            merged.append(current)
            current = piece
    if current:
        merged.append(current)

    total = len(merged)
    return [SpeechChunk(text=piece, index=index + 1, total=total,
                        pause_after=plan_pause(piece, index == total - 1))
            for index, piece in enumerate(merged)]


def prepare(text: str, max_chars: int = 0) -> PreparedSpeech:
    """The whole pipeline: written answer in, speakable chunks out.

    `max_chars` caps how much of a long answer is spoken. The full text is on
    screen regardless; reading two pages aloud takes minutes nobody wants to
    sit through. 0 means speak everything.
    """
    original = text or ""
    spoken = normalise(original)
    truncated = False

    if max_chars > 0 and len(spoken) > max_chars:
        cut = spoken.rfind(". ", 0, max_chars)
        if cut < max_chars // 2:
            cut = spoken.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        spoken = spoken[:cut + 1].strip() + " The rest of the answer is on screen."
        truncated = True

    return PreparedSpeech(chunks=chunk_for_speech(spoken), spoken_text=spoken,
                          original_text=original, truncated=truncated)


# ------------------------------------------------- the voice system prompt --

VOICE_STYLE_INSTRUCTION = (
    "You are ZEUS, a calm and precise industrial AI assistant, and this "
    "answer will be SPOKEN aloud rather than read.\n"
    "\n"
    "Speak in short, complete sentences, the way you would explain something "
    "to a colleague standing next to you. No headings, no bullet points, no "
    "markdown, no numbered lists. Lead with the answer, then the detail that "
    "matters.\n"
    "\n"
    "Keep every technical value exactly as it is: measurements, pressures, "
    "temperatures, equipment tags, dates, percentages, document references "
    "and safety limits are quoted verbatim, never rounded, converted or "
    "summarised. If you are unsure of a figure, say so rather than producing "
    "one.\n"
    "\n"
    "Do not repeat the question back. Do not describe what you are about to "
    "do. Do not read a report aloud - explain it."
)


def voice_system_prompt(base_prompt: str = "") -> str:
    """The system prompt for VOICE mode.

    This changes only how an answer is PRESENTED. The detailed written path,
    document generation and every tool are untouched - asking for a report
    still produces a report.
    """
    return f"{base_prompt}\n\n{VOICE_STYLE_INSTRUCTION}".strip()
