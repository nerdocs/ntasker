"""Workspace plugin: the working environment around the tasks.

Four configurable directories -- team (Claude Code subagents), skills, a
Markdown knowledge base and generated documents -- get a ``/workspace``
page, collapsible sidebar sections and a file viewer with edit / rename /
create / delete-to-trash. Every filesystem operation is confined to those
roots (:mod:`ntasker.plugins.workspace.scan`); nothing is ever destroyed
outright.

The scanners in :mod:`.scan` are also imported by the task-context plugin
(attachment kinds ``member`` / ``skill`` / ``note`` / ``doc`` point into
these roots); that plugin works with this one disabled.
"""

from __future__ import annotations

import os

from ntasker.i18n import _, _lazy
from ntasker.plugins import PluginContext, PluginSpec
from ntasker.plugins.workspace.routes import router
from ntasker.settings import Validator

SPEC = PluginSpec(
    name="workspace",
    label=_lazy("Workspace"),
    description=_lazy(
        "Team, skills, knowledge base and documents: a /workspace page, sidebar "
        "sections and a file viewer with editing."
    ),
    icon="ti-layout-grid",
)


def _make_dir_validator(key: str) -> Validator:
    """Build the validator for one ``workspace_*_dir`` setting.

    Like ``projects_base``, ``~`` is stored verbatim and expanded per-machine
    at read time, so a settings DB stays portable between hosts. Existence
    is deliberately NOT enforced: a path on an external drive or a
    cloud-synced folder may be temporarily absent, and rejecting the write
    would strand the user. The scanners report ``exists: false`` instead.
    """

    def _validate(value: str) -> str:
        norm = (value or "").strip()
        if not norm:
            raise ValueError(_("{key} must not be empty -- unset it to clear.").format(key=key))
        if not os.path.isabs(os.path.expanduser(norm)):
            raise ValueError(
                _("{key} must be an absolute path (got {value!r}).").format(key=key, value=value)
            )
        return norm

    return _validate


def _js_strings() -> dict[str, str]:
    return {
        "workspace": _("Workspace"),
        "ws_skills": _("Skills"),
        "ws_knowledge": _("Knowledge base"),
        "ws_team": _("Team"),
        "ws_documents": _("Documents"),
        "ws_tooling": _("Tooling"),
        "ws_not_configured": _("Not configured"),
        "ws_configure_hint": _("Set this directory in the settings to fill this section."),
        "ws_open_settings": _("Open settings"),
        "ws_missing_dir": _("The configured directory does not exist:"),
        # Counts carry their noun so the msgid stays unambiguous.
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
        "ws_copy_text": _("Copy text"),
        "ws_text_copied": _("Text copied"),
        "ws_export_docx": _("Export to Word"),
        "ws_export_failed": _("Could not export the file."),
        "ws_close": _("Close"),
        "ws_loading": _("Loading..."),
        "ws_mcp_servers": _("MCP servers"),
        "ws_runtimes": _("Runtimes"),
        "ws_available": _("Available"),
        "ws_unavailable": _("Not found"),
        "ws_runtime_missing": _("This server cannot start -- its runtime is not on PATH."),
        "ws_transport": _("Transport"),
        "ws_secret_inline": _("Value stored in the config file"),
        "ws_secret_env": _("Read from an environment variable"),
        "ws_secret_empty": _("Empty value"),
        "ws_config_missing": _("No Claude Code config found at"),
        "ws_plugin": _("Plugin"),
        "ws_readonly": _("read-only"),
        "ws_readonly_hint": _("Linked in from outside the skills directory -- viewable there, not here."),
        "ws_bundles": _("Bundles:"),
        # Editing, browsing
        "ws_edit": _("Edit"),
        "ws_view": _("View"),
        "ws_save": _("Save"),
        "ws_cancel": _("Cancel"),
        "ws_create": _("Create"),
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
        "ws_field_new": _("New field"),
        "ws_field_key": _("Key, e.g. tools"),
        "ws_field_add": _("Add"),
        "ws_field_remove": _("Remove field"),
        "ws_saved": _("Saved"),
        "ws_and_more": _("{n} more..."),
        "ws_confirm_delete": _("Move {name} to the trash?"),
        "ws_trashed_os": _("{name} moved to the trash."),
        "ws_trashed_folder": _("{name} moved to the .ntasker-trash folder."),
        "ws_save_failed": _("Could not save the file."),
        "ws_delete_failed": _("Could not delete it."),
        "ws_rename_failed": _("Could not rename it."),
        "ws_create_failed": _("Could not create it."),
        "ws_open_failed": _("Could not open it."),
        # Viewer for MCP attachments
        "ws_mcp_transport": _("Transport"),
        "ws_mcp_command": _("Command"),
        "ws_mcp_runtime_missing": _("runtime missing"),
        "ws_mcp_gone": _("This server is no longer declared in ~/.claude.json."),
        "ws_file_preview_after_create": _("Create the task first -- then the file opens from here."),
    }


def register(ctx: PluginContext) -> None:
    ctx.add_router(router)
    ctx.add_setting(
        "workspace_skills_dir",
        _make_dir_validator("workspace_skills_dir"),
        label=_lazy("Skills directory"),
        hint=_lazy(
            "Directory holding your Claude Code skills, shown on the Workspace "
            "page with a load/broken verdict per skill. Defaults to "
            "~/.claude/skills when unset."
        ),
    )
    ctx.add_setting(
        "workspace_team_dir",
        _make_dir_validator("workspace_team_dir"),
        label=_lazy("Team directory"),
        hint=_lazy(
            "Directory of Claude Code subagent files (one Markdown file per agent "
            "with name/description front matter). Defaults to ~/.claude/agents "
            "when unset."
        ),
    )
    ctx.add_setting(
        "workspace_wiki_dir",
        _make_dir_validator("workspace_wiki_dir"),
        label=_lazy("Knowledge base"),
        hint=_lazy(
            "Root of your Markdown knowledge base (e.g. an Obsidian vault). The "
            "Workspace page lists its areas with note counts and links straight "
            "into Obsidian. Unset hides the card."
        ),
    )
    ctx.add_setting(
        "workspace_docs_dir",
        _make_dir_validator("workspace_docs_dir"),
        label=_lazy("Documents directory"),
        hint=_lazy(
            "Output folder for generated documents (offers, reviews, exports, "
            "plans -- ~/.claude/plans is a natural pick). The Workspace page "
            "lists them newest-first and previews Markdown, text and CSV files "
            "in place. Unset hides the card."
        ),
    )
    ctx.add_js_strings(_js_strings)
    ctx.add_template_slot("head", "workspace/templates/head.html")
    ctx.add_template_slot("sidebar", "workspace/templates/sidebar.html")
    ctx.add_template_slot("modals", "workspace/templates/modals.html")
    ctx.add_template_slot("scripts", "workspace/templates/scripts.html")
