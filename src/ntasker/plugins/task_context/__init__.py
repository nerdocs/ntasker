"""Task-context plugin: attach files, notes, personas, skills and MCP servers to a task.

Attachments are pointers, not copies -- the file stays the single source
of truth. The agent receives the list in its briefing at spawn (see
:func:`context_briefing`), which is the whole point: otherwise the user
re-types the same three paths every run.

Kinds (:data:`~ntasker.plugins.task_context.db.CONTEXT_KINDS`): ``skill``,
``note``, ``member`` and ``doc`` point into the workspace roots configured
for the workspace plugin (whose scanners this plugin imports; it does not
need that plugin to be *enabled*), ``file`` is any path on this machine,
``mcp`` names an MCP server from ``~/.claude.json``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable

from ntasker.i18n import _, _lazy
from ntasker.plugins import PluginContext, PluginSpec
from ntasker.plugins.task_context.db import (
    SCHEMA,
    add_context,
    load_context_bulk,
    load_context_for,
)
from ntasker.plugins.task_context.routes import ContextAdd, resolve_context_add, router

SPEC = PluginSpec(
    name="task_context",
    label=_lazy("Task context"),
    description=_lazy(
        "Attach files, notes, personas, skills and MCP servers to a task; "
        "the agent gets them in its briefing."
    ),
)


def context_briefing(context: list[dict]) -> list[str]:
    """Render attachments as briefing lines (Markdown). Empty list if none.

    The agent gets paths, not contents: a persona file or a knowledge-base
    note can be thousands of tokens, most of which the task will never
    need, and the agent has a Read tool. What it cannot guess is *which*
    files matter -- that is exactly what the user encoded by attaching them,
    so the list is stated as an instruction rather than as trivia.

    Attachments whose file has since moved are marked instead of dropped:
    an agent that knows a reference is dangling can say so, while a silently
    shortened list just looks like the user never attached anything.
    """
    if not context:
        return []
    labels = {
        "member": "Team persona",
        "skill": "Skill",
        "note": "Knowledge base",
        "doc": "Document",
        "file": "File",
        "mcp": "MCP server",
    }
    lines = [
        "",
        "## Attached context",
        "",
        "The user attached these workspace files to the task. Read the ones "
        "relevant to what you are doing before you start -- they carry the "
        "conventions, background and prior art this task depends on.",
        "",
    ]
    for entry in context:
        kind = labels.get(entry.get("kind", ""), "File")
        path = entry.get("path", "")
        line = f"- **{kind}: {entry.get('label') or '?'}** -- `{path}`"
        if not entry.get("exists", True):
            line += "  _(file not found -- mention this rather than guessing)_"
        lines.append(line)
        if entry.get("note"):
            lines.append(f"  - {entry['note']}")
        if entry.get("kind") == "member":
            lines.append(
                "  - This is a Claude Code subagent definition (frontmatter `name`): "
                "delegate to it via the Agent tool rather than reading it as prose."
            )
        if entry.get("kind") == "file":
            if os.path.isdir(path):
                lines.append("  - This is a folder: list it first, then read what the task needs.")
            else:
                lines.append(
                    "  - Read this file with your Read tool before you rely on it "
                    "(PDFs and images included)."
                )
        if entry.get("kind") == "mcp":
            lines.append(
                "  - Use the tools of this MCP server for the task (ToolSearch by its "
                "name if they are deferred). If the server is not connected, say so."
            )
    return lines


def _task_hook(conn: sqlite3.Connection, tasks: list[dict]) -> None:
    by_id = load_context_bulk(conn, [int(t["id"]) for t in tasks])
    for t in tasks:
        t["context"] = by_id.get(int(t["id"]), [])


def _create_hook(payload: dict) -> Callable[[sqlite3.Connection, int], None]:
    # Validate up front -- a bad path aborts the create before anything is
    # written, so no half-created task is left behind.
    resolved = [resolve_context_add(ContextAdd(**c)) for c in payload.get("context") or []]

    def after(conn: sqlite3.Connection, task_id: int) -> None:
        for kind, path, label, note in resolved:
            add_context(conn, task_id, kind, path, label, note)

    return after


def _briefing(task_id: int) -> list[str]:
    from ntasker.db import get_conn  # noqa: PLC0415 -- lazy: avoid cycle at import

    try:
        with get_conn() as conn:
            context = load_context_for(conn, task_id)
    except Exception:  # noqa: BLE001 -- a briefing must never block a spawn
        return []
    return context_briefing(context)


def _js_strings() -> dict[str, str]:
    return {
        "ctx_attached": _("Attached context"),
        "ctx_attach": _("Attach"),
        "ctx_attach_context": _("Attach context"),
        "ctx_attach_failed": _("Could not attach it."),
        "ctx_detach_failed": _("Could not detach it."),
        "ctx_detach": _("Detach"),
        "ctx_no_context": _("Nothing attached yet."),
        "ctx_missing": _("This file no longer exists at that path."),
        "ctx_note_placeholder": _("Why is this attached? (optional)"),
        "ctx_files": _("Files"),
        "ctx_mcp": _("MCP servers"),
        "ctx_search": _("Search..."),
        "ctx_no_match": _("Nothing matches your search."),
        "ctx_file_hint": _(
            "Any file or folder on this machine. The agent reads it at the start of the run."
        ),
        "ctx_file_choose": _("Choose files..."),
        "ctx_file_choose_folder": _("Choose a folder..."),
        "ctx_file_picking": _("Waiting for the file dialog..."),
        "ctx_or_path": _("...or paste a path:"),
        "ctx_file_path_placeholder": _("/path/to/file-or-folder (one per line)"),
        "ctx_file_add": _("Add"),
        "ctx_file_none": _("No files attached yet."),
        "ctx_file_pick_failed": _("Could not open the file dialog."),
        "ctx_preview_after_create": _("Create the task first, then open its attachments."),
        "ctx_open_failed": _("Could not open it."),
        "ctx_mcp_runtime_missing": _("runtime missing"),
        "ctx_mcp_hint": _("Use the tools of the MCP server {name} for this task."),
    }


def register(ctx: PluginContext) -> None:
    ctx.add_schema(SCHEMA)
    ctx.add_router(router)
    ctx.add_task_hook(_task_hook)
    ctx.add_create_hook(_create_hook)
    ctx.add_briefing(_briefing)
    ctx.add_js_strings(_js_strings)
    ctx.add_template_slot("head", "task_context/templates/head.html")
    ctx.add_template_slot("task_form", "task_context/templates/task_form.html")
    ctx.add_template_slot("task_edit", "task_context/templates/task_edit.html")
    ctx.add_template_slot("task_card", "task_context/templates/task_card.html")
    ctx.add_template_slot("board_card", "task_context/templates/task_card.html")
    ctx.add_template_slot("modals", "task_context/templates/modals.html")
    ctx.add_template_slot("scripts", "task_context/templates/scripts.html")
