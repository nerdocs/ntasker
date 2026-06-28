"""PyPI update check.

Queries the PyPI JSON API for the latest released ``ntasker`` version and
compares it against the running :data:`ntasker.__version__`. The result is
cached in memory for :data:`_TTL` seconds, so a long-running (production)
server hits PyPI at most once a day, while a freshly (re)started server --
e.g. a ``ntasker serve --reload`` dev server -- re-checks on every boot
because the in-memory cache starts empty.

Network failures are non-fatal: offline simply means "no update info"
(``latest`` stays ``None`` and ``error`` carries the reason).
"""

from __future__ import annotations

import threading
import time

import httpx

from ntasker import __version__

PYPI_URL = "https://pypi.org/pypi/ntasker/json"
_TTL = 24 * 60 * 60  # re-check at most once a day on a long-running server
_TIMEOUT = 4.0  # keep a slow/offline PyPI from stalling the caller

_lock = threading.Lock()
_cache: dict | None = None
_checked_at: float = 0.0


def _parse(v: str) -> tuple[int, ...]:
    """Best-effort version parse: leading dotted integer components.

    ``"2.11.0"`` -> ``(2, 11, 0)``; a trailing pre-release/local suffix
    (``"2.12.0rc1"``) truncates the parse at the first non-integer chunk.
    Good enough for the simple ``x.y.z`` SemVer scheme ntasker ships under.
    """
    parts: list[int] = []
    for chunk in v.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    return _parse(latest) > _parse(current)


def _fetch() -> dict:
    """Hit PyPI once and build the result dict. Never raises."""
    result: dict = {
        "current": __version__,
        "latest": None,
        "update_available": False,
        "error": None,
    }
    try:
        resp = httpx.get(
            PYPI_URL, timeout=_TIMEOUT, headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        latest = resp.json()["info"]["version"]
        result["latest"] = latest
        result["update_available"] = _is_newer(latest, __version__)
    except Exception as exc:  # noqa: BLE001 -- offline / parse errors stay non-fatal
        result["error"] = str(exc)
    return result


def check(force: bool = False) -> dict:
    """Return cached update info, refreshing from PyPI when stale.

    Thread-safe. With ``force=True`` the TTL is ignored and PyPI is queried
    unconditionally. The returned dict is a copy, safe for the caller to
    mutate or serialise.
    """
    global _cache, _checked_at
    with _lock:
        fresh = _cache is not None and (time.time() - _checked_at) < _TTL
        if fresh and not force:
            return dict(_cache)
        _cache = _fetch()
        _checked_at = time.time()
        return dict(_cache)
