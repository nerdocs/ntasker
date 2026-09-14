"""Spoken punctuation for dictation.

Vosk returns lowercase words without punctuation, so a spoken "Punkt" or
"question mark" arrives as plain words. :func:`punctuate` turns those
into the marks, glues them to the preceding word, and capitalises the
word after a sentence end or line break -- the way classic dictation
software works. Deterministic, per language, no extra model.
"""

from __future__ import annotations

import os
import re

#: Spoken phrase -> mark, per language. Longer phrases are matched first.
MAPS: dict[str, dict[str, str]] = {
    "de": {
        "neuer absatz": "\n\n",
        "neue zeile": "\n",
        "absatz": "\n\n",
        "ausrufezeichen": "!",
        "rufzeichen": "!",
        "fragezeichen": "?",
        "doppelpunkt": ":",
        "strichpunkt": ";",
        "semikolon": ";",
        "komma": ",",
        "punkt": ".",
    },
    "en": {
        "new paragraph": "\n\n",
        "new line": "\n",
        "exclamation point": "!",
        "exclamation mark": "!",
        "question mark": "?",
        "full stop": ".",
        "semicolon": ";",
        "period": ".",
        "colon": ":",
        "comma": ",",
    },
}

_PATTERNS: dict[str, list[tuple[re.Pattern[str], str]]] = {
    lang: [
        (re.compile(rf"\b{re.escape(phrase)}\b"), mark)
        for phrase, mark in sorted(table.items(), key=lambda kv: -len(kv[0]))
    ]
    for lang, table in MAPS.items()
}


def language_of(spec: str) -> str | None:
    """Language of a ``voice_model`` value: ``de`` from ``de`` / ``de-ch`` /
    ``.../vosk-model-de-0.21``; ``None`` when it names no supported language."""
    name = os.path.basename(spec.rstrip("/")).lower()
    found = re.findall(r"(?<![a-z])(" + "|".join(MAPS) + r")(?![a-z])", name)
    return found[0] if found else None


def punctuate(text: str, lang: str | None) -> str:
    """Replace spoken punctuation in ``text``; unchanged for an unknown ``lang``."""
    patterns = _PATTERNS.get(lang or "")
    if not patterns or not text:
        return text
    for pattern, mark in patterns:
        text = pattern.sub(mark, text)
    text = re.sub(r" +([.,:;?!])", r"\1", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"([.?!] +|\n+)(\w)", lambda m: m[1] + m[2].upper(), text)
    return text.strip(" ")
