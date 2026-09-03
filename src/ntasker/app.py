"""FastAPI application for ntasker.

Submodule of the :mod:`ntasker` package; the CLI entry ``ntasker serve``
runs this app via uvicorn (see :mod:`ntasker.cli`). Static files and
templates are loaded via :func:`importlib.resources.files` so the package
works equally well from a wheel install and a local checkout.

Bind is the CLI's responsibility -- this module only exposes ``app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import sqlite3
import subprocess
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, WebSocket
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
from ntasker.agents import AGENTS, AGENT_KEYS, resolve_agent_key, resolve_home
from ntasker.claude_assets import scan_status
from ntasker.claude_runner import (
    active_session_ids,
    default_cwd_for_project,
    projects_base_dir,
    seed_command_for_task,
    session_states,
    stop_session,
    terminal_available,
)
from ntasker.claude_runner import serve as claude_serve
from ntasker.projects import discover_claude_projects
from ntasker import db as _db_module
from ntasker.db import (
    CONTEXT_KINDS,
    MCP_SCHEME,
    DepError,
    add_context,
    cleanup_database,
    delete_tags,
    get_conn,
    init_db,
    load_context_bulk,
    load_context_for,
    load_deps_bulk,
    load_deps_for,
    load_tags_bulk,
    load_tags_for,
    merge_tags,
    normalize_dep_ids,
    normalize_tags,
    remove_context,
    row_to_task,
    set_db_path,
    set_task_deps,
    set_task_tags,
    tasks_for_tag,
    title_from_description,
    validate_deps,
)
from ntasker.i18n import (
    N_,
    _,
    get_active_language,
    gettext_for_jinja,
    ngettext_for_jinja,
)
from ntasker.middleware import LanguageMiddleware
from ntasker import service
from ntasker import updates
from ntasker import workspace
from ntasker import brain
from ntasker.settings import (
    FIELD_CHOICES,
    FIELD_DEFAULTS,
    HINTS,
    VALIDATORS,
    delete_setting,
    ensure_settings_table,
    get_assets_mode_resolved,
    get_claude_open_terminal,
    get_default_agent,
    get_default_view,
    get_setting,
    get_setting_raw,
    list_settings,
    set_setting,
)

# Sentinel for "tasks without a project" (cross-project tasks). Used in
# multi-value project filters: ?project=__none__ -> include rows with project IS NULL.
PROJECT_NONE_SENTINEL = "__none__"
# Legacy single-value sentinel kept for backwards compatibility with old bookmarks.
PROJECT_NULL_LEGACY = "__null__"

# External links surfaced in the topbar + /info page. Single source of truth so
# the template anchors and the about section stay in sync.
LINKS = {
    "github": "https://github.com/nerdocs/ntasker",
    "issues": "https://github.com/nerdocs/ntasker/issues",
    "author": "https://github.com/nerdoc",
    "coffee": "https://buymeacoffee.com/nerdoc",
}

# Fixed phase order + English source labels. The workflow reads left-to-right:
# planned -> wip -> review. ``done`` is not a phase value but a status; the
# kanban view renders it as a fourth column derived from ``status='done'``.
# Labels go through ``_()`` at response time -- ``N_`` is a no-op marker
# so pybabel-extract picks up the strings; translations live in
# ``locale/<lang>/LC_MESSAGES/``.
PHASE_ORDER: list[tuple[str, str]] = [
    ("planned", N_("Planned")),
    ("wip", N_("In Progress")),
    ("review", N_("Review")),
]
PHASE_VALID = {value for value, _label in PHASE_ORDER}
PHASE_DEFAULT = "planned"

# Fixed priority order for the sidebar feed (highest first).
PRIORITY_ORDER: list[tuple[str, str]] = [
    ("critical", N_("Critical")),
    ("high", N_("High")),
    ("normal", N_("Normal")),
    ("low", N_("Low")),
]
PRIORITY_VALID = {value for value, _label in PRIORITY_ORDER}
PRIORITY_DEFAULT = "normal"

# SQL rank expression mirroring PRIORITY_ORDER (critical=0 .. low=3), so the
# ``sort=priority`` ordering stays in sync with the sidebar order. Unknown
# values sort last.
_PRIORITY_RANK_SQL = (
    "CASE priority "
    + " ".join(
        f"WHEN '{value}' THEN {rank}"
        for rank, (value, _label) in enumerate(PRIORITY_ORDER)
    )
    + f" ELSE {len(PRIORITY_ORDER)} END"
)


# ---------------------------------------------------------------------------
# Resource paths -- via importlib.resources so this works from a wheel install
# ---------------------------------------------------------------------------

_PKG_ROOT = files("ntasker")
TEMPLATES_DIR = _PKG_ROOT / "templates"
STATIC_DIR = _PKG_ROOT / "static"


# ---------------------------------------------------------------------------
# Projects: live, derived from tasks
# ---------------------------------------------------------------------------
#
# Since v2.0 projects are not a filesystem concept anymore. A "project" is
# simply a non-NULL ``tasks.project`` value -- the sidebar feed runs a
# ``SELECT DISTINCT project FROM tasks`` over the live data. That gives
# us automatic garbage collection: when the last task carrying a given
# project name is deleted (or moved to a different project), the
# project silently disappears from the sidebar and from autocomplete.
# There is no separate "projects" table, no FS scan, no projects_dir
# setting -- ``project`` is just a free-form string the user (or Claude)
# writes into a task.


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


Status = Literal["open", "done"]
Phase = Literal["planned", "wip", "review"]
Priority = Literal["low", "normal", "high", "critical"]


class ContextAdd(BaseModel):
    """One workspace file -- or, for ``kind="brain"``, one JCBrain note
    (``path`` = ``brain://<uuid>`` or the bare uuid) -- to attach to a task."""

    kind: str
    path: str
    label: str = ""
    note: str = ""


class TaskCreate(BaseModel):
    project: str | None = None
    # Optional: an empty/omitted title falls back to the start of the
    # description on insert (see ``title_from_description``).
    title: str = Field("", max_length=500)
    description: str | None = None
    # Tolerated as None for legacy clients (and for forms that emit an
    # empty <select>); the endpoint substitutes ``PHASE_DEFAULT`` on insert.
    phase: Phase | None = None
    # Plain ``str`` so the endpoint can return HTTP 400 (not 422) on bad
    # values via the explicit whitelist check.
    priority: str = "normal"
    # AI coding agent for this task (claude/opencode/pi). None -> the
    # ``default_agent`` setting decides at run time. Validated against the
    # registry on insert; a bad value yields HTTP 400.
    agent: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Task ids this task depends on. Validated (existence + no cycles) on
    # insert; an invalid set yields HTTP 400.
    depends: list[int] = Field(default_factory=list)
    # Workspace files attached at creation time -- same shape and the same
    # root-confinement checks as POST /api/tasks/{id}/context, validated
    # *before* the insert so a bad path never leaves a half-created task.
    context: list[ContextAdd] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    project: str | None = None
    # None (omitted) = unchanged. An explicit empty/whitespace title falls
    # back to the start of the description on update (mirrors create).
    title: str | None = Field(None, max_length=500)
    description: str | None = None
    status: Status | None = None
    phase: Phase | None = None
    priority: str | None = None
    # None when omitted = unchanged; an explicit null clears it (-> default
    # agent). Validated against the registry on update.
    agent: str | None = None
    archived: bool | None = None
    tags: list[str] | None = None  # None = unchanged; [] = clear all
    depends: list[int] | None = None  # None = unchanged; [] = clear all
    # Manual drag&drop position (fractional). None = unchanged. Written as a
    # plain column by the generic UPDATE path -- no extra validation needed.
    sort_order: float | None = None


class SettingUpdate(BaseModel):
    value: str


class TagMerge(BaseModel):
    # Source tags to fold into ``target``. ``target`` is created if new
    # (rename) or reused if it already exists (merge).
    sources: list[str] = Field(..., min_length=1)
    target: str = Field(..., min_length=1)


class TagDelete(BaseModel):
    names: list[str] = Field(..., min_length=1)


class ReorderIn(BaseModel):
    # Explicit manual order: ids[0] ends up topmost (largest sort_order).
    ids: list[int] = Field(..., min_length=1)


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


def _static_bust(name: str) -> str:
    """Return a cache-buster query-string for a local /static/ asset.

    Uses ``<VERSION>-<mtime>`` so two distinct edits within the same
    release window still invalidate the browser cache. Templates use
    ``<link href="/static/style.css?v={{ static_bust('style.css') }}">``
    instead of the bare ``?v={{ version }}`` we relied on before, which
    failed to bust the cache during development when many code changes
    sit under the same ``__version__``.
    """
    try:
        path = STATIC_DIR / name
        mtime = int(path.stat().st_mtime)
    except (OSError, AttributeError):
        # ``files()`` over a zipped wheel returns a Traversable that may
        # not support ``.stat()``. Fall back to just the version -- the
        # zip is immutable so the cache is fine.
        return VERSION
    return f"{VERSION}-{mtime}"


@functools.lru_cache(maxsize=1)
def get_git_commit() -> str | None:
    """Short git commit hash of the running source tree, or ``None`` when
    not run from a checkout (installed wheel, no git available).

    Shown only on the info page so a dev build reads ``v2.12.0.abc1234``;
    the result never changes without a restart, hence the cache.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


templates.env.globals["asset"] = _asset
templates.env.globals["asset_sri"] = _asset_sri
templates.env.globals["asset_mode"] = _asset_mode
templates.env.globals["t"] = _t
templates.env.globals["static_bust"] = _static_bust


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
        "manage_tags": _("Manage tags"),
        # Top bar
        "settings": _("Settings"),
        "light_mode": _("Light mode"),
        "dark_mode": _("Dark mode"),
        "info": _("Info"),
        "update_available_short": _("Update available"),
        "github": _("Source on GitHub"),
        "buy_me_a_coffee": _("Buy me a coffee"),
        "stale_version_notice": _(
            "nTasker was updated to v{version} -- reload the page to use the new version."
        ),
        "reload_page": _("Reload page"),
        # Page header / view switcher
        "tasks_title": _("Task list"),
        "view_list": _("Task list"),
        "view_kanban": _("Kanban"),
        "sort_by_priority": _("Sort by priority"),
        "sort_by_priority_title": _("Restore the default priority order (discards manual drag order)"),
        # Kanban board
        "kanban_col_done": _("Done"),
        "kanban_empty_column": _("(empty)"),
        "reorder_hint": _("Drag to reorder"),
        "expand_done": _("Expand done column"),
        "collapse_done": _("Collapse done column"),
        # Banners
        "configure_projects_dir": _(
            "Please configure the projects directory -- otherwise the project list stays empty."
        ),
        "go_to_settings": _("Go to settings"),
        # New-task form
        "new_task": _("New task"),
        "project": _("Project"),
        "project_placeholder": _("Project name (leave empty for cross-project)"),
        "project_will_create": _("New project -- a directory will be created at {path}"),
        "project_no_base_hint": _(
            "New project -- set a projects directory in the settings to have it created automatically"
        ),
        "phase": _("Phase"),
        "phase_none": _("--"),
        "phase_wip": _("In Progress"),
        "phase_planned": _("Planned"),
        "phase_review": _("Review"),
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
        "dependency_input_placeholder": _("Type a task title or #id"),
        "remove_dependency": _("Remove dependency"),
        "blocked": _("Blocked"),
        "blocked_hint": _("Blocked: a dependency is not done yet."),
        "blocked_by": _("blocked by:"),
        "create": _("Create"),
        # Search
        "search_placeholder": _("Search in title and description..."),
        "clear_search": _("Clear search"),
        "show_all_tasks": _("Show all tasks"),
        "show_all_tasks_title": _("Clear search and all filters -- show every task"),
        # Settings -- server restart
        "restart_server": _("Restart server"),
        "restart_initiated": _("Restarting server..."),
        "restart_failed": _("Restart failed -- the server is not running as a service."),
        "restart_timeout": _("Server did not come back in time -- reload manually."),
        "restart_blocked_tasks": _(
            "Restart blocked -- {n} task session(s) still running. "
            "A restart would interrupt them; wait until they finish."
        ),
        # Settings -- database cleanup
        "db_cleanup": _("Clean up database"),
        "db_cleanup_running": _("Cleaning up..."),
        "db_cleanup_done": _("Database cleaned up -- {freed} freed."),
        "db_cleanup_compact": _("Database cleaned up -- already compact."),
        "db_cleanup_failed": _("Cleanup failed -- try again."),
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
        "filter_phase_review": _("Filter: phase Review"),
        "filter_priority_critical": _("Filter: priority Critical"),
        "filter_priority_high": _("Filter: priority High"),
        "filter_priority_low": _("Filter: priority Low"),
        "filter_tag": _("Filter: tag {name}"),
        "edit": _("Edit"),
        # Distinct msgid from the "Archive" tab label: the button is an
        # action ("archive this task"), the tab is a place.
        "archive": _("Archive task"),
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
        "create_ok": _("Task #{id} created."),
        "create_ok_hidden": _("Task #{id} created -- hidden by an active filter."),
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
        "agent_integration": _("AI agent integration"),
        "agent_integration_intro": _(
            "ntasker ships a skill (SKILL.md) and a /task <id> slash command for "
            "each agent. This shows the install status per agent -- writes go "
            "exclusively through the CLI."
        ),
        "default_agent_label": _("Default agent"),
        "default_agent_hint": _(
            "Agent new tasks use, and the fallback for any task without one."
        ),
        "installed": _("Installed"),
        "package_version": _("Package version"),
        "drift": _("Drift"),
        "agent_home": _("Config home"),
        "agent_cli_missing": _(
            "CLI not found on PATH -- runs are disabled for this agent until it is installed."
        ),
        "agent_available_badge": _("CLI available"),
        "agent_unavailable_badge": _("CLI missing"),
        "yes": _("yes"),
        "no": _("no"),
        "agent_not_installed": _("Skill + slash command are not installed yet. Install with:"),
        "agent_drift": _(
            "Installed files differ from the package version. Update with backup:"
        ),
        "opencode_auto_label": _("Auto-approve actions (--auto)"),
        "opencode_auto_hint": _(
            "Run OpenCode sessions with --auto, so it accepts its own actions."
        ),
        "agent_bin_label": _("CLI path"),
        "agent_bin_hint": _(
            "Full path to the agent's CLI when it is not on the server's PATH "
            "(e.g. run as a service without nvm). Leave empty to auto-detect."
        ),
        "all_settings": _("All settings (DB content)"),
        "key": _("Key"),
        "value": _("Value"),
        "updated": _("updated"),
        "no_settings": _("No settings configured."),
        "no_settings_hint_prefix": _(
            "Configure a known key above, or set one via CLI:"
        ),
        # Agent run -- interactive terminal session
        "claude_run": _("Run"),
        "claude_resume": _("Resume session"),
        "claude_switch_session": _("Switch to session"),
        "claude_waiting": _("The agent is waiting for your input"),
        "claude_back": _("Back"),
        "claude_stop": _("Stop"),
        "claude_mark_done": _("Mark done"),
        "claude_started_background": _("Task #{id} started in the background."),
        "claude_connect_failed": _("Could not connect to the agent session."),
        "claude_disconnected": _("Connection to the session lost."),
        "claude_term_init_failed": _(
            "The terminal component failed to load -- reload the page (Ctrl+Shift+R)."
        ),
        "claude_file_too_large": _("File is too large to drop into the terminal (max 25 MB)."),
        "running_now": _("Active projects"),
        "confirm_parallel_run": _(
            'Project "{project}" already has a running agent session. '
            "Two agents in one project can conflict and cause inconsistencies. "
            "Start another anyway?"
        ),
        "project_busy_hint": _(
            "An agent is already running in this project -- a second one can get "
            "in its way. You can still start this task if you want to."
        ),
        "new_task_for_project": _("New task in this project -- opens the form to fill in"),
        "quick_run_for_project": _(
            "Start an agent in this project right away -- creates a task "
            "and opens a session with an empty prompt"
        ),
        # Placeholder title for the task the quick run creates on the fly.
        "quick_task_title": _("New task"),
        # Sidebar project categories
        "assign_category": _("Assign category"),
        "uncategorized": _("Uncategorized"),
        "category_placeholder": _("Category -- empty removes"),
        # Sidebar project hiding
        "hide_project": _("Hide this project -- tasks are kept, only the sidebar entry disappears"),
        "restore_project": _("Show this project again"),
        "show_hidden_projects": _("Show hidden projects"),
        # New-task / edit -- agent picker
        "agent_label": _("Agent"),
        "agent_not_installed_hint": _("not installed"),
        # Tag-management page
        "tags_manage_title": _("Manage tags"),
        "tags_table_intro": _(
            "Rename, merge or delete tags. Renaming a tag to an existing name "
            "merges the two; deleting strips the tag from every task."
        ),
        "tag_name": _("Tag"),
        "tag_open_count": _("Open"),
        "tag_total_count": _("Total"),
        "actions": _("Actions"),
        "rename_merge": _("Rename / merge"),
        "rename_merge_title": _("Rename or merge “#{name}”"),
        "rename_merge_hint": _(
            "Type a new name. If it already exists, the two tags are merged "
            "and every task is updated."
        ),
        "new_tag_name": _("New tag name"),
        "merge_into_existing": _("“#{name}” already exists -- the tags will be merged."),
        "merge_done": _("{n} task(s) updated to #{name}."),
        "merge_failed": _("Rename/merge failed."),
        "delete_tag": _("Delete tag"),
        "delete_tag_title": _("Delete “#{name}”?"),
        "delete_tag_unused": _("This tag is not used by any task."),
        "delete_tag_attached": _("The following tasks still use this tag:"),
        "delete_tag_warning": _("This removes the tag from those tasks. It cannot be undone."),
        "delete_tag_done": _("Tag #{name} deleted."),
        "delete_tag_failed": _("Delete failed."),
        "tags_empty": _("No tags yet."),
        "tags_filter_placeholder": _("Filter tags..."),
        "confirm": _("Delete"),
        "loading": _("Loading..."),
        # Info / About page
        "info_title": _("Info"),
        "news": _("News"),
        "update_available_title": _("A new version is available"),
        "update_available_body": _(
            "You are running {current}, but {latest} is available on PyPI. "
            "Update with:"
        ),
        "up_to_date": _("ntasker is up to date."),
        "update_check_failed": _("Could not reach PyPI to check for updates."),
        "checking_updates": _("Checking for updates..."),
        "about": _("About"),
        "made_by": _("Made by"),
        "license_label": _("License"),
        "open_github": _("View on GitHub"),
        "report_issue": _("Report an issue"),
        # Workspace
        "workspace": _("Workspace"),
        "ws_skills": _("Skills"),
        "ws_knowledge": _("Knowledge base"),
        "ws_team": _("Team"),
        "ws_documents": _("Documents"),
        "ws_tooling": _("Tooling"),
        "ws_not_configured": _("Not configured"),
        "ws_configure_hint": _(
            "Set this directory in the settings to fill this section."
        ),
        "ws_open_settings": _("Open settings"),
        "ws_missing_dir": _("The configured directory does not exist:"),
        # Counts carry their noun so the msgid stays unambiguous -- bare
        # words like "loads" / "broken" / "notes" already exist in this
        # catalog with unrelated meanings ("broken" -> "Blockiert").
        "ws_loads": _("{n} load correctly"),
        "ws_broken": _("{n} will not load"),
        "ws_skill_ok": _("Loads correctly"),
        "ws_skill_broken": _("Will not load"),
        "ws_notes": _("{n} notes"),
        "ws_areas": _("Areas"),
        "ws_indexes": _("Index notes"),
        "ws_open_obsidian": _("Open in Obsidian"),
        "ws_members": _("{n} personas"),
        "ws_no_role": _("No role stated"),
        "ws_search": _("Search..."),
        "ws_no_match": _("Nothing matches your search."),
        "ws_empty": _("This directory is empty."),
        "ws_all_kinds": _("All types"),
        "ws_modified": _("Modified"),
        "ws_size": _("Size"),
        "ws_preview": _("Preview"),
        "ws_preview_unavailable": _("This file type cannot be previewed."),
        "ws_preview_failed": _("Could not load the file."),
        "ws_truncated": _("Preview truncated -- the file is larger."),
        "ws_copy_path": _("Copy path"),
        "ws_copied": _("Copied"),
        "ws_close": _("Close"),
        "ws_loading": _("Loading..."),
        "ws_mcp_servers": _("MCP servers"),
        "ws_runtimes": _("Runtimes"),
        "ws_available": _("Available"),
        "ws_unavailable": _("Not found"),
        "ws_runtime_missing": _(
            "This server cannot start -- its runtime is not on PATH."
        ),
        "ws_transport": _("Transport"),
        "ws_secret_inline": _("Value stored in the config file"),
        "ws_secret_env": _("Read from an environment variable"),
        "ws_secret_empty": _("Empty value"),
        "ws_config_missing": _("No Claude Code config found at"),
        # Workspace: editing, browsing, attaching
        "ws_edit": _("Edit"),
        "ws_view": _("View"),
        "ws_save": _("Save"),
        "ws_cancel": _("Cancel"),
        "ws_create": _("Create"),
        "ws_done": _("Done"),
        "ws_delete": _("Move to trash"),
        "ws_rename": _("Rename"),
        "ws_open_external": _("Open in the default app"),
        "ws_open_page": _("Open the workspace page"),
        "ws_up": _("One level up"),
        "ws_browse_root": _("Browse all notes"),
        "ws_new_note": _("New note"),
        "ws_new_note_prompt": _("Name of the new note:"),
        "ws_new_note_placeholder": _("Name of the new note (.md is added)"),
        "ws_rename_prompt": _("New name:"),
        "ws_save_hint": _("Cmd/Ctrl+S saves"),
        "ws_saved": _("Saved"),
        "ws_and_more": _("{n} more..."),
        # Deletes name the trash they went to -- "deleted" from a browser
        # is alarming enough that the user deserves to know it is
        # recoverable, and from where.
        "ws_confirm_delete": _("Move {name} to the trash?"),
        "ws_trashed_os": _("{name} moved to the trash."),
        "ws_trashed_folder": _("{name} moved to the .ntasker-trash folder."),
        "ws_save_failed": _("Could not save the file."),
        "ws_delete_failed": _("Could not delete it."),
        "ws_rename_failed": _("Could not rename it."),
        "ws_create_failed": _("Could not create it."),
        "ws_open_failed": _("Could not open it."),
        # Task context attachments
        "ws_attach_context": _("Attach context"),
        "ws_attach_failed": _("Could not attach it."),
        "ws_detach_failed": _("Could not detach it."),
        "ws_detach": _("Detach"),
        "ws_no_context": _("Nothing attached yet."),
        "ws_context_missing": _("This file no longer exists at that path."),
        "ws_note_placeholder": _("Why is this attached? (optional)"),
        # Local files as context
        "ws_files": _("Files"),
        "ws_file": _("File"),
        "ws_file_hint": _(
            "Any file or folder on this computer. The agent gets the path and "
            "reads it when it needs to -- no more pasting paths into the description."
        ),
        "ws_file_path_placeholder": _("Paste a path, e.g. ~/Desktop/report.pdf"),
        "ws_file_add": _("Add"),
        "ws_file_choose": _("Choose files…"),
        "ws_file_choose_folder": _("Choose folder…"),
        "ws_file_picking": _("Waiting for the file dialog…"),
        "ws_file_pick_failed": _("Could not open the file dialog."),
        "ws_file_none": _("No files attached yet."),
        "ws_file_preview_after_create": _("Create the task first -- then the file opens from here."),
        "ws_folder": _("Folder"),
        "ws_fs_places": _("Places"),
        "ws_fs_up": _("Up one level"),
        "ws_fs_attach_folder": _("Attach this folder"),
        "ws_fs_filter": _("Filter this folder…"),
        "ws_fs_empty": _("This folder is empty."),
        "ws_fs_truncated": _("Only the first {n} entries are shown."),
        "ws_fs_browse_failed": _("Could not open that folder."),
        "ws_fs_or_path": _("Or paste a path:"),
        # JCBrain notes as context
        "ws_brain": _("JCBrain"),
        "ws_brain_note": _("JCBrain note"),
        "ws_brain_search_hint": _("Type to search your JCBrain notes by meaning."),
        "ws_brain_searching": _("Searching JCBrain…"),
        "ws_brain_no_results": _("No notes match."),
        "ws_brain_not_configured": _(
            "JCBrain is not configured -- add the MCP server to ~/.claude.json "
            "(setting: brain_server)."
        ),
        "ws_brain_failed": _("Could not reach JCBrain."),
        "ws_brain_captured": _("Captured"),
        # MCP servers as context
        "ws_mcp": _("MCP servers"),
        "ws_mcp_server": _("MCP server"),
        "ws_mcp_hint": _("Servers declared in ~/.claude.json. Attaching one tells the agent to use its tools for this task."),
        "ws_mcp_runtime_missing": _("runtime missing"),
        "ws_mcp_transport": _("Transport"),
        "ws_mcp_command": _("Command"),
        "ws_mcp_gone": _("This server is no longer declared in ~/.claude.json."),
        "ws_plugin": _("Plugin"),
        "ws_bundles": _("Bundles:"),
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


# How often the background sweep checks whether a live Claude session's task has
# been finished behind the server's back (CLI / direct DB write / deletion).
CLAUDE_REAP_INTERVAL = 3.0

# How often the background poll actively refreshes the PyPI update-check cache.
UPDATE_POLL_INTERVAL = 24 * 60 * 60  # once a day

_reaper_task: asyncio.Task | None = None
_update_poll_task: asyncio.Task | None = None


async def _reap_finished_claude_sessions() -> None:
    """Tear down any live Claude session whose task is no longer open.

    The DB is the single source of truth, and *both* the HTTP API and the CLI
    write task status straight to SQLite -- but only the API path reaches
    :func:`stop_session` synchronously. ``ntasker done`` / ``ntasker patch
    --status done`` (and any direct DB edit or delete) flip the bit in another
    process, so this loop watches the DB and stops the now-orphaned session no
    matter which path finished the task. Reliability backstop -- the immediate
    teardown in the PATCH handler still fires first on the UI path.
    """
    while True:
        await asyncio.sleep(CLAUDE_REAP_INTERVAL)
        tids = active_session_ids()
        if not tids:
            continue
        try:
            placeholders = ",".join("?" * len(tids))
            with get_conn() as conn:
                rows = conn.execute(
                    f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
                    tids,
                ).fetchall()
        except Exception:  # noqa: BLE001 -- a DB hiccup must never kill the loop
            continue
        still_open = {r["id"] for r in rows if r["status"] != "done"}
        for tid in tids:
            # ``done`` or row gone (deleted) -> the session is orphaned, stop it.
            if tid not in still_open:
                stop_session(tid)


@app.on_event("startup")
async def _start_claude_reaper() -> None:
    global _reaper_task
    _reaper_task = asyncio.create_task(_reap_finished_claude_sessions())


async def _poll_updates() -> None:
    """Actively refresh the PyPI update-check cache on boot and once a day.

    The in-memory cache in :mod:`ntasker.updates` has a 24h TTL, but it only
    refreshes when something *calls* ``check()`` -- i.e. when a client hits
    ``/api/update-check``. On a long-running server nobody may load a page for
    days, so the "update available" badge would silently go stale. This loop
    drives the refresh itself: once right after startup, then every 24h,
    ``force``-ing past the TTL. Each check runs in a worker thread and swallows
    everything so a slow/offline PyPI never delays readiness or kills the loop.
    """
    while True:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(updates.check, True)
        await asyncio.sleep(UPDATE_POLL_INTERVAL)


@app.on_event("startup")
async def _start_update_poll() -> None:
    global _update_poll_task
    _update_poll_task = asyncio.create_task(_poll_updates())


@app.on_event("shutdown")
async def _stop_claude_reaper() -> None:
    if _reaper_task is not None:
        _reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _reaper_task


@app.on_event("shutdown")
async def _stop_update_poll() -> None:
    if _update_poll_task is not None:
        _update_poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _update_poll_task


# ---------------------------------------------------------------------------
# Routes -- liveness probe
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    """Liveness probe for `ntasker serve --detach` and external supervisors.

    Intentionally DB-free so a half-broken install still reports `ok`
    quickly. Returns the package version so callers can detect a stale
    background server after an upgrade.
    """
    return {"ok": True, "version": VERSION}


def _self_terminate() -> None:
    """Schedule a clean self-shutdown shortly after the response is flushed.

    Used by ``POST /shutdown`` -- send a signal to our own process so
    uvicorn runs its lifespan-shutdown hooks (DB connections, etc.) and
    exits with the standard code path. On Windows ``SIGTERM`` is still
    callable through ``os.kill`` (Python maps it to TerminateProcess);
    uvicorn handles either form gracefully.
    """
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    import time  # noqa: PLC0415

    time.sleep(0.05)  # let the HTTP response finish flushing
    os.kill(os.getpid(), signal.SIGTERM)


@app.post("/shutdown")
def shutdown(background_tasks: BackgroundTasks) -> JSONResponse:
    """Ask the server to shut itself down. Used by ``ntasker stop``.

    The actual signal is sent from a background task so the HTTP response
    leaves the socket cleanly before the process dies. Bound to
    127.0.0.1 only at the uvicorn layer -- never exposed externally.

    Idempotent from the caller's perspective: if the server is already
    gone, the connection just refuses and the CLI treats that as success.
    """
    background_tasks.add_task(_self_terminate)
    return JSONResponse({"ok": True, "shutting_down": True}, status_code=202)


def _self_restart() -> None:
    """Ask the service manager to restart us, after the response has flushed.

    ``systemctl restart`` / ``launchctl kickstart`` hand the restart job to
    the supervisor, which then tears down and re-spawns this process -- so the
    new code is picked up without the client losing the server for good.
    """
    import time  # noqa: PLC0415

    time.sleep(0.05)  # let the HTTP response finish flushing
    service.restart_service()


@app.post("/api/service/restart")
def restart(background_tasks: BackgroundTasks) -> JSONResponse:
    """Restart the supervised server via its service manager.

    Only works when ntasker runs under an installed systemd/launchd unit;
    standalone there is no supervisor to bring the process back, so we refuse
    with 409. The restart itself runs as a background task so the HTTP response
    leaves the socket before the supervisor stops the process.

    Refused with 409 ``tasks_running`` while any Claude task session is live:
    ``KillMode=control-group`` means a restart tears down every child in the
    unit's cgroup, so we never kill a running -- or input-waiting -- task from
    under the user. The settings button mirrors this by disabling itself.
    """
    if not service.service_installed():
        return JSONResponse({"ok": False, "reason": "not_supervised"}, status_code=409)
    active = active_session_ids()
    if active:
        return JSONResponse(
            {"ok": False, "reason": "tasks_running", "tasks": sorted(active)},
            status_code=409,
        )
    background_tasks.add_task(_self_restart)
    return JSONResponse({"ok": True, "restarting": True}, status_code=202)


@app.post("/api/maintenance/cleanup")
def maintenance_cleanup() -> JSONResponse:
    """Compact the database: VACUUM + PRAGMA optimize.

    Reclaims the free pages left by deleted/archived tasks and refreshes the
    query-planner stats. Synchronous -- the file is small and the operation
    is near-instant, so the client just waits for the freed-bytes report.
    """
    stats = cleanup_database()
    return JSONResponse({"ok": True, **stats})


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
            "default_view": get_default_view(),
            "claude_open_terminal": get_claude_open_terminal(),
            "default_agent": get_default_agent(),
            # Configured projects base (expanded) or "" -- lets the project
            # input show where a new project's directory will be created.
            "projects_base": str(projects_base_dir() or ""),
            "links": LINKS,
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
    # Coerce the LazyString labels/descriptions to plain strings for the
    # active language; None descriptions become "".
    field_choices = {
        key: [
            {"value": value, "label": str(label), "desc": str(desc) if desc else ""}
            for (value, label, desc) in opts
        ]
        for key, opts in FIELD_CHOICES.items()
    }
    response = templates.TemplateResponse(
        request,
        "settings.html",
        context={
            "version": VERSION,
            "hints": hints_text,
            "field_choices": field_choices,
            "field_defaults": FIELD_DEFAULTS,
            "known_keys": sorted(VALIDATORS.keys()),
            "language": get_active_language(),
            "js_strings": build_js_strings(),
            "can_restart": service.service_installed(),
            "links": LINKS,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request) -> HTMLResponse:
    """Tag-management UI: list, rename/merge, delete, clean up unused tags."""
    response = templates.TemplateResponse(
        request,
        "tags.html",
        context={
            "version": VERSION,
            "language": get_active_language(),
            "js_strings": build_js_strings(),
            "links": LINKS,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/info", response_class=HTMLResponse)
def info_page(request: Request) -> HTMLResponse:
    """Info / About page: update alerts on top, author + project info below."""
    response = templates.TemplateResponse(
        request,
        "info.html",
        context={
            "version": VERSION,
            "commit": get_git_commit(),
            "language": get_active_language(),
            "js_strings": build_js_strings(),
            "links": LINKS,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/workspace", response_class=HTMLResponse)
def workspace_page(request: Request) -> HTMLResponse:
    """Workspace page: skills, knowledge base, team personas, tooling.

    The context around the tasks -- what automates work (skills), what it
    draws on (knowledge base), who it is delegated to (personas), and what
    has to be installed for any of it to run (tooling).
    """
    response = templates.TemplateResponse(
        request,
        "workspace.html",
        context={
            "version": VERSION,
            "language": get_active_language(),
            "js_strings": build_js_strings(),
            "links": LINKS,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/workspace")
def api_workspace() -> JSONResponse:
    """Return the full workspace inventory.

    Read-only filesystem scan driven by the ``workspace_*_dir`` settings.
    Unconfigured or missing directories yield empty sections with
    ``configured`` / ``exists`` flags rather than an error -- having none of
    them set up is the normal state for most installs.
    """
    return JSONResponse(
        workspace.collect(
            skills_dir=get_setting("workspace_skills_dir"),
            wiki_dir=get_setting("workspace_wiki_dir"),
            team_dir=get_setting("workspace_team_dir"),
            docs_dir=get_setting("workspace_docs_dir"),
        )
    )


@app.get("/api/workspace/file")
def api_workspace_file(path: str = Query(..., description="Absolute file path")) -> JSONResponse:
    """Return one file's content for the in-page previewer.

    Reads are confined to the configured ``workspace_*_dir`` directories
    (see :func:`ntasker.workspace.allowed_roots`). A path outside all of
    them is refused with 403 -- binding to localhost is not an access
    control, and this endpoint would otherwise expose the entire
    filesystem to anything that can reach the port.
    """
    roots = workspace.allowed_roots(
        skills_dir=get_setting("workspace_skills_dir"),
        wiki_dir=get_setting("workspace_wiki_dir"),
        team_dir=get_setting("workspace_team_dir"),
        docs_dir=get_setting("workspace_docs_dir"),
    )
    try:
        return JSONResponse(workspace.read_file(path, roots))
    except workspace.PreviewError as exc:
        status = {"forbidden": 403, "not_found": 404, "too_large": 413}.get(
            exc.reason, 400
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.get("/api/workspace/browse")
def api_workspace_browse(
    path: str = Query(..., description="Absolute directory path"),
) -> JSONResponse:
    """List one directory inside the configured workspace directories.

    Lets the UI walk into a knowledge base rather than only summarising it
    -- the section scan says "Publikationen: 372 notes", this is how the
    user gets to note 214.
    """
    roots = _workspace_roots()
    try:
        return JSONResponse(workspace.browse(path, roots))
    except workspace.PreviewError as exc:
        status = {"forbidden": 403, "not_found": 404, "too_large": 413}.get(
            exc.reason, 400
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _workspace_roots() -> list:
    """The configured workspace directories -- the boundary for every
    filesystem operation the workspace endpoints perform."""
    return workspace.allowed_roots(
        skills_dir=get_setting("workspace_skills_dir"),
        wiki_dir=get_setting("workspace_wiki_dir"),
        team_dir=get_setting("workspace_team_dir"),
        docs_dir=get_setting("workspace_docs_dir"),
    )


#: Maps a :class:`~ntasker.workspace.WriteError` reason to its HTTP status.
_WRITE_STATUS = {
    "forbidden": 403,
    "not_found": 404,
    "exists": 409,
    "too_large": 413,
    "invalid": 400,
}


def _require_local_origin(request: Request) -> None:
    """Reject cross-site requests to the filesystem-mutating endpoints.

    ntasker has no auth -- it does not need any for its own data, which
    never leaves the machine. These endpoints are different: they rename and
    trash real files, and any page the user happens to have open could fire
    a ``fetch()`` at ``127.0.0.1:8766`` in the background. A browser always
    stamps such a request with its own ``Origin``, so requiring the origin
    to be either absent (curl, the CLI, a WebView with no origin) or one of
    ours is enough to shut that door.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    host = urlsplit(origin).hostname or ""
    if host in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        return
    raise HTTPException(
        status_code=403,
        detail=_("Cross-site requests may not change files."),
    )


def _write_guard(exc: "workspace.WriteError") -> HTTPException:
    """Turn a WriteError into the matching HTTPException."""
    return HTTPException(
        status_code=_WRITE_STATUS.get(exc.reason, 400), detail=str(exc)
    )


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


@app.put("/api/workspace/file")
def api_workspace_write(request: Request, payload: WorkspaceWrite) -> JSONResponse:
    """Overwrite one text file inside the configured workspace directories."""
    _require_local_origin(request)
    try:
        return JSONResponse(
            workspace.write_file(payload.path, payload.text, _workspace_roots())
        )
    except workspace.WriteError as exc:
        raise _write_guard(exc) from exc
    except workspace.PreviewError as exc:
        # write_file re-reads the file to hand the fresh content back. Every
        # editable suffix is also previewable, so the only way to land here
        # is the file vanishing between the two steps.
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/workspace/entry", status_code=201)
def api_workspace_create(request: Request, payload: WorkspaceCreate) -> JSONResponse:
    """Create an empty note (or a folder) inside a workspace directory."""
    _require_local_origin(request)
    try:
        return JSONResponse(
            workspace.create_entry(
                payload.parent, payload.name, _workspace_roots(), payload.directory
            ),
            status_code=201,
        )
    except workspace.WriteError as exc:
        raise _write_guard(exc) from exc


@app.post("/api/workspace/rename")
def api_workspace_rename(request: Request, payload: WorkspaceRename) -> JSONResponse:
    """Rename a file or folder in place."""
    _require_local_origin(request)
    try:
        return JSONResponse(
            workspace.rename_entry(payload.path, payload.name, _workspace_roots())
        )
    except workspace.WriteError as exc:
        raise _write_guard(exc) from exc


@app.post("/api/workspace/delete")
def api_workspace_delete(request: Request, payload: WorkspacePath) -> JSONResponse:
    """Move a file or folder to the trash.

    POST rather than DELETE so the target path travels in the body: paths
    here routinely contain spaces, umlauts and ``#`` (OneDrive's
    "OneDrive-Persönlich" alone breaks naive query-string handling), and a
    body sidesteps every layer of URL escaping between the browser and
    Starlette's router.
    """
    _require_local_origin(request)
    try:
        return JSONResponse(workspace.delete_entry(payload.path, _workspace_roots()))
    except workspace.WriteError as exc:
        raise _write_guard(exc) from exc


@app.post("/api/workspace/reveal")
def api_workspace_reveal(request: Request, payload: WorkspacePath) -> JSONResponse:
    """Hand a file to the desktop's default application."""
    _require_local_origin(request)
    try:
        return JSONResponse(workspace.reveal(payload.path, _workspace_roots()))
    except workspace.WriteError as exc:
        raise _write_guard(exc) from exc


@app.get("/api/update-check")
def api_update_check() -> JSONResponse:
    """Report whether a newer ntasker release is on PyPI.

    Returns ``{current, latest, update_available, error}``. Cached for 24h
    (see :mod:`ntasker.updates`); offline simply yields ``latest=null`` with
    an ``error`` string -- never an HTTP error.
    """
    return JSONResponse(updates.check())


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
# Routes -- API: agents (registry + availability + integration status)
# ---------------------------------------------------------------------------


@app.get("/api/agents")
def api_agents() -> JSONResponse:
    """Per-agent registry feed: availability + ``/task`` integration status.

    Single source for the frontend: which agents exist, whether each one's CLI
    is launchable (binary on PATH + a POSIX PTY), and whether its skill + slash
    command are installed (and match the package). Drives the per-task run
    button, the new-task agent picker, and the /settings integration cards.

    Read-only -- installs go through the ``ntasker agent install`` CLI to avoid
    CSRF / DNS-rebind write surface. Reports ``default`` so the UI knows which
    agent a task without an explicit ``agent`` will run on.
    """
    default = get_default_agent()
    out: list[dict] = []
    for spec in AGENTS.values():
        available, reason = terminal_available(spec)
        try:
            home = resolve_home(spec)
            status = scan_status(spec, home, command_name="task")
            assets = {
                "installed": status.installed,
                "drift": status.drift,
                "home": str(home),
            }
        except Exception:  # noqa: BLE001 -- a broken home must not 500 the page
            assets = {"installed": False, "drift": False, "home": None}
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "icon": spec.icon,
                "available": available,
                "reason": reason,
                "is_default": spec.key == default,
                "assets": assets,
            }
        )
    return JSONResponse({"default": default, "package_version": VERSION, "agents": out})


# ---------------------------------------------------------------------------
# Routes -- API + WebSocket: interactive agent runs
# ---------------------------------------------------------------------------


@app.get("/api/claude/sessions")
def api_claude_sessions() -> JSONResponse:
    """Live sessions per task -- feeds the busy + "waiting for input" indicators.

    ``active``: every task id with a live session. ``waiting``: the subset that
    has gone silent long enough to look blocked on a prompt (see
    :func:`ntasker.claude_runner.session_states`). ``projects``: the project of
    each active task (id -> name|null) -- feeds the running-projects chips and
    the same-project parallel-run warning on the frontend. ``agents``: the
    resolved agent key of each active task (id -> key) -- feeds the agent logo
    on each running-session link. ``titles``: the current title of each active
    task (id -> title) -- lets the run-view tab strip follow a title that
    changes mid-session (e.g. a placeholder task getting its real name).
    """
    states = session_states()
    active = list(states.keys())
    projects: dict[int, str | None] = {}
    agents: dict[int, str] = {}
    titles: dict[int, str] = {}
    if active:
        placeholders = ",".join("?" * len(active))
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, title, project, agent FROM tasks WHERE id IN ({placeholders})",
                active,
            ).fetchall()
        projects = {row["id"]: row["project"] for row in rows}
        agents = {row["id"]: resolve_agent_key(row["agent"]) for row in rows}
        titles = {row["id"]: row["title"] for row in rows}
    return JSONResponse(
        {
            "active": active,
            "waiting": [tid for tid, st in states.items() if st == "waiting"],
            "agents": agents,
            "projects": projects,
            "titles": titles,
        }
    )


@app.get("/api/tasks/{task_id}/claude-run/defaults")
def api_claude_run_defaults(task_id: int) -> JSONResponse:
    """Pre-fill the run launch: a guessed cwd + the ``/task <id>`` seed input."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=_("Task not found"))
    task = dict(row)
    return JSONResponse(
        {
            "seed": seed_command_for_task(task),
            "cwd": default_cwd_for_project(task["project"]) or "",
        }
    )


@app.websocket("/ws/claude/{task_id}")
async def ws_claude_run(websocket: WebSocket, task_id: int) -> None:
    """Bridge the browser terminal to task ``task_id``'s interactive ``claude``.

    Bound to 127.0.0.1 like the rest of ntasker; there is no auth layer, so the
    session is the local user's full interactive Claude Code -- shell included.
    See :mod:`ntasker.claude_runner`.
    """
    await websocket.accept()
    await claude_serve(websocket, task_id)


# ---------------------------------------------------------------------------
# Routes -- API: projects + tags + phases + priorities
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def api_projects() -> JSONResponse:
    """Sidebar feed: ``__none__`` first, then the union of every Claude Code
    project and every project name already referenced by a task, each with its
    open-task count.

    Projects are sourced from two places (since v2.1):

    * Claude Code's own project directories under ``~/.claude/projects`` --
      decoded to ``~``-relative, ``/``-separated names (``Projekte/medux``).
      See :func:`ntasker.projects.discover_claude_projects`.
    * Any non-NULL ``tasks.project`` value -- so free-form names that do not
      correspond to a Claude project (and never vanish a project that still
      carries tasks) keep showing up.
    """
    with get_conn() as conn:
        # All distinct project names currently referenced by any task
        # (archived included so a project with only archived tasks still
        # appears -- the user can decide to unarchive or delete).
        names_rows = conn.execute(
            "SELECT DISTINCT project FROM tasks "
            "WHERE project IS NOT NULL "
            "ORDER BY project COLLATE NOCASE ASC"
        ).fetchall()
        # Open-counts: archived/done excluded, same semantics as in v1.x.
        count_rows = conn.execute(
            """
            SELECT project, COUNT(*) AS c
            FROM tasks
            WHERE status = 'open' AND archived = 0
            GROUP BY project
            """
        ).fetchall()
        cat_rows = conn.execute(
            "SELECT project, category FROM project_categories"
        ).fetchall()
        hidden_rows = conn.execute("SELECT project FROM hidden_projects").fetchall()
    counts: dict[str | None, int] = {row["project"]: int(row["c"]) for row in count_rows}
    categories: dict[str, str] = {row["project"]: row["category"] for row in cat_rows}
    hidden: set[str] = {row["project"] for row in hidden_rows}

    # Union of Claude-discovered projects and names already on a task.
    # Defensively drop the reserved sentinels so a task that accidentally
    # stored one as its project value can never produce a duplicate row.
    names = (set(discover_claude_projects()) | {row["project"] for row in names_rows}) - {
        PROJECT_NONE_SENTINEL,
        PROJECT_NULL_LEGACY,
    }

    out: list[dict] = [
        {
            "name": PROJECT_NONE_SENTINEL,
            "open_count": counts.get(None, 0),
            "category": None,
            "hidden": False,
        },
    ]
    for name in sorted(names, key=str.casefold):
        out.append(
            {
                "name": name,
                "open_count": counts.get(name, 0),
                "category": categories.get(name),
                "hidden": name in hidden,
            }
        )

    return JSONResponse(out)


class ProjectCategorySet(BaseModel):
    """Assign (or clear) the sidebar category of one project."""

    project: str
    # None or blank clears the assignment -- the project goes back to the
    # uncategorized group.
    category: str | None = None


@app.put("/api/projects/category")
def api_set_project_category(payload: ProjectCategorySet) -> JSONResponse:
    """Set or clear a project's sidebar category.

    Body-based (not a path parameter) because project names may contain
    slashes (``Code/Heimprojekte/Poolterrasse``). The category is a free-form
    string -- the set of categories is exactly the set currently in use,
    nothing is pre-registered. No existence check on the project: categories
    may be assigned to discovered (task-less) projects too.
    """
    project = payload.project.strip()
    if not project or project == PROJECT_NONE_SENTINEL:
        raise HTTPException(status_code=400, detail=_("Invalid project name"))
    category = (payload.category or "").strip()
    with get_conn() as conn:
        if category:
            conn.execute(
                """
                INSERT INTO project_categories (project, category) VALUES (?, ?)
                ON CONFLICT (project) DO UPDATE SET category = excluded.category
                """,
                (project, category),
            )
        else:
            conn.execute(
                "DELETE FROM project_categories WHERE project = ?", (project,)
            )
    return JSONResponse({"project": project, "category": category or None})


class ProjectHiddenSet(BaseModel):
    """Hide a project from the sidebar entirely, or restore it."""

    project: str
    hidden: bool


@app.put("/api/projects/hidden")
def api_set_project_hidden(payload: ProjectHiddenSet) -> JSONResponse:
    """Hide or restore one project in the sidebar feed.

    Hiding is a persisted veto, not a delete: tasks keep their ``project``
    value untouched and discovered directories stay on disk -- the name is
    only excluded from the sidebar until restored. Body-based like the
    category endpoint, because project names may contain slashes.
    """
    project = payload.project.strip()
    if not project or project == PROJECT_NONE_SENTINEL:
        raise HTTPException(status_code=400, detail=_("Invalid project name"))
    with get_conn() as conn:
        if payload.hidden:
            conn.execute(
                "INSERT OR IGNORE INTO hidden_projects (project) VALUES (?)",
                (project,),
            )
        else:
            conn.execute("DELETE FROM hidden_projects WHERE project = ?", (project,))
    return JSONResponse({"project": project, "hidden": payload.hidden})


@app.get("/api/tags")
def api_tags() -> JSONResponse:
    """All known tags with open- and total-counts, sorted by
    ``open_count DESC, name ASC``. ``total_count`` covers every task (archived
    and done included) and drives the tag-management page."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.name AS name,
                   COALESCE(SUM(CASE WHEN tasks.status = 'open' AND tasks.archived = 0
                                     THEN 1 ELSE 0 END), 0) AS open_count,
                   COUNT(tasks.id) AS total_count
            FROM tags t
            LEFT JOIN task_tags tt ON tt.tag_id = t.id
            LEFT JOIN tasks ON tasks.id = tt.task_id
            GROUP BY t.id, t.name
            ORDER BY open_count DESC, name ASC
            """
        ).fetchall()
    return JSONResponse(
        [
            {
                "name": r["name"],
                "open_count": int(r["open_count"]),
                "total_count": int(r["total_count"]),
            }
            for r in rows
        ]
    )


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


@app.get("/api/tags/{name}/tasks")
def api_tag_tasks(name: str) -> JSONResponse:
    """Tasks carrying *name* -- powers the delete confirmation ("the following
    tasks still use this tag")."""
    with get_conn() as conn:
        rows = tasks_for_tag(conn, name)
    return JSONResponse(
        [
            {
                "id": int(r["id"]),
                "title": r["title"],
                "status": r["status"],
                "archived": bool(r["archived"]),
            }
            for r in rows
        ]
    )


@app.post("/api/tags/merge")
def api_tags_merge(payload: TagMerge) -> JSONResponse:
    """Rename or merge tags: re-point every task on a source tag onto the
    target, then drop the sources. Idempotent for already-merged sets."""
    norm_target = normalize_tags([payload.target])
    if not norm_target:
        raise HTTPException(status_code=400, detail=_("Target tag is empty."))
    with get_conn() as conn:
        affected = merge_tags(conn, payload.sources, norm_target[0])
    return JSONResponse({"target": norm_target[0], "affected": affected})


@app.post("/api/tags/delete")
def api_tags_delete(payload: TagDelete) -> JSONResponse:
    """Delete tags outright, stripping them from every task they touch."""
    with get_conn() as conn:
        removed = delete_tags(conn, payload.names)
    return JSONResponse({"removed": removed})


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
    """Sidebar feed for the phase filter.

    Returns the three workflow phases in their canonical order. Done is
    intentionally not a phase -- it's derived from ``status`` and shown
    as a separate kanban column in the UI.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT phase, COUNT(*) AS c
            FROM tasks
            WHERE status = 'open' AND archived = 0
            GROUP BY phase
            """
        ).fetchall()
    counts: dict[str, int] = {row["phase"]: int(row["c"]) for row in rows}

    out: list[dict] = []
    for value, label in PHASE_ORDER:
        out.append({"value": value, "label": _(label), "open_count": counts.get(value, 0)})
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
    """Multi-value phase filter -> SQL fragment + bind params.

    Since v2.0 ``phase`` is NOT NULL and limited to ``{planned, wip, review}``;
    unknown values are silently dropped. There is no ``__none__`` sentinel
    anymore -- legacy queries that send it just match nothing.
    """
    if not phase:
        return "", []
    names = [p for p in phase if p in PHASE_VALID]
    if not names:
        return "", []
    placeholders = ", ".join("?" for _ in names)
    return f" AND phase IN ({placeholders})", list(names)


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
    sort: str = "priority",
) -> list[sqlite3.Row]:
    """Run the SELECT against the tasks table with filters applied.

    ``sort`` picks the primary in-group ordering: ``priority`` (default)
    ranks critical->low then newest first; ``manual`` honours the
    drag&drop ``sort_order``.
    """
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
        # Substring match on title / description (always). Additionally, if
        # the search string (with an optional leading `#` stripped) is
        # purely digits, also match `tasks.id` exactly so users can locate
        # a task by typing its number -- "240" and "#240" both find #240.
        clauses = ["title LIKE ?", "COALESCE(description, '') LIKE ?"]
        like = f"%{search}%"
        params.extend([like, like])
        candidate = search.lstrip("#").strip()
        if candidate.isdigit():
            clauses.append("tasks.id = ?")
            params.append(int(candidate))
        sql += " AND (" + " OR ".join(clauses) + ")"

    # archived/status keep done + archived rows grouped at the bottom of
    # unfiltered queries. The in-group order then depends on ``sort``:
    #  - ``priority``: rank critical->low, id DESC (newest first) as tie-break.
    #    sort_order is ignored -- the ordering is fully deterministic.
    #  - ``manual``: drag&drop sort_order DESC, id DESC as tie-break.
    if sort == "manual":
        in_group = "sort_order DESC, id DESC"
    else:
        in_group = f"{_PRIORITY_RANK_SQL} ASC, id DESC"
    sql += f" ORDER BY archived ASC, status ASC, {in_group}"

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
    sort: Literal["priority", "manual"] = "priority",
) -> JSONResponse:
    rows = _query_tasks(project, tag, phase, priority, status, archived, search, sort)
    ids = [int(r["id"]) for r in rows]
    with get_conn() as conn:
        tags_by_id = load_tags_bulk(conn, ids)
        deps_by_id = load_deps_bulk(conn, ids)
        context_by_id = load_context_bulk(conn, ids)
    return JSONResponse(
        [
            row_to_task(
                r,
                tags_by_id.get(int(r["id"]), []),
                deps_by_id.get(int(r["id"]), []),
                context_by_id.get(int(r["id"]), []),
            )
            for r in rows
        ]
    )


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


@app.get("/api/changes")
def api_changes() -> JSONResponse:
    """Cheap change token for the frontend live-update poll.

    Returns the DB file's modification time in nanoseconds. The CLI and the
    API both write straight to SQLite, so any mutation -- from either process
    -- bumps the file mtime (rollback-journal mode rewrites the main DB file on
    every commit). The UI polls this endpoint and only refetches the task list
    when the value changed, so a CLI-driven phase transition surfaces within
    one poll interval without the client repeatedly pulling the full list.

    NB: relies on rollback-journal mode (ntasker's default). Under WAL, commits
    land in the ``-wal`` sidecar and the main-file mtime would lag until a
    checkpoint -- ntasker does not enable WAL.
    """
    try:
        token = _db_module.DB_PATH.stat().st_mtime_ns
    except OSError:
        token = 0
    return JSONResponse({"v": token})


# NB: registered before ``/api/tasks/{task_id}`` so the literal "reorder"
# path isn't swallowed by the int path-param route (which would 422).
@app.patch("/api/tasks/reorder")
def api_reorder_tasks(payload: ReorderIn) -> JSONResponse:
    """Persist an explicit manual order for the given task ids.

    ``ids[0]`` becomes the topmost row (largest ``sort_order``), descending
    from there. The frontend calls this when the user drag-reorders while
    priority-sorted: it snapshots the displayed order (with the dragged task
    moved into its drop slot) so manual mode then shows exactly that
    arrangement. Ids not listed keep their current ``sort_order``.
    """
    ids = payload.ids
    n = len(ids)
    with get_conn() as conn:
        for i, tid in enumerate(ids):
            conn.execute(
                "UPDATE tasks SET sort_order = ? WHERE id = ?",
                (float(n - i), int(tid)),
            )
    return JSONResponse({"reordered": n})


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int) -> JSONResponse:
    """Single-task lookup. Used by FRIDAY for ``#<id>`` resolution."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=_("Task not found"))
        tags = load_tags_for(conn, task_id)
        depends = load_deps_for(conn, task_id)
        context = load_context_for(conn, task_id)
    return JSONResponse(row_to_task(row, tags, depends, context))


def _dep_error_detail(e: DepError) -> str:
    """Localized HTTP-400 message for a dependency validation failure."""
    if e.reason == "self":
        return _("A task cannot depend on itself.")
    if e.reason == "missing":
        return _("Dependency task #{id} does not exist.").format(id=e.ref)
    # cycle
    return _("That dependency would create a cycle (via task #{id}).").format(id=e.ref)


def _normalize_project(value: str | None) -> str | None:
    """Trim whitespace; convert empty / whitespace-only to NULL.

    Since v2.0 there is no projects whitelist -- any non-empty trimmed
    string is accepted and implicitly defines a project. NULL means
    "cross-project" (no project assigned), shown as ``__none__`` in the
    sidebar feed.
    """
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


@app.post("/api/tasks", status_code=201)
def api_create_task(request: Request, payload: TaskCreate) -> JSONResponse:
    if any(c.kind == "file" for c in payload.context):
        _require_local_origin(request)
    if payload.priority not in PRIORITY_VALID:
        raise HTTPException(status_code=400, detail=_("Invalid priority"))
    if payload.agent is not None and payload.agent not in AGENT_KEYS:
        raise HTTPException(status_code=400, detail=_("Invalid agent"))
    norm_tags = normalize_tags(payload.tags)
    phase_value = payload.phase or PHASE_DEFAULT
    project_value = _normalize_project(payload.project)
    # Title is optional: fall back to the start of the description.
    title_value = payload.title.strip() or title_from_description(payload.description)
    # Validate attachments up front -- a bad path aborts the create before
    # anything is written, so no half-created task is left behind.
    resolved_context = [_resolve_context_add(c) for c in payload.context]
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (project, title, description, phase, priority, agent,
                               sort_order)
            VALUES (?, ?, ?, ?, ?, ?,
                    (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tasks))
            """,
            (
                project_value,
                title_value,
                payload.description,
                phase_value,
                payload.priority,
                payload.agent,
            ),
        )
        new_id = int(cur.lastrowid)
        if norm_tags:
            set_task_tags(conn, new_id, norm_tags)
        dep_ids = normalize_dep_ids(payload.depends)
        if dep_ids:
            try:
                validate_deps(conn, new_id, dep_ids)
            except DepError as e:
                raise HTTPException(status_code=400, detail=_dep_error_detail(e))
            set_task_deps(conn, new_id, dep_ids)
        for kind, path, label, note in resolved_context:
            add_context(conn, new_id, kind, path, label, note)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()
        tags = load_tags_for(conn, new_id)
        depends = load_deps_for(conn, new_id)
        context = load_context_for(conn, new_id) if resolved_context else []
    return JSONResponse(row_to_task(row, tags, depends, context), status_code=201)


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, payload: TaskUpdate) -> JSONResponse:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail=_("No fields to update"))

    tags_raw = fields.pop("tags", None)
    deps_raw = fields.pop("depends", None)

    if "priority" in fields and fields["priority"] not in PRIORITY_VALID:
        raise HTTPException(status_code=400, detail=_("Invalid priority"))

    if "agent" in fields and fields["agent"] is not None and fields["agent"] not in AGENT_KEYS:
        raise HTTPException(status_code=400, detail=_("Invalid agent"))

    # phase is NOT NULL since v2.0: a legacy client trying to set phase=null
    # falls back to the canonical default rather than tripping the SQL
    # constraint. Unknown phase strings are rejected explicitly so callers
    # get a clean 400 instead of a 500 with an SQLite error.
    if "phase" in fields:
        if fields["phase"] is None:
            fields["phase"] = PHASE_DEFAULT
        elif fields["phase"] not in PHASE_VALID:
            raise HTTPException(status_code=400, detail=_("Invalid phase"))

    # Normalize project here too -- empty strings collapse to NULL so the
    # sidebar feed (DISTINCT-based) doesn't surface a phantom "" project.
    if "project" in fields:
        fields["project"] = _normalize_project(fields["project"])

    if "status" in fields:
        if fields["status"] == "done":
            fields["completed_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            fields["completed_at"] = None

    if "archived" in fields:
        fields["archived"] = 1 if fields["archived"] else 0

    with get_conn() as conn:
        # Title is optional: an emptied title falls back to the start of the
        # description -- the just-submitted one if present, else the stored
        # one. Mirrors the create endpoint.
        if "title" in fields and not (fields["title"] or "").strip():
            if "description" in fields:
                desc = fields["description"]
            else:
                cur = conn.execute("SELECT description FROM tasks WHERE id = ?", (task_id,))
                stored = cur.fetchone()
                desc = stored["description"] if stored else None
            fields["title"] = title_from_description(desc)

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

        if deps_raw is not None:
            dep_ids = normalize_dep_ids(deps_raw)
            try:
                validate_deps(conn, task_id, dep_ids)
            except DepError as e:
                raise HTTPException(status_code=400, detail=_dep_error_detail(e))
            set_task_deps(conn, task_id, dep_ids)

        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        tags = load_tags_for(conn, task_id)
        depends = load_deps_for(conn, task_id)
        context = load_context_for(conn, task_id)

    # The task is finished -- tear down its interactive Claude session, if any.
    if fields.get("status") == "done":
        stop_session(task_id)

    return JSONResponse(row_to_task(row, tags, depends, context))


def _resolve_context_add(payload: ContextAdd) -> tuple[str, str, str, str]:
    """Validate one attachment request; return ``(kind, path, label, note)``.

    The path is confined to the configured workspace roots -- the same
    boundary the file endpoints use. Without that check any caller could
    seed a task with a pointer to ``~/.ssh/id_rsa`` and have the agent
    briefing read it out at the next run. Shared by the attach endpoint
    and task creation so the two can never drift apart.
    """
    if payload.kind not in CONTEXT_KINDS:
        raise HTTPException(
            status_code=400,
            detail=_("Unknown context kind: {kind}").format(kind=payload.kind),
        )

    if payload.kind == "brain":
        # A JCBrain note lives on the server, not on disk: the only thing
        # to validate is the id's shape. Existence is not probed here --
        # attaching must work offline and the picker only offers ids the
        # server just returned. The label comes from the picker (the
        # thought's title); the bare id is the honest fallback.
        raw = payload.path.strip()
        tid = brain.thought_id(raw) if brain.is_brain_path(raw) else raw
        if not brain.is_valid_id(tid):
            raise HTTPException(
                status_code=400,
                detail=_("Not a JCBrain thought id: {value}").format(value=raw),
            )
        label = payload.label.strip() or f"JCBrain {tid[:8]}"
        return payload.kind, brain.thought_path(tid), label, payload.note.strip()
    if brain.is_brain_path(payload.path):
        raise HTTPException(
            status_code=400,
            detail=_("A brain:// path needs kind 'brain'."),
        )

    if payload.kind == "mcp":
        # An MCP server is referenced by the name of its entry in
        # ~/.claude.json; it must exist there now, otherwise the agent
        # would be told to use tools it cannot have.
        raw = payload.path.strip()
        name = raw[len(MCP_SCHEME) :] if raw.startswith(MCP_SCHEME) else raw
        name = name.strip()
        if not name or name not in _mcp_server_names():
            raise HTTPException(
                status_code=404,
                detail=_("No MCP server named {name} in ~/.claude.json.").format(name=name),
            )
        label = payload.label.strip() or name
        return payload.kind, f"{MCP_SCHEME}{name}", label, payload.note.strip()
    if payload.path.strip().startswith(MCP_SCHEME):
        raise HTTPException(
            status_code=400,
            detail=_("An mcp:// path needs kind 'mcp'."),
        )

    try:
        target = Path(
            os.path.expandvars(os.path.expanduser(payload.path.strip()))
        ).resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.kind == "file":
        # Any file or folder on this machine, named by the user explicitly
        # -- the one kind that is *not* confined to the workspace roots,
        # because "the spreadsheet on my Desktop" is exactly what it is
        # for. The endpoints that accept it require a local Origin (see
        # ``_require_local_origin``) so a foreign page cannot plant one.
        # The full name (with suffix) is the honest label: "report.pdf"
        # and "report.xlsx" side by side must stay tellable apart.
        if not payload.path.strip():
            raise HTTPException(status_code=400, detail=_("No such file or directory."))
        if not target.exists():
            raise HTTPException(status_code=404, detail=_("No such file or directory."))
        label = payload.label.strip() or target.name or str(target)
        return payload.kind, str(target), label, payload.note.strip()

    roots = _workspace_roots()
    if not roots or not workspace.within_roots(target, roots):
        raise HTTPException(
            status_code=403,
            detail=_("Path lies outside every configured workspace directory."),
        )
    if not target.exists():
        raise HTTPException(status_code=404, detail=_("No such file or directory."))

    # An empty label is the common case (the UI attaches straight from a
    # list where the file name *is* the label) -- derive it rather than
    # storing a blank the frontend would have to paper over.
    label = payload.label.strip() or target.stem
    return payload.kind, str(target), label, payload.note.strip()


def _mcp_server_names() -> set[str]:
    """Names of the MCP servers currently declared in ``~/.claude.json``."""
    return {s["name"] for s in workspace.scan_tooling().get("servers", [])}


def _require_task(conn, task_id: int) -> None:
    """404 unless the task exists."""
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=_("Task not found"))


@app.get("/api/tasks/{task_id}/context")
def api_list_context(task_id: int) -> JSONResponse:
    """The workspace files attached to one task."""
    with get_conn() as conn:
        _require_task(conn, task_id)
        return JSONResponse(load_context_for(conn, task_id))


@app.post("/api/tasks/{task_id}/context", status_code=201)
def api_add_context(request: Request, task_id: int, payload: ContextAdd) -> JSONResponse:
    """Attach a workspace file to a task.

    Validation (kind whitelist + workspace-root confinement) lives in
    :func:`_resolve_context_add`, shared with task creation. A ``file``
    attachment escapes the roots by design, so it must come from our own
    page (or a script with no Origin), never from a foreign site.
    """
    if payload.kind == "file":
        _require_local_origin(request)
    kind, path, label, note = _resolve_context_add(payload)
    with get_conn() as conn:
        _require_task(conn, task_id)
        entry = add_context(conn, task_id, kind, path, label, note)
    return JSONResponse(entry, status_code=201)


# ---------------------------------------------------------------------------
# JCBrain (OpenBrain) notes -- remote context source
# ---------------------------------------------------------------------------
#
# Thin proxy over the MCP server declared in ``~/.claude.json`` so the
# browser never sees the key and the ``/task`` loader can resolve a note
# without speaking MCP itself. Read-only: search + fetch, nothing else.


@app.get("/api/brain/status")
def api_brain_status() -> JSONResponse:
    """Whether a JCBrain server is configured (never returns the key)."""
    return JSONResponse(brain.status())


@app.get("/api/brain/search")
def api_brain_search(q: str = "") -> JSONResponse:
    """Semantic search; ``{query, results: [{id, title, url, path}]}``."""
    query = (q or "").strip()
    if not query:
        return JSONResponse({"query": "", "results": []})
    try:
        results = brain.search(query)
    except brain.BrainError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return JSONResponse({"query": query, "results": results})


@app.get("/api/brain/thoughts/{thought_id}")
def api_brain_thought(thought_id: str) -> JSONResponse:
    """One note in full -- the chip viewer and the loader read this."""
    if not brain.is_valid_id(thought_id):
        raise HTTPException(
            status_code=400,
            detail=_("Not a JCBrain thought id: {value}").format(value=thought_id),
        )
    try:
        doc = brain.fetch(thought_id)
    except brain.BrainError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return JSONResponse(doc)


@app.delete("/api/tasks/{task_id}/context/{context_id}", status_code=204)
def api_remove_context(task_id: int, context_id: int) -> None:
    """Detach a workspace file. The file itself is never touched."""
    with get_conn() as conn:
        _require_task(conn, task_id)
        if not remove_context(conn, task_id, context_id):
            raise HTTPException(status_code=404, detail=_("Attachment not found"))


@app.get("/api/tasks/{task_id}/context/{context_id}/file")
def api_context_file(task_id: int, context_id: int) -> JSONResponse:
    """Preview payload for one attachment's file, wherever it lives.

    The workspace preview refuses paths outside the configured roots; an
    attachment of kind ``file`` is outside them by definition. Here the
    authorisation is the attachment row itself -- the user put that exact
    path on this task -- so the preview reads what the row points at and
    nothing else (no ``path`` parameter to steer).
    """
    with get_conn() as conn:
        _require_task(conn, task_id)
        entry = next(
            (c for c in load_context_for(conn, task_id) if c["id"] == context_id), None
        )
    if entry is None:
        raise HTTPException(status_code=404, detail=_("Attachment not found"))
    if entry["remote"]:
        raise HTTPException(status_code=400, detail=_("This attachment is not a file."))
    try:
        return JSONResponse(workspace.describe_file(Path(entry["path"])))
    except workspace.PreviewError as exc:
        status = {"forbidden": 403, "not_found": 404, "too_large": 413}.get(
            exc.reason, 400
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/context/{context_id}/reveal")
def api_context_reveal(request: Request, task_id: int, context_id: int) -> JSONResponse:
    """Open one attachment's file in the desktop's default application."""
    _require_local_origin(request)
    with get_conn() as conn:
        _require_task(conn, task_id)
        entry = next(
            (c for c in load_context_for(conn, task_id) if c["id"] == context_id), None
        )
    if entry is None:
        raise HTTPException(status_code=404, detail=_("Attachment not found"))
    if entry["remote"]:
        raise HTTPException(status_code=400, detail=_("This attachment is not a file."))
    try:
        return JSONResponse(workspace.open_with_desktop(Path(entry["path"])))
    except workspace.WriteError as exc:
        raise _write_guard(exc) from exc


# ---------------------------------------------------------------------------
# Local filesystem helpers for "file" attachments
# ---------------------------------------------------------------------------


class PickRequest(BaseModel):
    """Options for the native file dialog."""

    folder: bool = False


@app.get("/api/fs/pick")
def api_fs_pick_available() -> JSONResponse:
    """Whether this machine can show a native file dialog (see POST)."""
    return JSONResponse({"available": workspace.picker_available()})


@app.post("/api/fs/pick")
def api_fs_pick(request: Request, payload: PickRequest) -> JSONResponse:
    """Open the OS file dialog on this desktop and return the chosen paths.

    Works because server and browser share a desktop: the page cannot learn
    a dropped file's path, but the local process can ask the OS. Blocks
    until the user picks or cancels (cancel = empty list). 501 when no
    dialog is available on this platform.
    """
    _require_local_origin(request)
    if not workspace.picker_available():
        raise HTTPException(
            status_code=501, detail=_("No native file dialog is available here.")
        )
    prompt = _("Choose a folder to attach") if payload.folder else _("Choose files to attach")
    try:
        paths = workspace.pick_paths(folder=payload.folder, prompt=prompt)
    except workspace.WriteError as exc:
        raise _write_guard(exc) from exc
    return JSONResponse({"paths": paths})


@app.get("/api/fs/places")
def api_fs_places(request: Request) -> JSONResponse:
    """Finder-style shortcuts for the file picker's browser."""
    _require_local_origin(request)
    places = workspace.fs_places(_workspace_roots())
    return JSONResponse([{**p, "name": _(p["name"])} for p in places])


@app.get("/api/fs/browse")
def api_fs_browse(
    request: Request,
    path: str = Query("", description="Directory to list; empty = home"),
) -> JSONResponse:
    """List any directory on this machine for the file picker.

    The workspace browser stops at the configured roots; this one is the
    Finder in the modal -- the whole point of a ``file`` attachment is
    reaching the spreadsheet on the Desktop. Same Origin rule as the
    other filesystem endpoints, and names only: contents still go
    through the attachment-row preview.
    """
    _require_local_origin(request)
    raw = path.strip() or "~"
    try:
        target = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return JSONResponse(workspace.list_directory(target))
    except workspace.PreviewError as exc:
        status = {"forbidden": 403, "not_found": 404, "too_large": 413}.get(
            exc.reason, 400
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.get("/api/fs/resolve")
def api_fs_resolve(
    request: Request,
    path: str = Query(..., description="A path as typed or pasted by the user"),
) -> JSONResponse:
    """Normalise a user-typed path and say whether it exists.

    Lets the picker validate a pasted path before it lands in a draft task
    (where nothing is sent to the server until Create). Only existence and
    the resolved form are reported -- never contents.
    """
    _require_local_origin(request)
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
        {
            "path": str(target),
            "name": target.name or str(target),
            "is_dir": target.is_dir(),
            "exists": True,
        }
    )


@app.delete("/api/tasks/{task_id}", status_code=204)
def api_delete_task(task_id: int) -> None:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=_("Task not found"))
