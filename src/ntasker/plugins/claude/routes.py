"""HTTP routes of the Claude plugin.

``GET /api/claude/usage`` feeds the topbar limits widget. Mounted with a
``require_enabled`` dependency, so it 404s while the plugin is off.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ntasker.plugins.claude import usage

router = APIRouter()


@router.get("/api/claude/usage")
def get_usage(fresh: bool = False) -> JSONResponse:
    """``{"usage": {five_hour, seven_day} | null}`` -- ``null`` without a subscription login.

    ``fresh=1`` asks past the usual cache window (the topbar sends it the
    moment a Claude session starts or ends), rate-limited server-side.
    """
    return JSONResponse({"usage": usage.snapshot(force=fresh)})
