"""OpenCode agent plugin: the ``opencode`` spec, its ``--auto`` switch and binary override."""

from __future__ import annotations

from pathlib import Path

from ntasker.agents import AgentSpec
from ntasker.i18n import _, _lazy
from ntasker.plugins import PluginContext, PluginSpec
from ntasker.settings import (
    _FALSE_STRINGS,
    _TRUE_STRINGS,
    BIN_OVERRIDE_HINT,
    get_setting,
    make_bin_validator,
)

SPEC = PluginSpec(
    name="opencode",
    label=_lazy("OpenCode"),
    description=_lazy("Run tasks in OpenCode sessions (run button, /task command, skill)."),
    kind="agent",
)


def validate_opencode_auto(value: str) -> str:
    """Validator for the ``opencode_auto`` boolean setting.

    When truthy, a spawned OpenCode session runs with ``--auto`` (it
    auto-approves its own actions). Normalizes truthy/falsy spellings to
    ``"true"`` / ``"false"``; rejects anything else.
    """
    norm = (value or "").strip().lower()
    if norm in _TRUE_STRINGS:
        return "true"
    if norm in _FALSE_STRINGS:
        return "false"
    raise ValueError(
        _("opencode_auto must be a yes/no value (got {value!r}).").format(value=value)
    )


def get_opencode_auto() -> bool:
    """Whether spawned OpenCode sessions run with ``--auto``. Defaults to False."""
    raw = get_setting("opencode_auto", env_var="NTASKER_OPENCODE_AUTO")
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_STRINGS


def _permission_args() -> list[str]:
    return ["--auto"] if get_opencode_auto() else []


def register(ctx: PluginContext) -> None:
    ctx.add_agent(
        AgentSpec(
            key="opencode",
            label="OpenCode",
            binary="opencode",
            icon=ctx.static_url("opencode.svg"),
            home_env="OPENCODE_CONFIG_DIR",
            default_home=Path.home() / ".config" / "opencode",
            commands_subdir="command",
            skills_subdir="skills/ntasker",
            command_template="task.generic.md.template",
            helper_ref_dir="~/.config/opencode/command",
            seed_mode="prompt-flag",
            extra_strip_env=("OPENCODE", "OPENCODE_BIN_PATH"),
            permission_args_fn=_permission_args,
        )
    )
    ctx.add_setting(
        "opencode_auto",
        validate_opencode_auto,
        _lazy(
            "Run spawned OpenCode sessions with --auto (auto-approve actions). "
            "Yes/no, default no."
        ),
    )
    ctx.add_setting("opencode_bin", make_bin_validator("opencode"), BIN_OVERRIDE_HINT)
