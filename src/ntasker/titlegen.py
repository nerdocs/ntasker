"""Automatic task-title suggestion from free text.

Combines two unsupervised keyword extractors:

* **YAKE** -- statistical, yields multi-word candidate phrases with a
  score (lower = better). Provides the actual title building blocks.
* **TextRank** (via ``summa``) -- graph-based keyword ranking. Used to
  *boost* YAKE phrases whose words TextRank also considers central, so
  the two methods vote together instead of competing.

The language is auto-detected with ``langdetect`` so both extractors use
the matching stopword lists / stemmer; anything undetectable or
unsupported falls back to English.

Entry point: :func:`suggest_title` -- used by ``POST /api/title-suggest``
in ``app.py``.
"""

from __future__ import annotations

import re
import warnings

import yake
from langdetect import DetectorFactory, LangDetectException, detect

# summa 1.2 ships regexes with invalid escape sequences -- silence the
# compile-time SyntaxWarnings its import would spam into the server log.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    from summa import keywords as _textrank

# Deterministic language detection (langdetect is randomized by default).
DetectorFactory.seed = 0

MAX_TITLE_LEN = 60

# summa's snowball stemmer wants full language names; map from the
# ISO-639-1 codes langdetect emits. Unlisted languages skip TextRank.
_SUMMA_LANGUAGES = {
    "ar": "arabic",
    "da": "danish",
    "de": "german",
    "en": "english",
    "es": "spanish",
    "fi": "finnish",
    "fr": "french",
    "hu": "hungarian",
    "it": "italian",
    "nl": "dutch",
    "no": "norwegian",
    "pl": "polish",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sv": "swedish",
}

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _detect_language(text: str) -> str:
    """Return the ISO-639-1 code for *text*, falling back to ``en``."""
    try:
        # langdetect may emit region tags like ``zh-cn`` -- keep the base code.
        return detect(text).split("-")[0]
    except LangDetectException:
        return "en"


def _words(phrase: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(phrase)}


def _yake_candidates(text: str, lang: str) -> list[tuple[str, float]]:
    """Top YAKE phrases (up to trigrams) as ``(phrase, score)``, lower = better."""
    try:
        extractor = yake.KeywordExtractor(lan=lang, n=3, top=10)
        return [(phrase, float(score)) for phrase, score in extractor.extract_keywords(text)]
    except Exception:
        # Unsupported language / degenerate input -- retry with English
        # stopwords rather than failing the request.
        if lang == "en":
            return []
        return _yake_candidates(text, "en")


def _textrank_words(text: str, lang: str) -> set[str]:
    """Central single words according to TextRank; empty set on failure."""
    language = _SUMMA_LANGUAGES.get(lang, "english")
    try:
        found = _textrank.keywords(text, language=language, split=True)
        return {w.lower() for w in found}
    except Exception:
        # summa chokes on very short texts and unknown stemmer languages.
        return set()


def _first_line_fallback(text: str) -> str:
    """Degenerate input (too short for extraction): trimmed first line."""
    line = text.strip().splitlines()[0].strip()
    if len(line) > MAX_TITLE_LEN:
        line = line[: MAX_TITLE_LEN - 1].rsplit(" ", 1)[0] + "…"
    return line


def suggest_title(text: str) -> dict[str, str]:
    """Build a short title for *text*.

    Returns ``{"title": ..., "language": ...}``. YAKE phrases are
    re-ranked by dividing their score by ``1 + |phrase words that
    TextRank also ranked central|``, then the best non-overlapping
    phrases are joined until :data:`MAX_TITLE_LEN` is reached.
    """
    lang = _detect_language(text)
    candidates = _yake_candidates(text, lang)
    if not candidates:
        return {"title": _first_line_fallback(text), "language": lang}

    central = _textrank_words(text, lang)
    ranked = sorted(
        candidates,
        key=lambda item: item[1] / (1 + len(_words(item[0]) & central)),
    )

    parts: list[str] = []
    used: set[str] = set()
    for phrase, _score in ranked:
        words = _words(phrase)
        if words & used:  # redundant with an already-chosen phrase
            continue
        joined = " – ".join([*parts, phrase])  # noqa: RUF001 -- en dash intended
        if parts and len(joined) > MAX_TITLE_LEN:
            break
        parts.append(phrase)
        used |= words
        if len(parts) >= 2:
            break

    title = " – ".join(parts)  # noqa: RUF001 -- en dash intended
    title = title[0].upper() + title[1:] if title else _first_line_fallback(text)
    return {"title": title, "language": lang}
