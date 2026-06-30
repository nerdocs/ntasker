"""Agent registry -- the single source of truth for the AI coding agents
ntasker can launch and integrate with.

ntasker is agent-agnostic: each task carries an ``agent`` (Claude Code,
OpenCode or Pi), and the run button, the spawned PTY session, and the
installed ``/task`` slash-command + skill all follow that choice. Every
agent-specific difference -- the binary name, how a session is spawned,
where its config home lives, which icon the button shows, how the
``/task`` integration is installed -- is captured here as one
:class:`AgentSpec` per agent.

Adding a fourth agent is a single :data:`AGENTS` entry plus (for the
``/task`` integration) a command template under ``claude_assets/command/``.
No other module hard-codes an agent name.

Design notes:

* Settings are read lazily inside the spawn helpers (not at import time)
  so this module stays import-cheap and free of cycles -- ``settings``
  imports ``db``/``assets``; ``agents`` is imported by the runner and the
  app, which must not pull settings at module load.
* The Claude spec is deliberately byte-compatible with the pre-multi-agent
  installer: same home, same subdirs, same rendered command -- so an
  existing ``~/.claude`` install never shows spurious drift.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Environment markers stripped from *every* spawned agent so a session
# always starts as a fresh top-level run rather than a nested child of
# whatever launched the ntasker server. Claude markers dominate the list
# (ntasker historically only ran Claude); the generic ``AI_AGENT`` marker
# is shared. Per-agent extras are merged in via :attr:`AgentSpec.strip_env`.
_BASE_STRIP_ENV: tuple[str, ...] = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_DISABLE_MOUSE",
    "AI_AGENT",
    "CLAUDE_EFFORT",
)


@dataclass(frozen=True)
class AgentSpec:
    """Everything ntasker needs to know about one AI coding agent.

    One instance per supported agent, registered in :data:`AGENTS`.
    """

    key: str
    """Stable identifier persisted in ``tasks.agent`` (``claude`` etc.)."""

    label: str
    """Human-facing name shown in the UI (``Claude Code``)."""

    binary: str
    """CLI executable looked up on ``PATH`` for availability + spawn."""

    icon: str
    """Static asset filename for the run button (``claude.webp``)."""

    # --- asset installer (``/task`` slash command + SKILL.md) -------------
    home_env: str
    """ENV var that overrides :attr:`default_home` for this agent."""

    default_home: Path
    """Default config home where commands + skills are installed."""

    commands_subdir: str
    """Where the slash-command file lands, relative to the home."""

    skills_subdir: str
    """Where ``SKILL.md`` lands (incl. the ``ntasker`` folder), relative to home."""

    command_template: str
    """Packaged template filename under ``claude_assets/command/``."""

    helper_ref_dir: str
    """Literal ``~``-form dir the rendered command points the helper at."""

    # --- runner ----------------------------------------------------------
    seed_mode: str
    """How the ``/task <id>`` seed is handed to the CLI: ``positional`` or ``prompt-flag``."""

    extra_strip_env: tuple[str, ...] = field(default_factory=tuple)
    """Agent-specific nesting markers, merged with :data:`_BASE_STRIP_ENV`."""

    @property
    def strip_env(self) -> tuple[str, ...]:
        """Full set of env vars to strip before spawning this agent."""
        return _BASE_STRIP_ENV + self.extra_strip_env

    def permission_args(self) -> list[str]:
        """Agent-specific permission/auto-approve CLI flags (settings-driven)."""
        from ntasker import settings  # noqa: PLC0415 -- lazy: avoid import cycle

        if self.key == "claude":
            mode = settings.claude_permission_mode()
            if mode == "bypassPermissions":
                return ["--dangerously-skip-permissions"]
            if mode in ("auto", "plan"):
                return ["--permission-mode", mode]
            return []
        if self.key == "opencode":
            return ["--auto"] if settings.get_opencode_auto() else []
        # pi: no documented permission flag yet.
        return []

    @property
    def bin_setting_key(self) -> str:
        """Settings key overriding this agent's binary path (``<key>_bin``)."""
        return f"{self.key}_bin"

    @property
    def bin_env_var(self) -> str:
        """ENV var overriding this agent's binary path (``NTASKER_<KEY>_BIN``)."""
        return f"NTASKER_{self.key.upper()}_BIN"

    def build_spawn(self, seed: str | None) -> list[str]:
        """Full argv for an interactive session, incl. permission flags + seed.

        The working directory is set by the caller via the subprocess ``cwd``
        (uniform across all three agents -- pi has no ``--dir`` flag), so the
        seed is the only agent-specific tail handled here. argv[0] is the
        resolved binary (a configured override, else the bare name on PATH).
        """
        args = [resolve_binary(self) or self.binary, *self.permission_args()]
        if seed:
            if self.seed_mode == "prompt-flag":
                args.extend(["--prompt", seed])
            else:  # positional
                args.append(seed)
        return args


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_HOME = Path.home()

AGENTS: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        key="claude",
        label="Claude Code",
        binary="claude",
        icon="claude.webp",
        home_env="NTASKER_CLAUDE_HOME",
        default_home=_HOME / ".claude",
        commands_subdir="commands",
        skills_subdir="skills/ntasker",
        command_template="task.md.template",
        helper_ref_dir="~/.claude/commands",
        seed_mode="positional",
    ),
    "opencode": AgentSpec(
        key="opencode",
        label="OpenCode",
        binary="opencode",
        icon="opencode.svg",
        home_env="OPENCODE_CONFIG_DIR",
        default_home=_HOME / ".config" / "opencode",
        commands_subdir="command",
        skills_subdir="skills/ntasker",
        command_template="task.generic.md.template",
        helper_ref_dir="~/.config/opencode/command",
        seed_mode="prompt-flag",
        extra_strip_env=("OPENCODE", "OPENCODE_BIN_PATH"),
    ),
    "pi": AgentSpec(
        key="pi",
        label="Pi",
        binary="pi",
        icon="pi.svg",
        home_env="PI_CODING_AGENT_DIR",
        default_home=_HOME / ".pi" / "agent",
        commands_subdir="prompts",
        skills_subdir="skills/ntasker",
        command_template="task.generic.md.template",
        helper_ref_dir="~/.pi/agent/prompts",
        seed_mode="positional",
        extra_strip_env=("PI_CODING_AGENT", "PI_SESSION_ID"),
    ),
}

#: Ordered list of agent keys -- drives validation whitelists + UI order.
AGENT_KEYS: tuple[str, ...] = tuple(AGENTS.keys())

#: Fallback agent when a task has none and no ``default_agent`` is set.
DEFAULT_AGENT = "claude"


def get_spec(key: str | None) -> AgentSpec:
    """Return the :class:`AgentSpec` for ``key``, falling back to the default.

    An unknown / ``None`` key degrades to :data:`DEFAULT_AGENT` rather than
    raising, so a stale ``tasks.agent`` value can never break a run.
    """
    if key and key in AGENTS:
        return AGENTS[key]
    return AGENTS[DEFAULT_AGENT]


def resolve_agent_key(task_agent: str | None) -> str:
    """Resolve the effective agent key for a task.

    Precedence: the task's own ``agent`` -> the ``default_agent`` setting ->
    :data:`DEFAULT_AGENT`. Unknown values are dropped at each step.
    """
    if task_agent and task_agent in AGENTS:
        return task_agent
    from ntasker.settings import get_default_agent  # noqa: PLC0415

    return get_default_agent()


def resolve_binary(spec: AgentSpec) -> str | None:
    """Resolve the agent's runnable binary, or ``None`` if not found.

    Precedence: the ``<key>_bin`` setting (ENV ``NTASKER_<KEY>_BIN`` first) ->
    ``PATH`` lookup of the bare binary name. The override exists because the
    ntasker server may run with a narrower ``PATH`` than the user's interactive
    shell (e.g. a systemd unit without ``nvm`` / ``~/.opencode/bin``): point it
    at the absolute path and runs work again.

    An override containing a ``/`` is treated as a path (expanded, must be an
    executable file); a bare name is looked up on ``PATH``. A configured value
    that does not resolve yields ``None`` (the agent reports unavailable).
    """
    from ntasker.settings import get_setting  # noqa: PLC0415 -- lazy: avoid cycle

    try:
        raw = get_setting(spec.bin_setting_key, env_var=spec.bin_env_var)
    except Exception:  # noqa: BLE001 -- a DB hiccup must not break detection
        raw = None
    if raw and raw.strip():
        cand = os.path.expanduser(raw.strip())
        if "/" in cand:
            return cand if (os.path.isfile(cand) and os.access(cand, os.X_OK)) else None
        return shutil.which(cand)
    return shutil.which(spec.binary)


def agent_available(spec: AgentSpec) -> bool:
    """Whether the agent's CLI binary resolves (override or on ``PATH``)."""
    return resolve_binary(spec) is not None


def resolve_home(spec: AgentSpec, override: str | os.PathLike | None = None) -> Path:
    """Resolve an agent's config home.

    Precedence: explicit ``override`` > the agent's ``home_env`` ENV var >
    :attr:`AgentSpec.default_home`. Expanded + absolutised; symlinks are kept
    (users expect to write through the logical path, e.g. a dotfiles symlink).
    """
    if override is not None:
        raw = str(override)
    else:
        raw = os.environ.get(spec.home_env, str(spec.default_home))
    return Path(os.path.abspath(os.path.expanduser(raw)))
