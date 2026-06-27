# Run with Claude

Every task row carries a robot button (**Run with Claude**, Claude-orange) that launches a Claude Code agent run on
that task, straight from the web UI -- no terminal, no copy-paste. Claude's output streams into a panel in the
browser as it works.

The button only appears when the feature is actually available (see [Requirements](#requirements)); otherwise the
rest of ntasker is untouched.

## The flow

1. Click the robot on a task (list or kanban view). A dialog opens, pre-filled with:
   - a **prompt** (`Please work on ntasker task #<id>: <title>` -- the `#<id>` makes Claude auto-load the ntasker
     skill, which knows the tracker workflow). Editable.
   - a guessed **working directory** (`projects_base`/`<project>`, else `~`/`<project>`). Editable -- it is only a
     guess from the task's free-form project name.
   - a **permission mode** and optional tool allow/deny lists (see below).
2. Hit **Start**. The browser opens a WebSocket; Claude starts and its messages stream into the panel (assistant
   text, thinking, tool calls, tool results, the final result), auto-scrolling as they arrive.
3. **Stop** ends the run; closing the dialog tears down the socket.

Run state is purely in-memory and lives exactly as long as its socket (`/ws/claude/<task_id>`) -- there is no
`claude_runs` table and no run history. This is ntasker's one and only WebSocket: Claude's events flow down, a stop
flows up.

## Permissions -- fixed at start

Permissions are chosen **before** the run starts and hold for its whole duration. This is deliberate, and it is the
only model that works reliably with the headless CLI:

| Mode                             | Behaviour                                                            |
|----------------------------------|---------------------------------------------------------------------|
| Plan only (read-only)            | Claude plans but cannot change anything -- a safe dry run            |
| Only allowed tools (others denied) | only tools on the *allowed* list run; anything else is denied     |
| Auto-approve file edits          | file edits run without asking; other tools still gated              |
| Allow everything                 | every tool runs, including shell, with no gate                      |

The **Allowed tools** / **Denied tools** fields take comma-separated tool names (`Read, Edit, Bash`) and map straight
onto the CLI's `--allowedTools` / `--disallowedTools`. They are most useful with the *Only allowed tools* mode, which
otherwise denies everything that needs approval.

To keep the UI authoritative, runs are launched with `setting_sources=[]` -- the user's ambient `~/.claude` permission
settings are **not** loaded, so a global allow rule cannot silently override (or a global deny block) the choice made
in the dialog. The trade-off is that project `CLAUDE.md` files are not auto-loaded into the run; skills are re-enabled
explicitly so the `#<id>` prompt can still pull in the ntasker skill.

### Why no interactive per-tool prompts

An earlier design asked the user to approve each tool call live. It was removed because it does not work against the
headless CLI this feature drives: the SDK's `can_use_tool` callback never fires (there is no human at the subprocess
to prompt), and mid-run `set_permission_mode` does not take effect. Both were verified empirically. The fixed,
up-front gate above is what the CLI honours, so that is what the UI exposes.

## Security

ntasker has no authentication and binds to `127.0.0.1` only. A Claude run inherits your **full local user
permissions** -- in *Allow everything* mode it is, in effect, remote code execution gated solely by that loopback
bind. Keep the bind local (never `0.0.0.0`), and prefer *Plan only* or a tight allow-list unless you have a reason
not to.

## Requirements

The feature is an **optional** extra. The robot button stays hidden unless both are present:

- the Python SDK: `pip install ntasker[claude]` (or `claude-agent-sdk` directly);
- the `claude` CLI on `PATH` (the SDK shells out to it).

`GET /api/claude/status` reports availability and, when unavailable, the reason.
