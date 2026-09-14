"""HTTP routes of the task-context plugin.

Attachments (``/api/tasks/{id}/context``), the native file picker
(``/api/fs/pick``), path normalisation (``/api/fs/resolve``) and the MCP
server list for the picker (``/api/context/mcp``). Mounted by
:mod:`ntasker.plugins` with a ``require_enabled`` dependency, so every
route 404s while the plugin is switched off.

Security boundary: an attachment of kind ``file`` may point anywhere on
this machine -- that is what it is for -- so the write endpoints rely on
:class:`ntasker.middleware.OriginGuardMiddleware` (Origin must match the
host) to keep a foreign page from planting one. The other kinds are
confined to the workspace roots (``workspace_*_dir`` settings).
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid
from pathlib import Path

import platformdirs

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ntasker.db import get_conn
from ntasker.i18n import _
from ntasker.plugins.task_context.db import (
    CONTEXT_KINDS,
    MCP_SCHEME,
    add_context,
    load_context_for,
    remove_context,
)
from ntasker.plugins.workspace import scan

router = APIRouter()


class ContextAdd(BaseModel):
    """One entry to attach to a task."""

    kind: str
    path: str
    label: str = ""
    note: str = ""


class PickRequest(BaseModel):
    """Options for the native file dialog."""

    folder: bool = False


class UploadIn(BaseModel):
    """An image pasted into a description: file name + base64 bytes."""

    name: str
    data: str


# Cap on one pasted image (decoded). A screenshot is a few MB at most.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def uploads_dir() -> Path:
    """Where pasted images live: the user-data dir, so an attachment keeps
    pointing at a file that exists after a reboot (a temp dir would not)."""
    return Path(platformdirs.user_data_dir("nTasker")) / "uploads"


#: Maps a :class:`~ntasker.plugins.workspace.scan.WriteError` reason to HTTP.
_WRITE_STATUS = {"forbidden": 403, "not_found": 404, "exists": 409, "too_large": 413, "invalid": 400}


def _write_guard(exc: scan.WriteError) -> HTTPException:
    return HTTPException(status_code=_WRITE_STATUS.get(exc.reason, 400), detail=str(exc))


def _mcp_server_names() -> set[str]:
    """Names of the MCP servers currently declared in ``~/.claude.json``."""
    return {s["name"] for s in scan.scan_tooling().get("servers", [])}


def resolve_context_add(payload: ContextAdd) -> tuple[str, str, str, str]:
    """Validate one attachment request; return ``(kind, path, label, note)``.

    Workspace kinds are confined to the configured roots -- without that
    check any caller could seed a task with a pointer to ``~/.ssh/id_rsa``
    and have the agent briefing read it out at the next run. Shared by the
    attach endpoint and task creation so the two can never drift apart.
    """
    if payload.kind not in CONTEXT_KINDS:
        raise HTTPException(
            status_code=400, detail=_("Unknown context kind: {kind}").format(kind=payload.kind)
        )

    if payload.kind == "mcp":
        # An MCP server is referenced by the name of its entry in
        # ~/.claude.json; it must exist there now, otherwise the agent
        # would be told to use tools it cannot have.
        raw = payload.path.strip()
        name = (raw[len(MCP_SCHEME) :] if raw.startswith(MCP_SCHEME) else raw).strip()
        if not name or name not in _mcp_server_names():
            raise HTTPException(
                status_code=404,
                detail=_("No MCP server named {name} in ~/.claude.json.").format(name=name),
            )
        return payload.kind, f"{MCP_SCHEME}{name}", payload.label.strip() or name, payload.note.strip()
    if payload.path.strip().startswith(MCP_SCHEME):
        raise HTTPException(status_code=400, detail=_("An mcp:// path needs kind 'mcp'."))

    if not payload.path.strip():
        raise HTTPException(status_code=400, detail=_("No such file or directory."))
    try:
        target = Path(os.path.expandvars(os.path.expanduser(payload.path.strip()))).resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.kind == "file":
        # Any file or folder on this machine, named by the user explicitly
        # -- the one kind that is *not* confined to the workspace roots,
        # because "the spreadsheet on my Desktop" is exactly what it is for.
        # The full name (with suffix) is the honest label: "report.pdf" and
        # "report.xlsx" side by side must stay tellable apart.
        if not target.exists():
            raise HTTPException(status_code=404, detail=_("No such file or directory."))
        label = payload.label.strip() or target.name or str(target)
        return payload.kind, str(target), label, payload.note.strip()

    roots = scan.configured_roots()
    if not roots or not scan.within_roots(target, roots):
        raise HTTPException(
            status_code=403, detail=_("Path lies outside every configured workspace directory.")
        )
    if not target.exists():
        raise HTTPException(status_code=404, detail=_("No such file or directory."))
    # An empty label is the common case (the UI attaches straight from a
    # list where the file name *is* the label) -- derive it rather than
    # storing a blank the frontend would have to paper over.
    return payload.kind, str(target), payload.label.strip() or target.stem, payload.note.strip()


def _require_task(conn, task_id: int) -> None:
    """404 unless the task exists."""
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=_("Task not found"))


def _entry(conn, task_id: int, context_id: int) -> dict:
    _require_task(conn, task_id)
    entry = next((c for c in load_context_for(conn, task_id) if c["id"] == context_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=_("Attachment not found"))
    return entry


@router.get("/api/tasks/{task_id}/context")
def api_list_context(task_id: int) -> JSONResponse:
    """The entries attached to one task."""
    with get_conn() as conn:
        _require_task(conn, task_id)
        return JSONResponse(load_context_for(conn, task_id))


@router.post("/api/tasks/{task_id}/context", status_code=201)
def api_add_context(task_id: int, payload: ContextAdd) -> JSONResponse:
    """Attach an entry to a task (see :func:`resolve_context_add`)."""
    kind, path, label, note = resolve_context_add(payload)
    with get_conn() as conn:
        _require_task(conn, task_id)
        entry = add_context(conn, task_id, kind, path, label, note)
    return JSONResponse(entry, status_code=201)


@router.delete("/api/tasks/{task_id}/context/{context_id}", status_code=204)
def api_remove_context(task_id: int, context_id: int) -> None:
    """Detach an entry. The file itself is never touched."""
    with get_conn() as conn:
        _require_task(conn, task_id)
        if not remove_context(conn, task_id, context_id):
            raise HTTPException(status_code=404, detail=_("Attachment not found"))


@router.get("/api/tasks/{task_id}/context/{context_id}/file")
def api_context_file(task_id: int, context_id: int) -> JSONResponse:
    """Preview payload for one attachment's file, wherever it lives.

    The authorisation is the attachment row itself -- the user put that
    exact path on this task -- so the preview reads what the row points
    at and nothing else (no ``path`` parameter to steer).
    """
    with get_conn() as conn:
        entry = _entry(conn, task_id, context_id)
    if entry["remote"]:
        raise HTTPException(status_code=400, detail=_("This attachment is not a file."))
    try:
        return JSONResponse(scan.describe_file(Path(entry["path"])))
    except scan.PreviewError as exc:
        status = {"forbidden": 403, "not_found": 404, "too_large": 413}.get(exc.reason, 400)
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/api/tasks/{task_id}/context/{context_id}/reveal")
def api_context_reveal(task_id: int, context_id: int) -> JSONResponse:
    """Open one attachment's file in the desktop's default application."""
    with get_conn() as conn:
        entry = _entry(conn, task_id, context_id)
    if entry["remote"]:
        raise HTTPException(status_code=400, detail=_("This attachment is not a file."))
    try:
        return JSONResponse(scan.open_with_desktop(Path(entry["path"])))
    except scan.WriteError as exc:
        raise _write_guard(exc) from exc


@router.post("/api/context/upload", status_code=201)
def api_context_upload(payload: UploadIn) -> JSONResponse:
    """Store an image pasted into a task description; returns its path.

    The frontend then attaches that path as a ``file`` context entry -- the
    same as picking the file by hand, so the agent reads it at the start of
    the run. The name is reduced to a basename so a crafted value cannot
    escape the uploads dir.
    """
    try:
        blob = base64.b64decode(payload.data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail=_("Invalid image data.")) from exc
    if not blob or len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_("Image is too large (max 25 MB)."))
    base = os.path.basename(payload.name).lstrip(".") or "image.png"
    target = uploads_dir() / f"{uuid.uuid4().hex[:8]}-{base}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    return JSONResponse({"path": str(target), "name": target.name}, status_code=201)


@router.get("/api/context/mcp")
def api_context_mcp() -> JSONResponse:
    """MCP servers from ``~/.claude.json`` for the picker's MCP tab (no secrets)."""
    servers = scan.scan_tooling().get("servers", [])
    return JSONResponse(
        [{"name": s["name"], "transport": s["transport"], "runtime_ok": s["runtime_ok"]} for s in servers]
    )


@router.get("/api/fs/pick")
def api_fs_pick_available() -> JSONResponse:
    """Whether this machine can show a native file dialog (see POST)."""
    return JSONResponse({"available": scan.picker_available()})


@router.post("/api/fs/pick")
def api_fs_pick(payload: PickRequest) -> JSONResponse:
    """Open the OS file dialog on this desktop and return the chosen paths.

    Works because server and browser share a desktop: the page cannot learn
    a dropped file's path, but the local process can ask the OS. Blocks
    until the user picks or cancels (cancel = empty list). 501 when no
    dialog is available on this platform.
    """
    if not scan.picker_available():
        raise HTTPException(status_code=501, detail=_("No native file dialog is available here."))
    prompt = _("Choose a folder to attach") if payload.folder else _("Choose files to attach")
    try:
        paths = scan.pick_paths(folder=payload.folder, prompt=prompt)
    except scan.WriteError as exc:
        raise _write_guard(exc) from exc
    return JSONResponse({"paths": paths})


@router.get("/api/fs/resolve")
def api_fs_resolve(
    path: str = Query(..., description="A path as typed or pasted by the user"),
) -> JSONResponse:
    """Normalise a user-typed path and say whether it exists.

    Lets the picker validate a pasted path before it lands in a draft task
    (where nothing is sent to the server until Create). Only existence and
    the resolved form are reported -- never contents.
    """
    raw = path.strip()
    if not raw:
        raise HTTPException(status_code=400, detail=_("No such file or directory."))
    try:
        target = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.exists():
        raise HTTPException(status_code=404, detail=_("No such file or directory."))
    return JSONResponse(
        {"path": str(target), "name": target.name or str(target), "is_dir": target.is_dir(), "exists": True}
    )
