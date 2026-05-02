"""Internationalisation helpers for ntasker.

Stack: Python ``gettext`` stdlib + Babel for extract / compile. Catalogs
ship inside the package at ``src/ntasker/locale/<lang>/LC_MESSAGES/ntasker.{po,mo}``
and are discovered via ``importlib.resources`` so this works equally for
editable installs, sdist, and wheel.

Active-language storage is a :class:`contextvars.ContextVar` (FastAPI is
async; thread-locals would leak across requests). The middleware in
:mod:`ntasker.middleware` sets the var at the start of every request and
resets it on response.

Public surface used elsewhere:

* :func:`_` - translate a constant message id against the active language.
* :func:`_lazy` - lazy variant for use in module-level defaults.
* :func:`get_active_language` / :func:`set_active_language` /
  :func:`reset_active_language` - context-var accessors.
* :func:`current_default` - resolve the default from the ``language``
  setting (used by middleware for ``auto`` and by the CLI).
* :func:`resolve_from_header` - parse an ``Accept-Language`` header and
  pick the best match from :data:`AVAILABLE_LANGUAGES`.
"""

from __future__ import annotations

import contextvars
import gettext
import os
from collections.abc import Iterable
from importlib.resources import as_file, files
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOMAIN: Final[str] = "ntasker"
"""Gettext domain. Catalogs live under ``locale/<lang>/LC_MESSAGES/ntasker.mo``."""

AVAILABLE_LANGUAGES: Final[tuple[str, ...]] = ("en", "de")
"""Languages we ship a catalog for (English is the source language and the
fallback; ``de`` is a real translation)."""

DEFAULT_LANGUAGE: Final[str] = "en"
"""Used when no setting / header / env hint matches."""


# ---------------------------------------------------------------------------
# Catalog directory (resilient against editable / wheel / sdist installs)
# ---------------------------------------------------------------------------


def _resolve_locales_dir() -> Path:
    """Return the on-disk path to the package's ``locale/`` directory.

    Uses ``importlib.resources.files`` + :func:`as_file` so that a wheel
    that lives inside a zip would still be materialised. In our (and the
    common) case ntasker is installed as a regular directory, so this
    just returns a real path.
    """
    pkg_root = files("ntasker")
    locale = pkg_root / "locale"
    # ``as_file`` is a context manager but yields a stable Path for
    # filesystem-backed packages; returning the path object directly is
    # safe here because we never write to it.
    with as_file(locale) as p:
        return Path(p)


LOCALES_DIR: Final[Path] = _resolve_locales_dir()


# ---------------------------------------------------------------------------
# Translation manager - lazily loads + caches a ``GNUTranslations`` per language
# ---------------------------------------------------------------------------


class TranslationManager:
    """Lazy-loaded, process-wide translation cache.

    Catalogs are loaded on first request per language and held for the
    lifetime of the process. ``fallback=True`` means a missing ``.mo``
    file degrades gracefully to the message id (English source string).
    """

    def __init__(self, locales_dir: Path, domain: str = DOMAIN) -> None:
        self._locales_dir = locales_dir
        self._domain = domain
        self._cache: dict[str, gettext.NullTranslations] = {}

    def _load(self, lang: str) -> gettext.NullTranslations:
        cached = self._cache.get(lang)
        if cached is not None:
            return cached
        translation = gettext.translation(
            self._domain,
            localedir=str(self._locales_dir),
            languages=[lang],
            fallback=True,
        )
        self._cache[lang] = translation
        return translation

    def translate(self, msgid: str, lang: str) -> str:
        return self._load(lang).gettext(msgid)


_manager = TranslationManager(LOCALES_DIR)


# ---------------------------------------------------------------------------
# Active-language context var + helpers
# ---------------------------------------------------------------------------


_active_language: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ntasker_active_language", default=None
)


def get_active_language() -> str:
    """Return the active language for the current async/thread context.

    Falls back to :func:`current_default` when no middleware (or CLI
    bootstrap) has set one yet, so calls from REPLs / tests / module
    init still resolve sensibly.
    """
    val = _active_language.get()
    if val is None:
        return current_default()
    return val


def set_active_language(lang: str) -> contextvars.Token[str | None]:
    """Set the active language. Returns the token for :func:`reset_active_language`."""
    return _active_language.set(lang)


def reset_active_language(token: contextvars.Token[str | None]) -> None:
    """Reset the active language to its previous state (paired with :func:`set_active_language`)."""
    _active_language.reset(token)


# ---------------------------------------------------------------------------
# Setting / header / env resolution
# ---------------------------------------------------------------------------


def _normalise(lang: str) -> str | None:
    """Map ``de_AT.UTF-8`` / ``de-AT`` -> ``de`` if in :data:`AVAILABLE_LANGUAGES`."""
    if not lang:
        return None
    # Strip charset / modifier (``de_AT.UTF-8@euro`` -> ``de_AT``)
    base = lang.split(".", 1)[0].split("@", 1)[0]
    # Try full tag (``de_AT``) -- not in our list, then primary (``de``).
    primary = base.replace("_", "-").split("-", 1)[0].lower()
    if primary in AVAILABLE_LANGUAGES:
        return primary
    return None


def resolve_from_header(accept_language: str | None) -> str:
    """Parse an ``Accept-Language`` header and pick the best match.

    Accepts comma-separated language tags with optional ``;q=`` weights;
    returns the highest-weighted tag whose primary subtag is in
    :data:`AVAILABLE_LANGUAGES`. Returns :data:`DEFAULT_LANGUAGE` on
    missing/unparseable input. Robust against junk - never raises.
    """
    if not accept_language:
        return DEFAULT_LANGUAGE
    candidates: list[tuple[float, str]] = []
    for raw in accept_language.split(","):
        token = raw.strip()
        if not token:
            continue
        if ";" in token:
            tag, _, params = token.partition(";")
            tag = tag.strip()
            q = 1.0
            for param in params.split(";"):
                param = param.strip()
                if param.startswith("q="):
                    try:
                        q = float(param[2:])
                    except ValueError:
                        q = 0.0
            candidates.append((q, tag))
        else:
            candidates.append((1.0, token))
    # Sort stable by descending weight; equal weights keep header order.
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _q, tag in candidates:
        norm = _normalise(tag)
        if norm is not None:
            return norm
    return DEFAULT_LANGUAGE


def current_default() -> str:
    """Resolve the default language from the persisted ``language`` setting.

    Read precedence (matches every other setting in ntasker):

    1. ENV ``NTASKER_LANGUAGE`` (handled inside ``get_setting``).
    2. DB row.
    3. ``"auto"`` - then resolved against ``Accept-Language`` upstream;
       here, with no header context, we just return :data:`DEFAULT_LANGUAGE`.

    Local import of :mod:`ntasker.settings` avoids the circular dependency
    (``settings`` imports ``i18n`` for hint translations).
    """
    from ntasker.settings import get_language_setting  # noqa: PLC0415

    raw = get_language_setting()
    if raw in AVAILABLE_LANGUAGES:
        return raw
    # ``auto`` or anything else: no header here -> default.
    return DEFAULT_LANGUAGE


def resolve_for_cli() -> str:
    """Pick the active language for a CLI invocation.

    Precedence: persisted setting > ``LANG`` / ``LC_MESSAGES`` env > English.
    The setting is honoured even at value ``auto``, which in CLI context
    falls through to the env vars (no HTTP header here).
    """
    from ntasker.settings import get_language_setting  # noqa: PLC0415

    raw = get_language_setting()
    if raw in AVAILABLE_LANGUAGES:
        return raw
    for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
        norm = _normalise(os.environ.get(env, ""))
        if norm is not None:
            return norm
    return DEFAULT_LANGUAGE


# ---------------------------------------------------------------------------
# Public translation API
# ---------------------------------------------------------------------------


def _(msgid: str) -> str:  # noqa: N802 -- single-underscore is the gettext convention
    """Translate ``msgid`` in the active language. Identity if no catalog matches."""
    return _manager.translate(msgid, get_active_language())


class LazyString:
    """Lazy translation wrapper.

    Use for module-level defaults that must be evaluated against the
    *call-time* active language, not the (English) language at import.
    Renders to ``str`` on demand via ``__str__``.
    """

    __slots__ = ("_msgid",)

    def __init__(self, msgid: str) -> None:
        self._msgid = msgid

    def __str__(self) -> str:
        return _(self._msgid)

    def __repr__(self) -> str:
        return f"LazyString({self._msgid!r})"

    # Make ``LazyString`` interpolate naturally inside f-strings and
    # ``str.format``.
    def __format__(self, format_spec: str) -> str:
        return format(str(self), format_spec)


def _lazy(msgid: str) -> LazyString:  # noqa: N802 -- mirrors `_`
    """Return a :class:`LazyString` for ``msgid``."""
    return LazyString(msgid)


def N_(msgid: str) -> str:  # noqa: N802 -- gettext convention
    """No-op translation marker: returns ``msgid`` unchanged.

    Use for module-level constants whose strings must be picked up by
    ``pybabel extract`` even though they are not yet translated at the
    call site (e.g. choice tables that get translated later via
    :func:`_`). Add ``--keyword=N_`` to the pybabel-extract invocation.
    """
    return msgid


# ---------------------------------------------------------------------------
# Helpers used by the Jinja env / FastAPI context-processor
# ---------------------------------------------------------------------------


def gettext_for_jinja(msgid: str) -> str:
    """Wrapper used by ``Environment.install_gettext_callables`` so the
    binding always resolves through the active context-var (not bound at
    install time).
    """
    return _(msgid)


def ngettext_for_jinja(singular: str, plural: str, n: int) -> str:
    """Naive plural rule (1 vs not-1) -- adequate for English/German default forms.

    Picks the singular for ``n == 1``, else the plural; both pass through
    the regular gettext resolver (so each form can be its own msgid).
    """
    return _(singular) if n == 1 else _(plural)


def available_languages() -> Iterable[str]:
    """Return the iterable of catalog languages we ship."""
    return AVAILABLE_LANGUAGES
