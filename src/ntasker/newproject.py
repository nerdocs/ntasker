"""Create a new project straight from ntasker.

A project in ntasker is only a name -- derived from tasks and from Claude
Code's session directories (see :mod:`ntasker.projects`). "Create project"
realises one on disk: a directory ``<projects_base>/<folder>/<name>``, an
optional ``git init``, and a sidebar group in the ``project_groups`` setting.

A freshly created directory has neither a task nor a Claude session yet, so
the name is also recorded in the ``created_projects`` table -- that keeps it
in the sidebar feed until the first task or session carries it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ntasker.claude_runner import projects_base_dir
from ntasker.db import get_conn
from ntasker.i18n import _
from ntasker.settings import get_setting, set_setting

router = APIRouter()

_GIT_TIMEOUT = 15


def _clean_segment(segment: str) -> str | None:
    """A single path component that is safe to create, or ``None``.

    Rejects empty parts, ``.``/``..``, dot-dirs and path separators -- the
    project must land *inside* the base, never beside or above it.
    """
    s = segment.strip()
    if not s or s in {".", ".."} or s.startswith(".") or "/" in s or "\\" in s or "\0" in s:
        return None
    return s


def _clean_folder(folder: str) -> list[str] | None:
    """Split a ``/``-separated sub-folder into safe components, or ``None``."""
    parts = [p for p in folder.strip().strip("/").split("/")]
    cleaned = [_clean_segment(p) for p in parts]
    if not cleaned or any(c is None for c in cleaned):
        return None
    return [c for c in cleaned if c is not None]


def list_folders(base: Path) -> list[str]:
    """Top-level sub-folders of ``base`` a new project may go into.

    Hidden (``.``) and internal (``_``) folders are left out.
    """
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    return [e.name for e in entries if e.is_dir() and not e.name.startswith((".", "_"))]


def _group_map() -> dict[str, str]:
    raw = get_setting("project_groups")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def created_projects() -> set[str]:
    """Names recorded by :func:`create_project` whose directory still exists."""
    with get_conn() as conn:
        rows = conn.execute("SELECT project, path FROM created_projects").fetchall()
    return {r["project"] for r in rows if os.path.isdir(r["path"])}


def _git_init(target: Path) -> str | None:
    """Run ``git init`` in ``target``. Returns an error text, or ``None``."""
    git = shutil.which("git")
    if git is None:
        return _("git is not installed")
    try:
        res = subprocess.run(
            [git, "init", "-q"],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if res.returncode != 0:
        return (res.stderr or res.stdout).strip() or f"git init exited {res.returncode}"
    return None


class ProjectCreate(BaseModel):
    name: str
    folder: str
    group: str
    git_init: bool = True


@router.get("/api/projects/folders")
def api_project_folders() -> JSONResponse:
    """Feed for the "New project" dialog: base, its sub-folders, known groups."""
    base = projects_base_dir()
    groups = sorted({g for g in _group_map().values() if g}, key=str.casefold)
    return JSONResponse(
        {
            "base": str(base) if base else None,
            "folders": list_folders(base) if base and base.is_dir() else [],
            "groups": groups,
        }
    )


@router.post("/api/projects/create", status_code=201)
def api_create_project(payload: ProjectCreate) -> JSONResponse:
    """Create ``<projects_base>/<folder>/<name>`` and register it as a project.

    400 without a ``projects_base``, on an unsafe name/folder or an empty
    group; 409 when the directory already exists. A failing ``git init`` does
    not undo the directory -- it comes back as ``git_error``.
    """
    base = projects_base_dir()
    if base is None:
        raise HTTPException(status_code=400, detail=_("No projects directory configured"))
    name = _clean_segment(payload.name)
    if name is None:
        raise HTTPException(status_code=400, detail=_("Invalid project name"))
    folder = _clean_folder(payload.folder)
    if folder is None:
        raise HTTPException(status_code=400, detail=_("Invalid folder"))
    group = payload.group.strip()
    if not group:
        raise HTTPException(status_code=400, detail=_("Please choose a group"))

    target = base.joinpath(*folder, name)
    project = "/".join([*folder, name])
    if target.exists():
        raise HTTPException(
            status_code=409, detail=_("{path} already exists").format(path=str(target))
        )
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    git_error = _git_init(target) if payload.git_init else None

    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO created_projects (project, path) VALUES (?, ?)",
            (project, str(target)),
        )
    groups = _group_map()
    groups[project] = group
    set_setting("project_groups", json.dumps(groups, ensure_ascii=False))

    return JSONResponse(
        {
            "project": project,
            "path": str(target),
            "group": group,
            "git": payload.git_init and git_error is None,
            "git_error": git_error,
        },
        status_code=201,
    )
