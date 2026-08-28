"""Per-request middleware for ntasker.

Two middlewares:

* :class:`OriginGuardMiddleware` -- rejects cross-origin state-changing
  requests and cross-origin WebSocket upgrades. ntasker has no auth layer,
  so the browser's ambient authority *is* the only thing an attacker needs.
* :class:`LanguageMiddleware` -- sets the active i18n language for the
  request based on the persisted ``language`` setting (and the request's
  ``Accept-Language`` header when the setting is ``auto``).
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ntasker.i18n import (
    AVAILABLE_LANGUAGES,
    DEFAULT_LANGUAGE,
    reset_active_language,
    resolve_from_header,
    set_active_language,
)

#: Methods that change state and therefore need CSRF protection.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Host names that can never be pointed elsewhere by a DNS answer. A rebinding
#: attack needs a *name* it controls; an IP literal cannot be rebound.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: Comma-separated extra host names, for the ``ntasker serve --host <other>``
#: case. Set automatically by the CLI; see :func:`ntasker.cli.serve`.
ALLOWED_HOSTS_ENV = "NTASKER_ALLOWED_HOSTS"


def _allowed_hosts() -> frozenset[str]:
    """Loopback names plus whatever ``NTASKER_ALLOWED_HOSTS`` adds."""
    extra = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return LOOPBACK_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


def _hostname(netloc: str) -> str:
    """Strip the port off a ``host[:port]`` value, IPv6-literal aware."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):  # [::1]:8766 -- the colon inside must survive
        return netloc.partition("]")[0] + "]"
    return netloc.partition(":")[0]


def reject_reason(headers: Headers, method: str | None) -> str | None:
    """Return why this request must be refused, or ``None`` if it may pass.

    Two independent checks:

    1. **Host** -- the ``Host`` header must name a loopback address (or an
       explicitly allowed host). Blocks DNS rebinding, where a page on
       ``http://evil.example`` resolves to 127.0.0.1 and then talks to us
       *same-origin*, which no ``Origin`` check would catch.
    2. **Origin** -- for unsafe methods and WebSocket upgrades, a present
       ``Origin`` must match the ``Host`` exactly. A missing ``Origin`` means
       a non-browser client (the CLI uses ``urllib``, curl, scripts); those
       carry no ambient authority and are allowed through.
    """
    host = headers.get("host", "")
    if _hostname(host) not in _allowed_hosts():
        return "host_not_allowed"

    # Safe methods only need the rebinding check above.
    if method is not None and method.upper() not in UNSAFE_METHODS:
        return None

    origin = headers.get("origin")
    if origin is None:
        return None  # non-browser client
    if urlsplit(origin).netloc.lower() != host.strip().lower():
        return "cross_origin"
    return None


class OriginGuardMiddleware:
    """Refuse cross-origin writes and cross-origin WebSocket upgrades.

    A plain ASGI middleware on purpose:
    :class:`~starlette.middleware.base.BaseHTTPMiddleware` only ever sees
    ``scope["type"] == "http"``, so it cannot guard the WebSocket route --
    which is the most dangerous endpoint here, because WebSocket upgrades
    are exempt from the same-origin policy and hand out an interactive
    shell.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        reason = reject_reason(headers, scope.get("method"))
        if reason is None:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # The handshake must be consumed before it can be refused.
            await receive()
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
            return

        body = json.dumps({"detail": f"Refused: {reason}"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class LanguageMiddleware(BaseHTTPMiddleware):
    """Resolve and pin the active language for each request.

    Resolution order:

    1. Persisted ``language`` setting (``en`` / ``de``) - explicit pin.
    2. ``language`` setting == ``auto`` (or unset) - parse the request's
       ``Accept-Language`` header and match against available catalogs.
    3. Fallback :data:`DEFAULT_LANGUAGE` (English).

    The language is stored in :data:`ntasker.i18n._active_language` (a
    :class:`contextvars.ContextVar`) for the duration of the request
    coroutine and reset on response.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Local import: avoid an import cycle at module load. The settings
        # module imports `_` from `i18n`, so importing `settings` at the
        # top of this module would create a chain at app boot.
        from ntasker.settings import get_language_setting  # noqa: PLC0415

        setting = get_language_setting()
        if setting in AVAILABLE_LANGUAGES:
            lang = setting
        else:
            # ``auto`` (default), unset, or an unrecognised value.
            lang = resolve_from_header(request.headers.get("accept-language"))
            if lang not in AVAILABLE_LANGUAGES:
                lang = DEFAULT_LANGUAGE

        token = set_active_language(lang)
        try:
            response: Response = await call_next(request)
        finally:
            reset_active_language(token)
        # Echo the resolved language so clients (and tests) can see what
        # the server picked. Harmless info; not a spec'd header.
        response.headers["Content-Language"] = lang
        return response
