"""FastAPI application for ntasker.

Submodule of the :mod:`ntasker` package; the CLI entry ``ntasker serve``
runs this app via uvicorn (see :mod:`ntasker.cli`). Static files and
templates are loaded via :func:`importlib.resources.files` so the package
works equally well from a wheel install and a local checkout.

Bind is the CLI's responsibility -- this module only exposes ``app``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from importlib.resources import files
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ntasker import __version__ as VERSION
from ntasker.assets import (
    assets_dir,
    get_asset_url,
    get_sri,
)
from ntasker.claude_assets import resolve_claude_home, scan_status
from ntasker import db as _db_module
from ntasker.db import (
    get_conn,
    init_db,
    load_tags_bulk,
    load_tags_for,
    normalize_tags,
    row_to_task,
    set_db_path,
    set_task_tags,
)
from ntasker.i18n import (
    N_,
    _,
    get_active_language,
    gettext_for_jinja,
    ngettext_for_jinja,
)
from ntasker.middleware import LanguageMiddleware
from ntasker.settings import (
    HINTS,
    VALIDATORS,
    delete_setting,
    ensure_settings_table,
    get_assets_mode_resolved,
    get_projects_dir,
    get_setting_raw,
    list_settings,
    set_setting,
)

# Sentinel for "tasks without a project" (cross-project tasks). Used in
# multi-value project filters: ?project=__none__ -> include rows with project IS NULL.
PROJECT_NONE_SENTINEL = "__none__"
# Legacy single-value sentinel kept for backwards compatibility with old bookmarks.
PROJECT_NULL_LEGACY = "__null__"

# Sentinel for "tasks without a phase" (analogous to PROJECT_NONE_SENTINEL).
PHASE_NONE_SENTINEL = "__none__"

# Fixed phase order + English source labels for the sidebar feed.
# Labels go through ``_()`` at response time -- ``N_`` is a no-op marker
# so pybabel-extract picks up the strings; translations live in
# ``locale/<lang>/LC_MESSAGES/``.
PHASE_ORDER: list[tuple[str, str]] = [
    ("wip", N_("In Progress")),
    ("planned", N_("Planned")),
    ("later", N_("Later")),
    (PHASE_NONE_SENTINEL, N_("No phase")),
]
PHASE_VALID = {value for value, _label in PHASE_ORDER}

# Fixed priority order for the sidebar feed (highest first).
PRIORITY_ORDER: list[tuple[str, str]] = [
    ("critical", N_("Critical")),
    ("high", N_("High")),
    ("normal", N_("Normal")),
    ("low", N_("Low")),
]
PRIORITY_VALID = {value for value, _label in PRIORITY_ORDER}
PRIORITY_DEFAULT = "normal"


# ---------------------------------------------------------------------------
# Resource paths -- via importlib.resources so this works from a wheel install
# ---------------------------------------------------------------------------

_PKG_ROOT = files("ntasker")
TEMPLATES_DIR = _PKG_ROOT / "templates"
STATIC_DIR = _PKG_ROOT / "static"


# ---------------------------------------------------------------------------
# Project discovery (settings-backed)
# ---------------------------------------------------------------------------


def list_projects() -> list[str]:
    """Return live list of project symlink names under the configured ``projects_dir``.

    If ``projects_dir`` is unset the list is empty -- the ``/api/projects``
    endpoint then signals this to the UI via the ``X-Settings-Missing``
    response header so a banner can prompt for configuration.
    """
    projects_dir = get_projects_dir()
    if projects_dir is None:
        return []
    return sorted(
        p.name
        for p in projects_dir.iterdir()
        if p.is_symlink() and not p.name.startswith(".") and not p.name.endswith(".lock")
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


Status = Literal["open", "done"]
Phase = Literal["wip", "planned", "later"]
Priority = Literal["low", "normal", "high", "critical"]


class TaskCreate(BaseModel):
    project: str | None = None
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    phase: Phase | None = None
    # Plain ``str`` so the endpoint can return HTTP 400 (not 422) on bad
    # values via the explicit whitelist check.
    priority: str = "normal"
    tags: list[str] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    project: str | None = None
    title: str | None = Field(None, min_length=1, max_length=500)
    description: str | None = None
    status: Status | None = None
    phase: Phase | None = None
    priority: str | None = None
    archived: bool | None = None
    tags: list[str] | None = None  # None = unchanged; [] = clear all


class SettingUpdate(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ntasker",
    description="Local single-user task tracker.",
    version=VERSION,
    docs_url="/api/docs",
    redoc_url=None,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Wire the Jinja i18n extension. ``newstyle=True`` enables {trans foo=...}
# placeholders. We deliberately bind callables (not pre-resolved strings)
# so each call goes through the active-language context-var.
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(
    gettext=gettext_for_jinja,
    ngettext=ngettext_for_jinja,
    newstyle=True,
)


def _asset(name: str) -> str:
    """Jinja global -- resolve a vendor-asset URL for the active mode."""
    return get_asset_url(name, get_assets_mode_resolved(), version=VERSION)


def _asset_sri(name: str) -> str:
    """Jinja global -- SRI hash string for use in ``integrity="..."``."""
    return get_sri(name, get_assets_mode_resolved())


def _asset_mode() -> str:
    """Jinja global -- expose the resolved mode so templates can decide
    whether to add ``crossorigin="anonymous"`` (required for CDN/SRI)."""
    return get_assets_mode_resolved()


def _t(key: str) -> str:
    """Short Jinja alias for :func:`_` -- ``{{ t('Save') }}`` reads cleaner
    than ``{{ _('Save') }}`` in dense markup, and avoids confusion with
    Jinja's loop-variable convention.
    """
    return _(key)


templates.env.globals["asset"] = _asset
templates.env.globals["asset_sri"] = _asset_sri
templates.env.globals["asset_mode"] = _asset_mode
templates.env.globals["t"] = _t


# ---------------------------------------------------------------------------
# Frontend i18n -- the JS/Alpine layer reads `window.__i18n` populated
# from this dict. Every string consumed by AlpineJS expressions, confirm
# dialogs, dynamic placeholders, or toast messages MUST live here so
# Babel's pybabel-extract picks it up via the surrounding _() call.
# ---------------------------------------------------------------------------


def build_js_strings() -> dict[str, str]:
    """Translate every JS-side string against the active language.

    Centralised here (not split across templates) so:

    * pybabel-extract sees a single Python source for all JS msgids;
    * the Jinja templates only need ``window.__i18n = {{ js_strings | tojson }}``;
    * adding a new key is one line in one place.
    """
    return {
        # Sidebar -- projects
        "projects": _("Projects"),
        "select_all": _("Select all"),
        "deselect_all": _("Deselect all"),
        "no_filter_active": _("No filter active -- all tasks visible."),
        "cross_project": _("Cross-project"),
        "no_project_symlinks": _("No project symlinks found."),
        # Sidebar -- phases
        "phases": _("Phases"),
        "clear_phase_filter": _("Clear phase filter"),
        "tasks_without_project": _("Tasks without a project"),
        # Sidebar -- priorities
        "priority": _("Priority"),
        "clear_priority_filter": _("Clear priority filter"),
        # Sidebar -- tags
        "tags": _("Tags"),
        "clear_tag_filter": _("Clear tag filter"),
        "cleanup_tags": _("Clean up tags"),
        "cleanup_tags_title": _("Remove unused tags"),
        # Top bar
        "settings": _("Settings"),
        "light_mode": _("Light mode"),
        "dark_mode": _("Dark mode"),
        # Page header
        "tasks_title": _("Tasks"),
        # Banners
        "configure_projects_dir": _(
            "Please configure the projects directory -- otherwise the project list stays empty."
        ),
        "go_to_settings": _("Go to settings"),
        # New-task form
        "new_task": _("New task"),
        "project": _("Project"),
        "cross_project_option": _("-- cross-project --"),
        "phase": _("Phase"),
        "phase_none": _("--"),
        "phase_wip": _("In Progress"),
        "phase_planned": _("Planned"),
        "phase_later": _("Later"),
        "priority_critical": _("Critical"),
        "priority_high": _("High"),
        "priority_normal": _("Normal"),
        "priority_low": _("Low"),
        "title": _("Title"),
        "title_placeholder": _("What needs to be done?"),
        "description": _("Description"),
        "description_placeholder": _("Optional"),
        "tag_input_placeholder": _("Type a tag, Enter to add"),
        "remove_tag": _("Remove tag"),
        "create": _("Create"),
        # Search
        "search_placeholder": _("Search in title and description..."),
        # Tabs
        "tab_open": _("Open"),
        "tab_done": _("Done"),
        "tab_archive": _("Archive"),
        # Task row
        "click_to_copy_id": _("Click to copy: #{id}"),
        "filter_project": _("Filter: project {name}"),
        "filter_cross_project": _("Filter: cross-project"),
        "filter_phase_wip": _("Filter: phase In Progress"),
        "filter_phase_planned": _("Filter: phase Planned"),
        "filter_phase_later": _("Filter: phase Later"),
        "filter_priority_critical": _("Filter: priority Critical"),
        "filter_priority_high": _("Filter: priority High"),
        "filter_priority_low": _("Filter: priority Low"),
        "filter_tag": _("Filter: tag {name}"),
        "edit": _("Edit"),
        "archive": _("Archive"),
        "unarchive": _("Restore"),
        "delete_permanently": _("Delete permanently"),
        "delete": _("Delete"),
        # Empty state
        "no_tasks": _("No tasks"),
        "empty_filtered": _("No matches for the active filters."),
        "empty_open": _("All done. Or nothing created yet."),
        "empty_done": _("Nothing finished yet."),
        "empty_archive": _("Archive is empty."),
        # Edit modal
        "edit_task": _("Edit task"),
        "task_n": _("Task"),
        "close": _("Close"),
        "cancel": _("Cancel"),
        "save": _("Save"),
        # Toasts
        "create_failed": _("Create failed."),
        "delete_failed": _("Delete failed."),
        "save_failed": _("Save failed."),
        "update_failed": _("Update failed."),
        "delete_only_archived": _("Only archived tasks can be deleted."),
        "confirm_delete": _('"{title}" -- delete permanently?'),
        "copied": _("Copied: {text}"),
        "copy_failed": _("Copy failed"),
        "cleanup_failed": _("Cleanup failed."),
        "cleanup_none": _("No unused tags."),
        "cleanup_removed": _("{n} unused tags removed: {head}{tail}"),
        "cleanup_more": _(", +{n} more"),
        # Settings page
        "settings_title": _("Settings"),
        "back_to_tasks": _("back to tasks"),
        "known_keys": _("Known keys"),
        "unset_placeholder": _("(not set yet)"),
        "saved": _("{key} saved."),
        "removed": _("{key} removed."),
        "claude_integration": _("Claude Code Integration"),
        "claude_intro": _(
            "ntasker ships a skill (SKILL.md) and a slash command (/task <id>) "
            "for Claude Code. This card shows the install status -- writes go "
            "exclusively through the CLI."
        ),
        "installed": _("Installed"),
        "package_version": _("Package version"),
        "drift": _("Drift"),
        "claude_home": _("Claude home"),
        "yes": _("yes"),
        "no": _("no"),
        "claude_not_installed": _("Skill + slash command are not installed yet."),
        "claude_drift": _(
            "Installed files differ from the package version. Update with backup:"
        ),
        "all_settings": _("All settings (DB content)"),
        "key": _("Key"),
        "value": _("Value"),
        "updated": _("updated"),
        "no_settings": _("No settings configured."),
        "no_settings_hint_prefix": _(
            "Configure a known key above, or set one via CLI:"
        ),
    }

# Mount the user-data vendor cache at ``/static/vendor`` *before* the
# broader ``/static`` mount. Starlette dispatches mounts in registration
# order and the more specific prefix wins -- but only if it is mounted
# first. Skip the mount entirely if no cache exists; templates use the
# CDN URLs in that case (mode=auto resolves to ``cdn``).
_vendor_cache = assets_dir()
if _vendor_cache.is_dir():
    app.mount(
        "/static/vendor",
        StaticFiles(directory=str(_vendor_cache)),
        name="static-vendor",
    )

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Language middleware -- set the active i18n language for each request.
# Must be added after the FastAPI() construction; runs *outermost* in the
# Starlette stack, which is exactly what we want (template rendering and
# every endpoint sees the resolved language).
app.add_middleware(LanguageMiddleware)


@app.on_event("startup")
def on_startup() -> None:
    """Make sure the DB path is bound and the schema is in place. Idempotent.

    Lifespan-safe: when uvicorn runs with ``--reload``, the worker is a
    fresh subprocess that imports ``ntasker.app:app`` directly without
    re-entering :func:`ntasker.cli.cmd_serve` -- so the module-level
    ``DB_PATH`` is unbound. We re-resolve here using the same precedence
    as the CLI (ENV ``NTASKER_DB`` > platformdirs default). The CLI sets
    ``NTASKER_DB`` from ``--db`` before invoking uvicorn so the worker
    inherits the right path even with ``--reload``.

    If ``DB_PATH`` is already bound (in-process import / test harness /
    non-reload CLI path), we keep it -- never overwrite an explicit bind.
    """
    if _db_module.DB_PATH is None:
        # Avoid importing paths at module load time -- keeps ``ntasker
        # --version`` snappy and lets the test harness rebind DB_PATH
        # before any code runs.
        from ntasker.paths import resolve_db_path  # noqa: PLC0415

        set_db_path(resolve_db_path())
    init_db()
    # Belt-and-braces: ensure settings table even on pre-1.0 DBs that
    # have not been run through ``ntasker init`` yet.
    with get_conn() as conn:
        ensure_settings_table(conn)


# ---------------------------------------------------------------------------
# Routes -- HTML
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Main UI page. ``Cache-Control: no-store`` invalidates the shell on
    every request; static assets carry ``?v=<VERSION>`` cache-busters.
    """
    response = templates.TemplateResponse(
        request,
        "index.html",
        context={
            "version": VERSION,
            "language": get_active_language(),
            "js_strings": build_js_strings(),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Settings UI: list known + ad-hoc keys, edit/delete via JS fetch."""
    # Hints are stored as ``LazyString`` -- coerce to ``str`` here so the
    # template gets a plain mapping with already-translated values for
    # the active language.
    hints_text = {key: str(val) for key, val in HINTS.items()}
    response = templates.TemplateResponse(
        request,
        "settings.html",
        context={
            "version": VERSION,
            "hints": hints_text,
            "known_keys": sorted(VALIDATORS.keys()),
            "language": get_active_language(),
            "js_strings": build_js_strings(),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Routes -- API: settings
# ---------------------------------------------------------------------------


@app.get("/api/settings")
def api_list_settings() -> JSONResponse:
    """Return all settings rows."""
    return JSONResponse(list_settings())


@app.get("/api/settings/{key}")
def api_get_setting(key: str) -> JSONResponse:
    row = get_setting_raw(key)
    if row is None:
        raise HTTPException(status_code=404, detail=_("Setting not found"))
    return JSONResponse(row)


@app.put("/api/settings/{key}")
def api_set_setting(key: str, payload: SettingUpdate) -> JSONResponse:
    try:
        row = set_setting(key, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(row)


@app.delete("/api/settings/{key}", status_code=204)
def api_delete_setting(key: str) -> None:
    if not delete_setting(key):
        raise HTTPException(status_code=404, detail=_("Setting not found"))


# ---------------------------------------------------------------------------
# Routes -- API: claude-assets (read-only status)
# ---------------------------------------------------------------------------


@app.get("/api/claude-assets/status")
def api_claude_assets_status() -> JSONResponse:
    """Read-only: report whether the packaged Claude Code skill + slash
    command are installed in ``~/.claude`` and match the package version.

    Intentionally no write counterpart: installs are user-initiated via
    the ``ntasker install-claude-assets`` CLI to avoid CSRF / DNS-rebind
    write surface.
    """
    claude_home = resolve_claude_home(None)
    status = scan_status(claude_home, command_name="task")
    body = {
        "installed": status.installed,
        "drift": status.drift,
        "package_version": VERSION,
        "claude_home": str(claude_home),
        "files": [f.to_dict() for f in status.files],
    }
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# Routes -- API: projects + tags + phases + priorities
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def api_projects() -> JSONResponse:
    """Sidebar feed: ``__none__`` first, then live symlinks, each with open_count.

    If ``projects_dir`` is not configured (neither ENV nor DB), responds
    with an empty list and the ``X-Settings-Missing: projects_dir``
    response header so the UI can render a configuration banner.
    """
    projects_dir = get_projects_dir()
    projects = list_projects()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT project, COUNT(*) AS c
            FROM tasks
            WHERE status = 'open' AND archived = 0
            GROUP BY project
            """
        ).fetchall()
    counts: dict[str | None, int] = {row["project"]: int(row["c"]) for row in rows}

    out: list[dict] = [
        {"name": PROJECT_NONE_SENTINEL, "open_count": counts.get(None, 0)},
    ]
    for name in projects:
        out.append({"name": name, "open_count": counts.get(name, 0)})

    response = JSONResponse(out)
    if projects_dir is None:
        response.headers["X-Settings-Missing"] = "projects_dir"
    return response


@app.get("/api/tags")
def api_tags() -> JSONResponse:
    """All known tags with open-counts, sorted by ``open_count DESC, name ASC``."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.name AS name,
                   COALESCE(SUM(CASE WHEN tasks.status = 'open' AND tasks.archived = 0
                                     THEN 1 ELSE 0 END), 0) AS open_count
            FROM tags t
            LEFT JOIN task_tags tt ON tt.tag_id = t.id
            LEFT JOIN tasks ON tasks.id = tt.task_id
            GROUP BY t.id, t.name
            ORDER BY open_count DESC, name ASC
            """
        ).fetchall()
    return JSONResponse([{"name": r["name"], "open_count": int(r["open_count"])} for r in rows])


@app.post("/api/tags/cleanup")
def api_tags_cleanup() -> JSONResponse:
    """Delete dangling tags (no row in ``task_tags``). Idempotent."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT name FROM tags
            WHERE id NOT IN (SELECT DISTINCT tag_id FROM task_tags)
            ORDER BY name ASC
            """
        ).fetchall()
        names = [r["name"] for r in rows]
        if names:
            conn.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM task_tags)"
            )
    return JSONResponse({"removed": len(names), "removed_names": names})


@app.get("/api/priorities")
def api_priorities() -> JSONResponse:
    """Sidebar feed for the priority filter."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT priority, COUNT(*) AS c
            FROM tasks
            WHERE status = 'open' AND archived = 0
            GROUP BY priority
            """
        ).fetchall()
    counts: dict[str, int] = {row["priority"]: int(row["c"]) for row in rows}

    out: list[dict] = []
    for value, label in PRIORITY_ORDER:
        # Translate the label per request -- the label is the gettext
        # msgid, the active language drives the actual string.
        out.append(
            {"value": value, "label": _(label), "open_count": counts.get(value, 0)}
        )
    return JSONResponse(out)


@app.get("/api/phases")
def api_phases() -> JSONResponse:
    """Sidebar feed for the phase filter."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT phase, COUNT(*) AS c
            FROM tasks
            WHERE status = 'open' AND archived = 0
            GROUP BY phase
            """
        ).fetchall()
    counts: dict[str | None, int] = {row["phase"]: int(row["c"]) for row in rows}

    out: list[dict] = []
    for value, label in PHASE_ORDER:
        key: str | None = None if value == PHASE_NONE_SENTINEL else value
        out.append(
            {"value": value, "label": _(label), "open_count": counts.get(key, 0)}
        )
    return JSONResponse(out)


# ---------------------------------------------------------------------------
# Routes -- API: tasks (filter helpers + endpoints)
# ---------------------------------------------------------------------------


def _build_project_filter(project: list[str]) -> tuple[str, list[object]]:
    """Multi-value project filter -> SQL fragment + bind params."""
    if not project:
        return "", []

    include_null = False
    names: list[str] = []
    for p in project:
        if p in (PROJECT_NONE_SENTINEL, PROJECT_NULL_LEGACY):
            include_null = True
        elif p:
            names.append(p)

    clauses: list[str] = []
    params: list[object] = []
    if names:
        placeholders = ", ".join("?" for _ in names)
        clauses.append(f"project IN ({placeholders})")
        params.extend(names)
    if include_null:
        clauses.append("project IS NULL")

    if not clauses:
        return "", []
    return " AND (" + " OR ".join(clauses) + ")", params


def _build_phase_filter(phase: list[str]) -> tuple[str, list[object]]:
    """Multi-value phase filter -> SQL fragment + bind params."""
    if not phase:
        return "", []

    include_null = False
    names: list[str] = []
    for p in phase:
        if p == PHASE_NONE_SENTINEL:
            include_null = True
        elif p in PHASE_VALID:
            names.append(p)

    clauses: list[str] = []
    params: list[object] = []
    if names:
        placeholders = ", ".join("?" for _ in names)
        clauses.append(f"phase IN ({placeholders})")
        params.extend(names)
    if include_null:
        clauses.append("phase IS NULL")

    if not clauses:
        return "", []
    return " AND (" + " OR ".join(clauses) + ")", params


def _build_priority_filter(priority: list[str]) -> tuple[str, list[object]]:
    """Multi-value priority filter -> SQL fragment + bind params."""
    if not priority:
        return "", []
    names = [p for p in priority if p in PRIORITY_VALID]
    if not names:
        return "", []
    placeholders = ", ".join("?" for _ in names)
    return f" AND priority IN ({placeholders})", list(names)


def _build_tag_filter(tag: list[str]) -> tuple[str, list[object]]:
    """Multi-value OR filter on tag names (case-insensitive)."""
    norm = normalize_tags(tag)
    if not norm:
        return "", []
    placeholders = ", ".join("?" for _ in norm)
    fragment = (
        f" AND tasks.id IN (SELECT tt.task_id FROM task_tags tt "
        f"JOIN tags t ON t.id = tt.tag_id "
        f"WHERE t.name IN ({placeholders}))"
    )
    return fragment, list(norm)


def _query_tasks(
    project: list[str],
    tag: list[str],
    phase: list[str],
    priority: list[str],
    status: Status | None,
    archived: bool | None,
    search: str | None,
) -> list[sqlite3.Row]:
    """Run the SELECT against the tasks table with filters applied."""
    sql = "SELECT tasks.* FROM tasks WHERE 1=1"
    params: list[object] = []

    proj_clause, proj_params = _build_project_filter(project)
    if proj_clause:
        sql += proj_clause
        params.extend(proj_params)

    tag_clause, tag_params = _build_tag_filter(tag)
    if tag_clause:
        sql += tag_clause
        params.extend(tag_params)

    phase_clause, phase_params = _build_phase_filter(phase)
    if phase_clause:
        sql += phase_clause
        params.extend(phase_params)

    prio_clause, prio_params = _build_priority_filter(priority)
    if prio_clause:
        sql += prio_clause
        params.extend(prio_params)

    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    if archived is not None:
        sql += " AND archived = ?"
        params.append(1 if archived else 0)
    if search:
        sql += " AND (title LIKE ? OR COALESCE(description, '') LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])

    sql += " ORDER BY archived ASC, status ASC, created_at DESC"

    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/api/tasks")
def api_list_tasks(
    project: list[str] = Query(default=[]),  # noqa: B008
    tag: list[str] = Query(default=[]),  # noqa: B008
    phase: list[str] = Query(default=[]),  # noqa: B008
    priority: list[str] = Query(default=[]),  # noqa: B008
    status: Status | None = None,
    archived: bool | None = None,
    search: str | None = None,
) -> JSONResponse:
    rows = _query_tasks(project, tag, phase, priority, status, archived, search)
    ids = [int(r["id"]) for r in rows]
    with get_conn() as conn:
        tags_by_id = load_tags_bulk(conn, ids)
    return JSONResponse([row_to_task(r, tags_by_id.get(int(r["id"]), [])) for r in rows])


@app.get("/api/stats")
def api_stats(
    project: list[str] = Query(default=[]),  # noqa: B008
    tag: list[str] = Query(default=[]),  # noqa: B008
    phase: list[str] = Query(default=[]),  # noqa: B008
    priority: list[str] = Query(default=[]),  # noqa: B008
    search: str | None = None,
) -> JSONResponse:
    """Tab counts (open/done/archive) honoring all filters + search."""
    proj_clause, proj_params = _build_project_filter(project)
    tag_clause, tag_params = _build_tag_filter(tag)
    phase_clause, phase_params = _build_phase_filter(phase)
    prio_clause, prio_params = _build_priority_filter(priority)

    base_params: list[object] = []
    base_where = " WHERE 1=1"
    if proj_clause:
        base_where += proj_clause
        base_params.extend(proj_params)
    if tag_clause:
        base_where += tag_clause
        base_params.extend(tag_params)
    if phase_clause:
        base_where += phase_clause
        base_params.extend(phase_params)
    if prio_clause:
        base_where += prio_clause
        base_params.extend(prio_params)
    if search:
        base_where += " AND (title LIKE ? OR COALESCE(description, '') LIKE ?)"
        like = f"%{search}%"
        base_params.extend([like, like])

    queries = {
        "open": " AND status = 'open' AND archived = 0",
        "done": " AND status = 'done' AND archived = 0",
        "archive": " AND archived = 1",
    }

    counts: dict[str, int] = {}
    with get_conn() as conn:
        for key, extra in queries.items():
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks" + base_where + extra,
                base_params,
            ).fetchone()
            counts[key] = int(row["c"])
    return JSONResponse(counts)


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int) -> JSONResponse:
    """Single-task lookup. Used by FRIDAY for ``#<id>`` resolution."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=_("Task not found"))
        tags = load_tags_for(conn, task_id)
    return JSONResponse(row_to_task(row, tags))


@app.post("/api/tasks", status_code=201)
def api_create_task(payload: TaskCreate) -> JSONResponse:
    if payload.priority not in PRIORITY_VALID:
        raise HTTPException(status_code=400, detail=_("Invalid priority"))
    norm_tags = normalize_tags(payload.tags)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (project, title, description, phase, priority)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload.project,
                payload.title,
                payload.description,
                payload.phase,
                payload.priority,
            ),
        )
        new_id = int(cur.lastrowid)
        if norm_tags:
            set_task_tags(conn, new_id, norm_tags)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()
        tags = load_tags_for(conn, new_id)
    return JSONResponse(row_to_task(row, tags), status_code=201)


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, payload: TaskUpdate) -> JSONResponse:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail=_("No fields to update"))

    tags_raw = fields.pop("tags", None)

    if "priority" in fields and fields["priority"] not in PRIORITY_VALID:
        raise HTTPException(status_code=400, detail=_("Invalid priority"))

    if "status" in fields:
        if fields["status"] == "done":
            fields["completed_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            fields["completed_at"] = None

    if "archived" in fields:
        fields["archived"] = 1 if fields["archived"] else 0

    with get_conn() as conn:
        if fields:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            params = [*fields.values(), task_id]
            cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", params)
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=_("Task not found"))
        else:
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail=_("Task not found"))

        if tags_raw is not None:
            set_task_tags(conn, task_id, normalize_tags(tags_raw))

        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        tags = load_tags_for(conn, task_id)
    return JSONResponse(row_to_task(row, tags))


@app.delete("/api/tasks/{task_id}", status_code=204)
def api_delete_task(task_id: int) -> None:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=_("Task not found"))
