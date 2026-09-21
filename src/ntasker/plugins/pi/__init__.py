"""Pi (pi.dev) agent plugin: the ``pi`` spec and its binary override."""

from __future__ import annotations

from pathlib import Path

from ntasker.agents import AgentSpec
from ntasker.i18n import _lazy
from ntasker.plugins import PluginContext, PluginSpec
from ntasker.settings import (
    BIN_OVERRIDE_HINT,
    BIN_OVERRIDE_LABEL,
    MODEL_LABEL,
    make_bin_validator,
    make_model_validator,
)

SPEC = PluginSpec(
    name="pi",
    label=_lazy("Pi"),
    description=_lazy("Run tasks in Pi sessions (run button, /task prompt, skill)."),
    kind="agent",
)


def register(ctx: PluginContext) -> None:
    ctx.add_agent(
        AgentSpec(
            key="pi",
            label="Pi",
            binary="pi",
            icon=ctx.static_url("pi.svg"),
            home_env="PI_CODING_AGENT_DIR",
            default_home=Path.home() / ".pi" / "agent",
            commands_subdir="prompts",
            skills_subdir="skills/ntasker",
            command_template="task.generic.md.template",
            helper_ref_dir="~/.pi/agent/prompts",
            seed_mode="positional",
            system_prompt_flag="--append-system-prompt",
            extra_strip_env=("PI_CODING_AGENT", "PI_SESSION_ID"),
            # pi: no documented permission flag yet.
            model_flag="--model",
        )
    )
    ctx.add_setting("pi_bin", make_bin_validator("pi"), BIN_OVERRIDE_HINT, BIN_OVERRIDE_LABEL)
    ctx.add_setting(
        "pi_model",
        make_model_validator("pi"),
        _lazy(
            "Model for spawned Pi sessions: a pattern or id (e.g. sonnet, provider/id, "
            "optionally :<thinking>). Passed to the CLI as --model. Unset for the CLI default."
        ),
        MODEL_LABEL,
        suggestions=("haiku", "sonnet", "opus"),
    )
