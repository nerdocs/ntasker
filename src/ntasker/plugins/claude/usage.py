"""Usage limits of the claude.ai subscription behind the local Claude Code login.

Claude Code stores its OAuth token in ``<claude home>/.credentials.json``
(``claudeAiOauth.accessToken``). With that token the same endpoint the
``/usage`` slash command reads -- ``/api/oauth/usage`` -- answers the
utilisation of the 5-hour and the weekly window. No token (API-key login,
never logged in, macOS keychain storage) means no subscription to show:
:func:`snapshot` returns ``None`` and the topbar widget stays hidden.

One in-process cache for :data:`CACHE_TTL` seconds keeps several tabs
from hammering the endpoint; a failed refresh serves the stale copy. A
forced refresh (the topbar right after a Claude session started or ended)
bypasses that TTL but still honours :data:`MIN_INTERVAL`, so no number of
tabs can turn session churn into a request flood.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from ntasker.agents import AGENTS, resolve_home

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_TTL = 30.0
MIN_INTERVAL = 5.0
TIMEOUT = 5.0

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "usage": None}


def _token() -> str | None:
    """The unexpired OAuth access token of the local Claude Code login, or ``None``."""
    path = resolve_home(AGENTS["claude"]) / ".credentials.json"
    try:
        oauth = json.loads(path.read_text()).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        return None
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 < time.time():
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _request(token: str) -> dict[str, Any]:
    """One ``GET /api/oauth/usage``; raises on any transport or decode error."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "ntasker",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _window(block: Any) -> dict[str, Any] | None:
    """``{utilization, resets_at}`` of one limit window, or ``None`` when absent."""
    if not isinstance(block, dict) or block.get("utilization") is None:
        return None
    return {"utilization": float(block["utilization"]), "resets_at": block.get("resets_at")}


def snapshot(force: bool = False) -> dict[str, Any] | None:
    """Current ``{five_hour, seven_day}`` utilisation, or ``None`` without a subscription.

    ``force`` shortens the cache window to :data:`MIN_INTERVAL` -- for the
    moments where the numbers just moved (a session started or ended) and the
    usual :data:`CACHE_TTL` would serve a figure from before that.
    """
    with _lock:
        if time.time() - _cache["at"] < (MIN_INTERVAL if force else CACHE_TTL):
            return _cache["usage"]
        token = _token()
        if token is None:
            _cache.update(at=time.time(), usage=None)
            return None
        try:
            payload = _request(token)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return _cache["usage"]  # stale beats nothing
        usage = {"five_hour": _window(payload.get("five_hour")), "seven_day": _window(payload.get("seven_day"))}
        if usage["five_hour"] is None and usage["seven_day"] is None:
            usage = None  # a login without subscription limits (console account)
        _cache.update(at=time.time(), usage=usage)
        return usage
