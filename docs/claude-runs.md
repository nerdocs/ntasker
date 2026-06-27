# Run with Claude

Every task row carries a robot button (**Run with Claude**, Claude-orange). It opens a full-page view that embeds the
**real interactive `claude` CLI** -- the genuine Claude Code TUI, rendered in the browser by xterm.js. Not a headless
wrapper: it is the same binary you run from a shell, so you get its full interactivity (it asks, you answer; you can
steer it, interrupt it with `Ctrl-C`, type anything) and the *identical* context.

## The flow

1. Click the robot on a task (list or kanban view). A full-page terminal opens (with a **Back** button), and a
   `claude` session starts in the task's project directory, seeded with the **`/task <id>`** slash command so the
   task is loaded into the session straight away via ntasker's existing Claude Code integration.
2. Work interactively, exactly as in a terminal: read Claude's output, answer its questions, approve or deny its
   permission prompts, type follow-ups, `Ctrl-C` to interrupt.
3. **Stop** terminates the session (kills the process group). **Back** returns to the list/kanban.

## Identical context

Because the session is the real `claude` binary launched in the project directory, it reads exactly what your own
shell session would: `~/.claude` config, the project's `CLAUDE.md`, skills (so `/task` and `#<id>` work natively),
MCP servers, and your permission settings. Permission prompts are handled **in the TUI** -- there is no separate
ntasker permission layer. The only thing ntasker strips from the child environment is the `CLAUDE_CODE_*` markers, so
the session always starts as a fresh top-level session rather than a nested one.

## Background sessions

Sessions are **persistent and reattachable**. The `claude` process lives server-side in a registry keyed by task id;
it keeps running when you press **Back** or even reload the page. Re-opening the run view reattaches: ntasker replays
the recent output buffer to reconstruct the screen, then streams live again. Several tasks can run at once, each with
its own indicator.

A page reload drops the *client* terminal but not the *server* session -- reopening reattaches. Stopping the session,
or the `claude` process exiting on its own, ends it; the next robot click then starts a fresh one.

**Marking the task done ends its session.** When a task's status flips to `done` (via the API -- which is also how
the ntasker skill closes a task), ntasker terminates that task's session completely: the work is finished, so the
interactive process is torn down. A done task shows **no run button** at all -- you cannot start a session from the
Done column.

## Session indicators -- running vs. waiting

A task with a live session is highlighted in both the list and kanban so it stands out, and its button reflects state:

* **Running** -- the session is actively working. The card gets a subtle blue tint + left accent and the button shows
  a **spinner**.
* **Waiting for input** -- Claude is parked at a prompt and wants you (a question, a permission dialog). The card turns
  **amber** and the button becomes a pulsing **question mark**.

The CLI emits no explicit "I have a question" signal, so ntasker infers *waiting* from **output silence**: while Claude
works its TUI keeps repainting, so a terminal that has produced nothing for a while is blocked on input. The silence
window is the **`claude_idle_seconds`** setting (default `8`, in seconds). There is no UI for it -- set it via CLI or
the settings API:

```
ntasker config set claude_idle_seconds 12          # CLI
curl -X PUT 127.0.0.1:8766/api/settings/claude_idle_seconds -H 'Content-Type: application/json' -d '{"value":"12"}'
```

The indicators self-heal: a poll refreshes them every ~1.5 s, so a stale "busy" state (e.g. after a server restart)
clears on its own rather than spinning forever.

## Security

ntasker has no authentication and binds to `127.0.0.1` only. A session is your **full interactive Claude Code, shell
included** -- gated solely by that loopback bind. Keep the bind local (never `0.0.0.0`).

## Implementation

* Backend (`src/ntasker/claude_runner.py`): spawns `claude` in a POSIX pseudo-terminal and bridges the PTY to a
  WebSocket (`/ws/claude/<task_id>`) -- output down (base64), keystrokes / resize / stop up. Sessions and a bounded
  replay buffer live in a module-level registry.
* Frontend: xterm.js + the fit addon, vendored through the CDN/SRI asset manifest in `src/ntasker/assets.py` (no
  build step), driving the terminal in `static/app.js`.
* Endpoints: `GET /api/claude/status` (CLI + PTY available?), `GET /api/claude/sessions` (`{active, waiting}` task-id
  lists, for the busy / waiting indicators), `GET /api/tasks/<id>/claude-run/defaults` (guessed cwd + `/task <id>`
  seed).

## Requirements

The feature needs the `claude` CLI on `PATH` and a POSIX pseudo-terminal (Linux/macOS). Without either, the robot
button stays hidden and `GET /api/claude/status` reports the reason. No Python SDK is involved.
