"""Per-request middleware for ntasker.

Currently the only middleware is :class:`LanguageMiddleware`, which sets
the active i18n language for the request based on the persisted
``language`` setting (and the request's ``Accept-Language`` header when
the setting is ``auto``).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from ntasker.i18n import (
    AVAILABLE_LANGUAGES,
    DEFAULT_LANGUAGE,
    reset_active_language,
    resolve_from_header,
    set_active_language,
)


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
