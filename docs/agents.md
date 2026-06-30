# Multi-agent framework

ntasker is **agent-agnostic**: every task can run on one of several AI coding agents, and the framework is built so that
adding another is a single registry entry. Today it ships three -- **Claude Code**, **OpenCode** and **Pi**.

This page covers the architecture, the per-agent config homes and command formats, the CLI, and the settings. For the
interactive run view itself (the embedded TUI, background sessions, busy/waiting indicators), see
[docs/claude-runs.md](claude-runs.md) -- that flow is identical across all agents.

## The registry is the single source of truth

Everything agent-specific lives in `src/ntasker/agents.py`, as one `AgentSpec` per agent in the `AGENTS` dict (keys
`claude`, `opencode`, `pi`). No other module hard-codes an agent name -- the runner, the app, the CLI and the settings
all read the registry. An `AgentSpec` captures:

- `key` / `label` -- stable id persisted in `tasks.agent`, and the human-facing name.
- `binary` -- the CLI executable looked up on `PATH` for availability and spawning.
- `icon` -- the static asset shown on the run button (`claude.webp`, `opencode.svg`, `pi.svg`).
- `home_env` / `default_home` / `commands_subdir` / `skills_subdir` -- where the `/task` slash command and `SKILL.md`
  install (see the table below).
- `command_template` -- the packaged template under `claude_assets/command/` rendered into the command file.
- `seed_mode` -- how the `/task <id>` seed is handed to the CLI: `positional` (appended as an argument) or
  `prompt-flag` (passed as `--prompt <seed>`).
- `extra_strip_env` -- agent-specific nesting markers stripped before spawn, on top of the shared `CLAUDE_CODE_*` /
  `AI_AGENT` base set, so a session always starts as a fresh top-level run.

Permission/auto-approve flags are produced by `AgentSpec.permission_args()`, which reads the agent-specific settings
lazily (Claude's permission mode, OpenCode's `--auto`).

### Adding a fourth agent

1. Add one `AgentSpec` entry to `AGENTS` in `src/ntasker/agents.py`.
2. Reuse `task.generic.md.template` (or add a new template under `src/ntasker/claude_assets/command/`) for its `/task`
   slash command.

That is the whole surface. The per-agent `<key>_bin` setting, the validation whitelists, the run button, the new-task
picker and the `/settings` card are all derived from the registry, so they pick the new agent up automatically.

## Per-task agent

Each task has a nullable `agent` field (a DB column). Resolution at run time is, highest precedence first:

1. the task's own `agent`,
2. the `default_agent` setting,
3. the built-in default `claude`.

Unknown values are dropped at each step (`resolve_agent_key` in `agents.py`), so a stale `tasks.agent` value can never
break a run. Set the agent in the new-task form, the edit dialog, or via the CLI:

```bash
ntasker add --title "..." --agent opencode    # create a task pinned to OpenCode
ntasker patch 34 --agent pi                    # repoint an existing task
ntasker patch 34 --agent ''                    # clear -> falls back to default_agent
```

## Config homes and command formats

ntasker installs its integration assets (the `SKILL.md` and the `/task <id>` slash command) into **each agent's own
config home**:

| Agent       | Key        | Binary     | Config home          | Command subdir | `/task` seed passed as |
|-------------|------------|------------|----------------------|----------------|------------------------|
| Claude Code | `claude`   | `claude`   | `~/.claude`          | `commands`     | positional             |
| OpenCode    | `opencode` | `opencode` | `~/.config/opencode` | `command`      | `--prompt <seed>`      |
| Pi          | `pi`       | `pi`       | `~/.pi/agent`        | `prompts`      | positional             |

The home can be overridden per agent via its own env var (`NTASKER_CLAUDE_HOME`, `OPENCODE_CONFIG_DIR`,
`PI_CODING_AGENT_DIR`) or the CLI `--home` flag. The skill always lands under `skills/ntasker/` inside that home.

## CLI

```bash
ntasker agent list                       # all agents: CLI availability + integration status
ntasker agent install opencode           # install the SKILL.md + /task slash command
ntasker agent install pi --check         # read-only status: exit 0=identical, 1=drift, 2=not installed
ntasker agent install claude --force     # overwrite divergent files (timestamped backups)
ntasker agent install opencode --dry-run # show planned actions without writing
ntasker agent install pi --command-name todo  # use /todo instead of /task
ntasker agent install claude --home /tmp/test-home  # redirect to a non-default config home
```

`install-claude-assets` remains as a **deprecated alias** of `ntasker agent install claude`.

`ntasker agent list` prints a per-agent table with the CLI state (`ok` / `-`), the integration state
(`installed` / `drift` / `-`) and the resolved config home; pair it with `--json` for machine-readable output.

## Settings

| Setting                  | ENV                       | Meaning                                                  |
|--------------------------|---------------------------|----------------------------------------------------------|
| `default_agent`          | `NTASKER_DEFAULT_AGENT`   | Default / fallback agent: `claude`/`opencode`/`pi`       |
| `claude_bin`             | `NTASKER_CLAUDE_BIN`      | Path to the Claude CLI when not on the server PATH       |
| `opencode_bin`           | `NTASKER_OPENCODE_BIN`    | Path to the OpenCode CLI when not on the server PATH     |
| `pi_bin`                 | `NTASKER_PI_BIN`          | Path to the Pi CLI when not on the server PATH           |
| `claude_permission_mode` | --                        | `default`/`auto`/`plan`/`bypassPermissions`              |
| `opencode_auto`          | `NTASKER_OPENCODE_AUTO`   | Run OpenCode sessions with `--auto` (auto-approve)       |
| `claude_open_terminal`   | `NTASKER_CLAUDE_OPEN_TERMINAL` | Open the terminal now vs. start in the background   |

In the `/settings` UI these are grouped under an **AI agent integration** card (common: default agent + open-terminal)
with one sub-card per agent showing availability, the CLI-path field, the agent-specific run options, and the install
status.

### Configurable CLI path

The ntasker server may run with a narrower `PATH` than your interactive shell -- e.g. as a `systemd --user` unit without
`nvm` or `~/.opencode/bin` on the path. When an agent's CLI is not found, point ntasker at it with the per-agent
`<key>_bin` setting (or its `NTASKER_<KEY>_BIN` env var):

```bash
ntasker config set opencode_bin ~/.opencode/bin/opencode
ntasker config unset opencode_bin          # back to auto-detect on PATH
```

A value containing a `/` is treated as a path (expanded; must be an executable file); a bare name is looked up on
`PATH`. An empty value auto-detects. The resolution logic lives in `ntasker.agents.resolve_binary`; a configured value
that does not resolve leaves the agent reporting unavailable (its run button stays hidden).

## API

`GET /api/agents` returns the registry as the single feed for the frontend: each agent's `key`, `label`, `icon`,
availability (binary resolves + a POSIX PTY exists) with a `reason`, the `/task` integration status, and which agent is
the current default. It is read-only -- installs go through the CLI to avoid CSRF / DNS-rebind write surface.
