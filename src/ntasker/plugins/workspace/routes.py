"""HTTP routes of the workspace plugin: the ``/workspace`` page and ``/api/workspace/*``.

Reads and writes are confined to :func:`~ntasker.plugins.workspace.scan.configured_roots`
-- the directories the user explicitly configured. Mutating endpoints are
covered by :class:`ntasker.middleware.OriginGuardMiddleware` (Origin must
match the host); every route 404s while the plugin is disabled.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ntasker.plugins.workspace import scan
from ntasker.settings import get_setting

router = APIRouter()

#: Maps a :class:`~ntasker.plugins.workspace.scan.WriteError` reason to HTTP.
_WRITE_STATUS = {"forbidden": 403, "not_found": 404, "exists": 409, "too_large": 413, "invalid": 400}
_PREVIEW_STATUS = {"forbidden": 403, "not_found": 404, "too_large": 413}


def _write_guard(exc: scan.WriteError) -> HTTPException:
    return HTTPException(status_code=_WRITE_STATUS.get(exc.reason, 400), detail=str(exc))


def _preview_guard(exc: scan.PreviewError) -> HTTPException:
    return HTTPException(status_code=_PREVIEW_STATUS.get(exc.reason, 400), detail=str(exc))


class WorkspaceWrite(BaseModel):
    """Full new content for an existing text file."""

    path: str
    text: str = ""


class WorkspaceCreate(BaseModel):
    """A new file or directory inside ``parent``."""

    parent: str
    name: str
    directory: bool = False


class WorkspaceRename(BaseModel):
    """A new name for an entry, staying in its current directory."""

    path: str
    name: str


class WorkspacePath(BaseModel):
    """A single target path (delete / reveal)."""

    path: str


@router.get("/workspace", response_class=HTMLResponse)
def workspace_page(request: Request) -> HTMLResponse:
    """Workspace page: skills, knowledge base, team, documents, tooling."""
    # Lazy: the page renderer lives in ntasker.app, which imports this
    # plugin at load -- importing it at module level would be a cycle.
    from ntasker import __version__ as VERSION  # noqa: PLC0415
    from ntasker.app import LINKS, _page_plugins, build_js_strings, templates  # noqa: PLC0415
    from ntasker.i18n import get_active_language  # noqa: PLC0415

    response = templates.TemplateResponse(
        request,
        "workspace/templates/workspace.html",
        context={
            "version": VERSION,
            "language": get_active_language(),
            "js_strings": build_js_strings(),
            "links": LINKS,
            **_page_plugins(),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/workspace")
def api_workspace() -> JSONResponse:
    """The full workspace inventory (see :func:`scan.collect`).

    Unconfigured or missing directories yield empty sections with
    ``configured`` / ``exists`` flags rather than an error -- having none of
    them set up is the normal state for most installs.
    """
    return JSONResponse(
        scan.collect(
            skills_dir=get_setting("workspace_skills_dir"),
            wiki_dir=get_setting("workspace_wiki_dir"),
            team_dir=get_setting("workspace_team_dir"),
            docs_dir=get_setting("workspace_docs_dir"),
        )
    )


@router.get("/api/workspace/file")
def api_workspace_file(path: str = Query(..., description="Absolute file path")) -> JSONResponse:
    """One file's content for the previewer, confined to the configured roots."""
    try:
        return JSONResponse(scan.read_file(path, scan.configured_roots()))
    except scan.PreviewError as exc:
        raise _preview_guard(exc) from exc


@router.get("/api/workspace/browse")
def api_workspace_browse(path: str = Query(..., description="Absolute directory path")) -> JSONResponse:
    """List one directory inside the configured roots."""
    try:
        return JSONResponse(scan.browse(path, scan.configured_roots()))
    except scan.PreviewError as exc:
        raise _preview_guard(exc) from exc


@router.put("/api/workspace/file")
def api_workspace_write(payload: WorkspaceWrite) -> JSONResponse:
    """Overwrite one text file inside the configured roots."""
    try:
        return JSONResponse(scan.write_file(payload.path, payload.text, scan.configured_roots()))
    except scan.WriteError as exc:
        raise _write_guard(exc) from exc
    except scan.PreviewError as exc:
        # write_file re-reads the file to hand the fresh content back; the
        # only way to land here is the file vanishing between the two steps.
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/workspace/entry", status_code=201)
def api_workspace_create(payload: WorkspaceCreate) -> JSONResponse:
    """Create an empty note (or a folder) inside a workspace directory."""
    try:
        return JSONResponse(
            scan.create_entry(payload.parent, payload.name, scan.configured_roots(), payload.directory),
            status_code=201,
        )
    except scan.WriteError as exc:
        raise _write_guard(exc) from exc


@router.post("/api/workspace/rename")
def api_workspace_rename(payload: WorkspaceRename) -> JSONResponse:
    """Rename a file or folder in place."""
    try:
        return JSONResponse(scan.rename_entry(payload.path, payload.name, scan.configured_roots()))
    except scan.WriteError as exc:
        raise _write_guard(exc) from exc


@router.post("/api/workspace/delete")
def api_workspace_delete(payload: WorkspacePath) -> JSONResponse:
    """Move a file or folder to the trash.

    POST rather than DELETE so the target path travels in the body: paths
    here routinely contain spaces, umlauts and ``#``, and a body sidesteps
    every layer of URL escaping between the browser and the router.
    """
    try:
        return JSONResponse(scan.delete_entry(payload.path, scan.configured_roots()))
    except scan.WriteError as exc:
        raise _write_guard(exc) from exc


@router.post("/api/workspace/reveal")
def api_workspace_reveal(payload: WorkspacePath) -> JSONResponse:
    """Hand a file to the desktop's default application."""
    try:
        return JSONResponse(scan.reveal(payload.path, scan.configured_roots()))
    except scan.WriteError as exc:
        raise _write_guard(exc) from exc


