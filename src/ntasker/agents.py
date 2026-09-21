"""Agent registry -- the single source of truth for the AI coding agents
ntasker can launch and integrate with.

ntasker is agent-agnostic: each task carries an ``agent`` (Claude Code,
OpenCode or Pi), and the run button, the spawned PTY session, and the
installed ``/task`` slash-command + skill all follow that choice. Every
agent-specific difference -- the binary name, how a session is spawned,
where its config home lives, which icon the button shows, how the
``/task`` integration is installed -- is captured here as one
:class:`AgentSpec` per agent.

Agents are contributed by plugins (``ntasker/plugins/<key>/``): each
plugin's ``register()`` adds one :class:`AgentSpec` to :data:`AGENTS`, plus
its own settings keys. Adding a fourth agent is a new plugin package plus
(for the ``/task`` integration) a command template under
``claude_assets/command/``. No other module hard-codes an agent name, and
a disabled plugin's agent is neither listed nor resolvable.

Design notes:

* Settings are read lazily inside the spawn helpers (not at import time)
  so this module stays import-cheap and free of cycles -- ``settings``
  imports ``db``/``assets``; ``agents`` is imported by the runner and the
  app, which must not pull settings at module load.
* The registry fills itself on first use: every accessor calls
  :func:`_ensure_loaded`, which runs :func:`ntasker.plugins.load_all`.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
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

    session_flag: str | None = None
    """CLI flag that forces a specific session id at spawn (``--session-id``),
    or ``None`` if the agent has no such capability. When set, ntasker mints a
    session id, hands it over here, and persists it so the run can be resumed."""

    resume_flag: str | None = None
    """CLI flag that resumes a stored session by id (``--resume``), or ``None``.
    Requires :attr:`session_flag` to be meaningful (an id must have been forced
    and persisted first)."""

    system_prompt_flag: str | None = None
    """CLI flag that appends to the agent's system prompt
    (``--append-system-prompt``), or ``None`` if it has no such capability.
    Used to brief a session without putting anything in the *user* prompt --
    see the quick run in :mod:`ntasker.claude_runner`."""

    settings_flag: str | None = None
    """CLI flag that loads an extra settings file for the session
    (``--settings``), or ``None``. ntasker passes its hooks file through it --
    see :func:`ntasker.claude_assets.hooks_settings_path`."""

    extra_strip_env: tuple[str, ...] = field(default_factory=tuple)
    """Agent-specific nesting markers, merged with :data:`_BASE_STRIP_ENV`."""

    permission_args_fn: Callable[[], list[str]] | None = None
    """Settings-driven permission / auto-approve flags, supplied by the agent's
    plugin (``None`` = the agent has no such flags)."""

    model_flag: str | None = None
    """CLI flag that picks the model for the session (``--model``), or
    ``None``. Its value comes from the ``<key>_model`` setting (ENV
    ``NTASKER_<KEY>_MODEL`` first); unset = the CLI's own default."""

    @property
    def strip_env(self) -> tuple[str, ...]:
        """Full set of env vars to strip before spawning this agent."""
        return _BASE_STRIP_ENV + self.extra_strip_env

    def permission_args(self) -> list[str]:
        """Agent-specific permission/auto-approve CLI flags (settings-driven)."""
        return self.permission_args_fn() if self.permission_args_fn else []

    def model_args(self) -> list[str]:
        """``[model_flag, <model>]`` when a model is configured, else ``[]``."""
        if not self.model_flag:
            return []
        from ntasker.settings import get_setting  # noqa: PLC0415 -- lazy: avoid cycle

        model = (get_setting(self.model_setting_key, env_var=self.model_env_var) or "").strip()
        return [self.model_flag, model] if model else []

    @property
    def model_setting_key(self) -> str:
        """Settings key holding this agent's model (``<key>_model``)."""
        return f"{self.key}_model"

    @property
    def model_env_var(self) -> str:
        """ENV var overriding this agent's model (``NTASKER_<KEY>_MODEL``)."""
        return f"NTASKER_{self.key.upper()}_MODEL"

    @property
    def bin_setting_key(self) -> str:
        """Settings key overriding this agent's binary path (``<key>_bin``)."""
        return f"{self.key}_bin"

    @property
    def bin_env_var(self) -> str:
        """ENV var overriding this agent's binary path (``NTASKER_<KEY>_BIN``)."""
        return f"NTASKER_{self.key.upper()}_BIN"

    def build_spawn(
        self,
        seed: str | None,
        *,
        session_id: str | None = None,
        resume_id: str | None = None,
        system_prompt: str | None = None,
        settings_path: str | None = None,
    ) -> list[str]:
        """Full argv for an interactive session, incl. permission/model flags + seed.

        The working directory is set by the caller via the subprocess ``cwd``
        (uniform across all three agents -- pi has no ``--dir`` flag), so the
        seed is the only agent-specific tail handled here. argv[0] is the
        resolved binary (a configured override, else the bare name on PATH).

        ``resume_id`` reopens a stored session (:attr:`resume_flag`) -- the
        conversation is already there, so the ``seed`` is ignored. Otherwise
        ``session_id`` forces a fresh session's id (:attr:`session_flag`) so
        ntasker can persist it and resume the run later. ``system_prompt`` is
        appended to the agent's system prompt (:attr:`system_prompt_flag`) --
        a briefing that leaves the user prompt untouched. ``settings_path`` is
        an extra settings file (:attr:`settings_flag`) -- ntasker's hooks. All
        four are no-ops on an agent that lacks the corresponding flag.
        """
        args = [resolve_binary(self) or self.binary, *self.permission_args(), *self.model_args()]
        if settings_path and self.settings_flag:
            args.extend([self.settings_flag, settings_path])
        if resume_id and self.resume_flag:
            args.extend([self.resume_flag, resume_id])
            return args  # resuming replays the conversation -- no seed
        if session_id and self.session_flag:
            args.extend([self.session_flag, session_id])
        if system_prompt and self.system_prompt_flag:
            args.extend([self.system_prompt_flag, system_prompt])
        if seed:
            if self.seed_mode == "prompt-flag":
                args.extend(["--prompt", seed])
            else:  # positional
                args.append(seed)
        return args


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: key -> spec, in plugin load order. Filled by :func:`ntasker.plugins.load_all`
#: (every registered agent, enabled or not); read through the accessors below.
AGENTS: dict[str, AgentSpec] = {}


def _ensure_loaded() -> None:
    """Register the agent plugins on first use (idempotent, lazy import)."""
    from ntasker import plugins  # noqa: PLC0415 -- lazy: plugins import settings

    plugins.load_all()


def agent_keys() -> tuple[str, ...]:
    """Keys of the *enabled* agents, in load order -- validation whitelist + UI order."""
    _ensure_loaded()
    from ntasker import plugins  # noqa: PLC0415

    off = plugins.disabled_plugins()
    return tuple(key for key in AGENTS if key not in off)


def enabled_agents() -> list[AgentSpec]:
    """Specs of the enabled agents, in load order."""
    return [AGENTS[key] for key in agent_keys()]


def default_agent_key() -> str:
    """Fallback agent when a task has none and no ``default_agent`` is set.

    ``claude`` while its plugin is enabled, else the first enabled agent.
    """
    keys = agent_keys()
    return "claude" if "claude" in keys else keys[0]


def get_spec(key: str | None) -> AgentSpec:
    """Return the :class:`AgentSpec` for ``key``, falling back to the default.

    An unknown / ``None`` / disabled key degrades to :func:`default_agent_key`
    rather than raising, so a stale ``tasks.agent`` value can never break a run.
    """
    if key and key in agent_keys():
        return AGENTS[key]
    return AGENTS[default_agent_key()]


def resolve_agent_key(task_agent: str | None) -> str:
    """Resolve the effective agent key for a task.

    Precedence: the task's own ``agent`` -> the ``default_agent`` setting ->
    :func:`default_agent_key`. Unknown or disabled values are dropped at each
    step.
    """
    if task_agent and task_agent in agent_keys():
        return task_agent
    from ntasker.settings import get_default_agent  # noqa: PLC0415

    return get_default_agent()


def resolve_binary(spec: AgentSpec) -> str | None:
    """Resolve the agent's runnable binary, or ``None`` if not found.

    Precedence: the ``<key>_bin`` setting (ENV ``NTASKER_<KEY>_BIN`` first) ->
    ``PATH`` lookup of the bare binary name -> :func:`well_known_bin_dirs`.
    The fallback exists because the ntasker server may run with a narrower
    ``PATH`` than the user's interactive shell (e.g. a systemd unit or
    launchd agent without ``nvm`` / ``~/.opencode/bin``); the override covers
    installs outside those conventional places.

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
    found = shutil.which(spec.binary)
    if found:
        return found
    for d in well_known_bin_dirs():
        cand = os.path.join(d, spec.binary)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def well_known_bin_dirs(home: str | os.PathLike | None = None) -> list[str]:
    """Directories where agent CLIs conventionally land, best first.

    Covers the native installers (``~/.local/bin``, ``~/.claude/local``,
    ``~/.opencode/bin``), Homebrew on macOS, ``/usr/local/bin``, and every
    nvm-managed Node -- the nvm ``default`` alias first, then the remaining
    versions newest first, so a freshly installed Node wins over a stale one.
    """
    home = Path(home or Path.home())
    dirs = [
        home / ".local" / "bin",
        home / ".claude" / "local",
        home / ".opencode" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    nvm = home / ".nvm"
    versions = nvm / "versions" / "node"
    if versions.is_dir():
        default = _nvm_default_version(nvm)
        node_dirs = sorted(
            (d for d in versions.iterdir() if d.is_dir()),
            key=lambda d: _version_key(d.name),
            reverse=True,
        )
        if default:
            node_dirs.sort(
                key=lambda d: not (d.name == default or d.name.startswith(default + "."))
            )
        dirs.extend(d / "bin" for d in node_dirs)
    return [str(d) for d in dirs]


def _nvm_default_version(nvm: Path) -> str | None:
    """The nvm ``default`` alias as a ``vN[.N.N]`` prefix, or ``None``.

    The alias file holds e.g. ``24``, ``v24.14.1`` or ``lts/*``; only a
    numeric form is usable as a prefix, anything else is ignored.
    """
    try:
        raw = (nvm / "alias" / "default").read_text().strip()
    except OSError:
        return None
    raw = raw.lstrip("v")
    return f"v{raw}" if raw[:1].isdigit() else None


def _version_key(name: str) -> tuple[int, ...]:
    """Sort key for ``vMAJOR.MINOR.PATCH`` directory names."""
    return tuple(int(p) for p in name.lstrip("v").split(".") if p.isdigit())


def agent_available(spec: AgentSpec) -> bool:
    """Whether the agent's CLI binary resolves (override, ``PATH`` or well-known dir)."""
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
