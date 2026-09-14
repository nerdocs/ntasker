# Run with an agent

Every task row carries a run button showing **its agent's logo**. It puts the task at the head of the
[task queue](task-queue.md) -- the only path that starts a session -- and, once the queue worker has spawned it, opens
a full-page view that embeds the **real interactive agent CLI** -- the genuine TUI, rendered in the browser by
xterm.js. Not a headless wrapper: it is the same binary you run from a shell, so you get its full interactivity (it
asks, you answer; you can steer it, interrupt it with `Ctrl-C`, type anything) and the *identical* context.

## Multiple agents (Claude · OpenCode · Pi)

ntasker is agent-agnostic. Each task carries an **agent** (`claude`, `opencode` or `pi`); pick it in the new-task form
or the edit dialog, or leave it on the **default agent** (the `default_agent` setting). The run button shows that
agent's logo and only appears when the agent's CLI is on `PATH`.

The agent registry lives in `src/ntasker/agents.py` -- one `AgentSpec` per agent captures the binary, the spawn
command (permission flags + how the `/task` seed is passed), the config home, and where the integration assets install.
Adding a fourth agent is one registry entry plus a command template.

**Integration assets per agent.** Each agent gets ntasker's skill (`SKILL.md`) and `/task <id>` slash command
installed into its own config home -- `~/.claude`, `~/.config/opencode`, `~/.pi/agent`. Install / check per agent:

```
ntasker agent list                 # CLI availability + integration status
ntasker agent install opencode     # install the skill + /task command
ntasker agent install pi --check   # 0=ok, 1=drift, 2=not installed
```

The /settings page groups this as **AI agent integration** (common: default agent + open-terminal) with one subgroup
card per agent (availability, run options, install status). `install-claude-assets` stays as a deprecated alias for
`agent install claude`.

The rest of this page describes the Claude session in detail; OpenCode and Pi work the same way (their CLI is spawned
in the task's project directory with the same seed), differing only in the per-agent options above.

**The seed.** A spawned run does not use the `/task <id>` slash command -- that would make the agent run the loader
script first, a full extra inference pass plus a couple thousand prompt tokens. ntasker instead inlines the task data
-- id, title, description, project, tags, plugin briefings, and the queue's hand-off rules -- directly into the
initial prompt (`claude_runner.queue_seed_for_task`), and performs the loader's `phase=wip` move itself at spawn (same
guards: archived / `status=done` tasks are never resurrected). The `/task` command stays installed and keeps working
in manual terminal sessions.

## The flow

1. Click the agent logo on a task (list or kanban view). The task moves to the head of its project's queue lane; the
   worker starts a `claude` session in the task's project directory as soon as the lane is free (within ~2 s when it
   is), seeded with the task. With `claude_open_terminal` on, the full-page terminal (with a **Back** button) opens as
   soon as the session is live; off, you get a toast and the board stays.
2. Work interactively, exactly as in a terminal: read Claude's output, answer its questions, approve or deny its
   permission prompts, type follow-ups, `Ctrl-C` to interrupt.
3. **Stop** terminates the session (kills the process group). **Back** returns to the list/kanban.

Another agent already live in the same project? The run simply waits in the queue behind it -- the worker runs one
session per project. The board damps such tasks and their tooltip says so. Work spanning several repos takes
[directory locks](directory-locks.md) on the other projects, and waits for those directories too.

### Working directory

The run starts in the task's project directory. A directory that does not exist yet is created when it lies inside
`projects_base` (a "new project" starts in a fresh dir). Everything else -- a task without a project, or a project
path outside the base -- falls back to the **`no_project_dir`** setting, then to `projects_base`, then to your home
directory. Keep a real directory configured: Claude Code treats the home directory as an untrusted workspace and
parks the session on its trust prompt, which looks like a run that never starts.

## Quick run -- an agent in a project, right now

Sometimes there is no task yet, just the urge to work in a project. Every project row in the sidebar carries the
**default agent's logo** next to its `+`. One click:

1. creates a task in that project (placeholder title, straight to `phase=wip`) so the session has something to hang on
   (`POST /api/projects/quick-run`), and puts it at the head of the queue,
2. the worker starts the agent in the project directory **with a completely empty prompt** -- no seed, nothing typed --
   and the terminal opens with the caret in it,
3. briefs the agent -- via the *system* prompt, so the input line stays empty -- to give that placeholder task a real
   title itself as soon as your request is clear (`ntasker patch <id> --title "..."`).

The button only shows when the default agent's CLI is launchable; a project with a live session makes the quick run
wait like any other run. Agents without a system-prompt flag (`AgentSpec.system_prompt_flag`, today Claude's
`--append-system-prompt`) start the same way, they just never get the naming hint -- the task then keeps its
placeholder title until you rename it.

The setting **open terminal on run** does not apply here: a quick run always reveals and focuses the terminal, because
typing into it immediately is the whole point.

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

ntasker ends a session on its own in exactly one case: the task being set to `done`. Everything else -- the agent's
review hand-off, moving a task around the board, deleting it -- leaves the session alone; it ends when its process
exits or you press **Stop**. Before it hands off, the agent writes its **final report**
(`ntasker report <id>`) -- read it via the report icon on the card, no need to reopen the session. See
[task-queue.md](task-queue.md).

A page reload drops the *client* terminal but not the *server* session -- reopening reattaches. Stopping the session,
or the `claude` process exiting on its own, ends it; the next run click queues a fresh one.

**Marking the task done ends its session.** When a task's status flips to `done` -- from the board, the run header,
the API, or `ntasker done` run inside the session itself -- ntasker terminates that task's session completely: the
work is finished, so the interactive process is torn down and its tab disappears. A done task shows **no run
button** -- you cannot start a *fresh* session from the Done column.

## Resuming a finished session (Claude only)

Every Claude web-terminal run is started with a forced session id (`--session-id <uuid>`), which ntasker persists on
the task. Because Claude Code keeps its conversation on disk, a session that has ended does not lose the history. A
done task whose run was Claude therefore shows a **Resume session** button (the Claude logo with a small
rotate glyph) in place of the run button. Clicking it reopens the terminal on `claude --resume <uuid>` in the task's
project directory -- the whole conversation replays and you can keep working where you left off.

The button appears only when the task ran at least once (a captured session id), its agent is Claude, and the `claude`
CLI is launchable. OpenCode and Pi have their own session mechanics and do not expose a resume button yet.

## Session indicators -- running vs. waiting

A task with a live session is highlighted in both the list and kanban so it stands out, and its button reflects state:

* **Running** -- the session is actively working. The card gets a subtle blue tint + left accent and the button shows
  a **spinner**.
* **Waiting for input** -- Claude is parked at a prompt and wants you (a question, a permission dialog). The card turns
  **amber** and the button becomes a pulsing **question mark**.

For Claude Code the signal is **explicit**: every spawned session carries ntasker's hooks (via `--settings`, see
[directory-locks.md](directory-locks.md#hooks-claude-code)) -- `Stop` and a permission prompt report *waiting*, a
submitted prompt or a finished tool call reports *running* -- so the badge flips the moment Claude stops or asks,
not after a timeout.

OpenCode, Pi, and a Claude session before its first hook has fired fall back to the **output-silence** heuristic:
while an agent works its TUI keeps repainting, so a terminal that has produced nothing for a while is blocked on
input. The silence window is the **`claude_idle_seconds`** setting (default `8`, in seconds). There is no UI for it --
set it via CLI or the settings API:

```
ntasker config set claude_idle_seconds 12          # CLI
curl -X PUT 127.0.0.1:8766/api/settings/claude_idle_seconds -H 'Content-Type: application/json' -d '{"value":"12"}'
```

The indicators self-heal: a poll refreshes them every ~1.5 s, so a stale "busy" state (e.g. after a server restart)
clears on its own rather than spinning forever.

## Quick prompts

The run view's toolbar can carry buttons that type a canned prompt into the live session and send it -- the same
as if you had entered it in the terminal. Configure them under *Settings -> Agents & runs -> Quick prompts* (one
label + prompt per row), or set the `quick_prompts` key directly:

```
ntasker config set quick_prompts '[{"label": "Review", "prompt": "Write the report and hand the task off to review."}]'
```

The text goes straight to the agent's input line, so use them while the session is idle at its prompt. While the
agent is working, Claude Code queues the text for the next turn; at a permission dialog or a selection prompt the
keystrokes land in that dialog instead.

## Security

ntasker has no authentication and binds to `127.0.0.1` only. A session is your **full interactive Claude Code, shell
included** -- gated solely by that loopback bind. Keep the bind local (never `0.0.0.0`).

## Implementation

* Backend (`src/ntasker/claude_runner.py`): spawns `claude` in a POSIX pseudo-terminal and bridges the PTY to a
  WebSocket (`/ws/claude/<task_id>`) -- output down (base64), keystrokes / resize / stop up. Sessions and a bounded
  replay buffer live in a module-level registry. The `attach` message only **reattaches** a live session; without one
  it answers `{"type":"error"}` -- fresh sessions are spawned by the queue worker (`start_detached_session`). The one
  exception is `attach {resume: true}`, which reopens a finished task's stored session.
* Frontend: xterm.js + the fit addon, vendored through the CDN/SRI asset manifest in `src/ntasker/assets.py` (no
  build step), driving the terminal in `static/app.js` (`runNext` queues, `_openWhenLive` waits for the session).
* Endpoints: `GET /api/claude/status` (CLI + PTY available?), `GET /api/claude/sessions` (`{active, waiting, projects,
  titles}`, for the busy / waiting indicators and the run tabs), `POST /api/projects/quick-run`.

## Requirements

The feature needs the `claude` CLI on `PATH` and a POSIX pseudo-terminal (Linux/macOS). Without either, the robot
button stays hidden and `GET /api/claude/status` reports the reason. No Python SDK is involved.
