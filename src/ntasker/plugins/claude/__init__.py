"""Claude Code agent plugin.

Contributes the ``claude`` :class:`~ntasker.agents.AgentSpec`, its
permission-mode settings and its binary override. The run infrastructure
(PTY runner, ``/task`` installer, command templates) is core -- this
module only says *how* Claude is spawned and configured.

The spec is byte-compatible with the pre-plugin registry: same home, same
subdirs, same rendered command, so an existing ``~/.claude`` install never
shows spurious drift.
"""

from __future__ import annotations

from pathlib import Path

from ntasker.agents import AgentSpec
from ntasker.i18n import _, _lazy
from ntasker.plugins import PluginContext, PluginSpec
from ntasker.settings import (
    _FALSE_STRINGS,
    _TRUE_STRINGS,
    BIN_OVERRIDE_HINT,
    BIN_OVERRIDE_LABEL,
    get_setting,
    make_bin_validator,
)

SPEC = PluginSpec(
    name="claude",
    label=_lazy("Claude Code"),
    description=_lazy("Run tasks in Claude Code sessions (run button, /task command, skill)."),
    kind="agent",
)

# Permission modes a spawned ``claude`` session can launch in. The values are
# the literal ``--permission-mode`` choices the CLI accepts; ntasker exposes the
# four the user picks between (see :func:`claude_permission_mode` and the
# /settings selector). ``default`` is normal (Claude asks first);
# ``bypassPermissions`` skips every prompt and is the only dangerous one.
CLAUDE_PERMISSION_MODES = ("default", "auto", "plan", "bypassPermissions")
CLAUDE_PERMISSION_MODE_DEFAULT = "default"


def validate_claude_permission_mode(value: str) -> str:
    """Validator for the ``claude_permission_mode`` setting.

    One of :data:`CLAUDE_PERMISSION_MODES`; empty normalizes to the safe
    default. Read at spawn by :func:`claude_permission_mode`.
    """
    norm = (value or "").strip()
    if not norm:
        return CLAUDE_PERMISSION_MODE_DEFAULT
    if norm in CLAUDE_PERMISSION_MODES:
        return norm
    raise ValueError(
        _("claude_permission_mode must be one of {modes} (got {value!r}).").format(
            modes=", ".join(CLAUDE_PERMISSION_MODES), value=value
        )
    )


def validate_claude_auto_mode(value: str) -> str:
    """Validator for the legacy ``claude_auto_mode`` boolean setting.

    Superseded by ``claude_permission_mode`` (the /settings selector). Retained
    so a value stored by an older ntasker stays valid and readable: a legacy
    truthy value still maps to ``bypassPermissions`` in
    :func:`claude_permission_mode`. Normalizes truthy/falsy spellings to
    ``"true"`` / ``"false"``; rejects anything else.
    """
    norm = (value or "").strip().lower()
    if norm in _TRUE_STRINGS:
        return "true"
    if norm in _FALSE_STRINGS:
        return "false"
    raise ValueError(
        _("claude_auto_mode must be a yes/no value (got {value!r}).").format(value=value)
    )


def claude_permission_mode() -> str:
    """Resolved permission mode for interactive Claude sessions.

    Backs the /settings mode selector; read at session spawn through
    :meth:`AgentSpec.permission_args`. Returns one of
    :data:`CLAUDE_PERMISSION_MODES`, defaulting to ``"default"`` (safe, normal).

    Backward compatibility: the pre-selector ``claude_auto_mode`` boolean meant
    "skip every prompt", so a legacy truthy value maps to ``bypassPermissions``
    when no explicit ``claude_permission_mode`` is set.
    """
    raw = (get_setting("claude_permission_mode") or "").strip()
    if raw in CLAUDE_PERMISSION_MODES:
        return raw
    if (get_setting("claude_auto_mode") or "").strip().lower() in _TRUE_STRINGS:
        return "bypassPermissions"
    return CLAUDE_PERMISSION_MODE_DEFAULT


def _permission_args() -> list[str]:
    """CLI flags for the configured permission mode."""
    mode = claude_permission_mode()
    if mode == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    if mode in ("auto", "plan"):
        return ["--permission-mode", mode]
    return []


def register(ctx: PluginContext) -> None:
    ctx.add_agent(
        AgentSpec(
            key="claude",
            label="Claude Code",
            binary="claude",
            icon=ctx.static_url("claude.webp"),
            home_env="NTASKER_CLAUDE_HOME",
            default_home=Path.home() / ".claude",
            commands_subdir="commands",
            skills_subdir="skills/ntasker",
            command_template="task.md.template",
            helper_ref_dir="~/.claude/commands",
            seed_mode="positional",
            session_flag="--session-id",
            resume_flag="--resume",
            system_prompt_flag="--append-system-prompt",
            settings_flag="--settings",
            permission_args_fn=_permission_args,
        )
    )
    ctx.add_setting(
        "claude_permission_mode",
        validate_claude_permission_mode,
        _lazy(
            "Permission mode for interactive Claude sessions: 'default' (normal -- "
            "Claude asks first), 'auto' (auto-accept actions), 'plan' (plan only, no "
            "changes), or 'bypassPermissions' (skip every prompt -- dangerous). "
            "Passed to the CLI as --permission-mode. Default: default."
        ),
    )
    ctx.add_setting("claude_auto_mode", validate_claude_auto_mode)
    ctx.add_setting("claude_bin", make_bin_validator("claude"), BIN_OVERRIDE_HINT, BIN_OVERRIDE_LABEL)
