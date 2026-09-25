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
initial prompt (`claude_runner.queue_seed_for_task`), and performs the loader's `phase=wip` move itself -- already
when the run button queues the task, so a task waiting behind a running one shows as in progress, and again at spawn
(same guards: archived / `status=done` tasks are never resurrected). The `/task` command stays installed and keeps
working in manual terminal sessions.

## The flow

1. Click the agent logo on a task (list or kanban view). The task moves to the head of its project's queue lane; the
   worker starts a `claude` session in the task's project directory as soon as the lane is free (within ~2 s when it
   is), seeded with the task. With `claude_open_terminal` on, the full-page terminal (with a **Back** button) opens as
   soon as the session is live; off, you get a toast and the board stays.
2. Work interactively, exactly as in a terminal: read Claude's output, answer its questions, approve or deny its
   permission prompts, type follow-ups, `Ctrl-C` to interrupt. Clipboard: selecting text copies it; `Ctrl-V` and
   middle-click paste the clipboard; `Ctrl-C` with a selection keeps it instead of interrupting. An image -- pasted
   with `Ctrl-V` or dragged onto the terminal -- is saved to a temp file and its path typed into the prompt, so the
   agent can read it.
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

## Starting a run from a terminal

The run button has a CLI twin -- the *remote* start, for when the board is not the window in front of you:

```
ntasker run 123            # queue it and print where to watch it
ntasker run 123 --open     # ... and open that view in a browser
```

It posts to the same endpoint the button uses, so everything above applies unchanged: the task lands in its project's
lane, moves to `wip`, and the worker spawns the agent as soon as the lane is free. The command answers with the
position in that lane (`1.` = starting now) and the run view's URL, and says so when the queue is paused. It needs the
server -- that is where the worker lives. See [task-queue.md](task-queue.md#cli) for how it differs from
`ntasker queue add`.

Typed inside an agent session it means "hand this one to a session of its own": the other task runs in the web
terminal while this session carries on.

## Quick run -- an agent in a project, right now

Sometimes there is no task yet, just the urge to work in a project. Every project row in the sidebar carries the
**default agent's logo** next to its `+`. One click:

1. creates a task in that project (placeholder title, straight to `phase=wip`) so the session has something to hang on
   (`POST /api/projects/quick-run`), and appends it to the queue,
2. the worker starts the agent in the project directory **with a completely empty prompt** -- no seed, nothing typed --
   and the terminal opens with the caret in it,
3. briefs the agent -- via the *system* prompt, so the input line stays empty -- to give that placeholder task a real
   title itself as soon as your request is clear (`ntasker patch <id> --title "..."`).

The button only shows when the default agent's CLI is launchable. A quick run is a **Quicktask**: with
`quicktasks_bypass_lanes` on (the default) it starts even while another session runs in the project -- see
[task-queue.md](task-queue.md); off, it waits for its lane like any other run. Agents without a system-prompt flag (`AgentSpec.system_prompt_flag`, today Claude's
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

## Resuming a session (Claude only)

Every Claude web-terminal run is started with a forced session id (`--session-id <uuid>`), which ntasker persists on
the task. Because Claude Code keeps its conversation on disk, a session that has ended does not lose the history. A
done task whose run was Claude therefore shows a **Resume session** button (the Claude logo with a small
rotate glyph) in place of the run button. Clicking it reopens the terminal on `claude --resume <uuid>` in the task's
project directory -- the whole conversation replays and you can keep working where you left off.

The button appears only when the task ran at least once (a captured session id), its agent is Claude, and the `claude`
CLI is launchable. OpenCode and Pi have their own session mechanics and do not expose a resume button yet.

An **open** task with a stored session gets the same button too -- on its board row and kanban card, *next to* the run
button rather than in place of it, because both make sense there: the run button starts the task over with a fresh
seed, the resume button continues the conversation it already had. This is the common case after a review hand-off,
where the task sits in `review` with its work described in a session nobody can reach any more. It shows while the
task has no live session to switch to, and never on a draft.

Which path the board's resume takes depends on the task: an entry whose queued run **ended** goes through the queue
(`POST /api/queue/resume`), so it keeps its position and loses its ended flag, and the worker reopens it under the
usual lane rules; any other task reattaches directly, exactly like a done task's resume. The queue worker also
resumes ended entries on its own after an ntasker restart -- see [task-queue.md](task-queue.md#skipped-entries).

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

### External sessions -- `/task` in a terminal

A task can also be started **outside** ntasker: `/task <id>` typed into a Claude Code session in a terminal. That
run moves the task to `wip` like any other, but ntasker has no PTY to attach to -- so the loader reports the
session instead. Claude Code exports `CLAUDE_PID` to its subprocesses; the loader posts it to
`POST /api/claude/sessions/<id>/external`, and the server keeps the task marked as long as that process is alive
(a `kill(pid, 0)` probe on every poll -- no end-of-session hook needed; a second `/task` in the same session
replaces the first). The loader skips this inside an ntasker-spawned session (`NTASKER_TASK_ID` set).

An external task is **locked in the UI**: the card is greyed out with a *"Running in an external terminal"* stamp,
the run button shows a spinner and is disabled -- starting a second agent on the same task is exactly what this
prevents. The card stays editable. The queue treats the session like one of its own for the one-per-project rule
and the directory locks (the lane and the held dirs are occupied), but it is no queue run: it never advances or
ends a queue entry. There is no waiting/running distinction for external sessions -- they carry no hooks.

The loader also reports the session's **own id**. Claude Code exports it as `CLAUDE_CODE_SESSION_ID` -- the same
canonical UUID that names its transcript and that `--resume` takes -- so the loader sends it along with the pid and
the working directory, and the server stores both on the task. The moment that terminal is closed, the board's
**Resume session** button reopens exactly that conversation. Nothing to copy, nothing to type.

Requires the loader from v3.4+ (`ntasker agent install claude` after upgrading); the session id needs v3.11+.

## Picking up a session ntasker did not start

A conversation often starts in a terminal without any task -- you were just trying something -- and only later turns
into work worth tracking. Three ways lead from there into ntasker, all ending in the same place: a task whose
`session_id` and `session_cwd` point at that conversation, resumable from the board.

**From inside the running session** -- `ntasker adopt`:

```
ntasker adopt 42                      # hand this session to an existing task
ntasker adopt --title "Fix the parser" # ... or to one created right here
```

It reads the session id and pid from its own environment and the directory from the shell, so there is nothing to
look up. With `--title`, the project is derived from the working directory (the known project it sits in, else the
path relative to `projects_base` / home) unless `--project` says otherwise. While the session keeps running the task
shows as busy, exactly like a `/task` one.

**From the board** -- the *Pick up a session* button in the page header. It opens on the conversations running
elsewhere *right now*, across all projects, newest first: each row names its project, the first thing you asked and
when it last ran. One click ends that session, files it under a new task (titled after that first prompt, in the
session's own project) and opens it right here. Sessions that already belong to a task are left out -- they are on the
board anyway.

*Also show finished sessions* folds out the ones that have ended. Those are only findable inside the directories of
one project, so that list asks for a project first. It is read from the transcripts Claude Code writes under
`<claude home>/projects/<cwd-slug>/<session-id>.jsonl`, so it also finds sessions from long before ntasker knew
anything about them; one already owned by a task is marked as such and a click continues that task instead of making
a second one.

**Knowing which sessions are still running** needs the **Pick up terminal sessions** setting
(*Settings -> Agents & runs*, key `session_discovery`). It is off by default because switching it on writes into
*your* Claude Code settings (`<claude home>/settings.json`): two hooks calling `ntasker hook session`, on
`SessionStart` and `UserPromptSubmit`. Every terminal session then reports its id, pid and directory to ntasker --
that is what makes the *running* badge and the *End and continue here* button possible. The edit is surgical (other
hooks stay, a timestamped `.bak` is kept) and switching the setting off removes exactly those two entries again.
Without it the running list stays empty -- the dialog says so and links to the setting; the finished sessions behind
the toggle are still listed and adoptable, but a session that is still open has to be closed in its own terminal
first.

A resume always spawns in the directory the session was recorded in (`session_cwd`), not in the project directory:
Claude Code finds a session id only under the directory it belongs to, so a conversation started in a subfolder
resumes there.

## Letting a session run ntasker commands

Claude Code's auto mode counts every `ntasker` call as a write to an external system and stops to ask -- including
the `ntasker finish` a session ends its own task with, which makes an unattended queued run stall on a permission
dialog. The **Run ntasker commands without asking** setting (*Settings -> Agents & runs*, key
`claude_permissions`) adds one allow rule, `Bash(ntasker *)`, to `permissions.allow` in *your* Claude Code settings
(`<claude home>/settings.json`).

Off by default, for the same reason as the setting above: it edits a file that belongs to you. The edit is surgical
(every other rule stays, a timestamped `.bak` is kept) and switching it off removes exactly that one rule again.
`ntasker agent install claude` notes when the rule is missing but never writes it by itself.

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

## Subscription limits in the topbar

With a claude.ai subscription login, the topbar shows the two usage windows Claude Code itself reports -- the
**5-hour** and the **weekly** window -- as small meters (`5h 11%`, `7d 90%`): green, yellow from 50 %, red from
80 %. Hovering a meter shows when the window resets. The numbers come from the same endpoint the `/usage` slash
command reads, authenticated with the OAuth token Claude Code keeps in `<claude home>/.credentials.json`
(`NTASKER_CLAUDE_HOME` moves the home). The widget is hidden when there is nothing to show: the Claude plugin is
off, no token is stored (API-key login, never logged in, or the token lives in the macOS keychain), the token has
expired, or the account has no subscription limits. ntasker never refreshes the token -- a running Claude Code does
that.

`GET /api/claude/usage` -> `{"usage": {"five_hour": {"utilization", "resets_at"}, "seven_day": {...}} | null}`.
The server caches the answer for 30 s; the page polls once a minute. A failed refresh serves the last answer.

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
* Endpoints: `GET /api/claude/status` (CLI + PTY available?), `GET /api/claude/sessions` (`{active, waiting, external,
  projects, titles}`, for the busy / waiting indicators and the run tabs), `POST /api/projects/quick-run`,
  `POST /api/claude/sessions/<id>/external` (the `/task` loader's registration, see above),
  `POST /api/claude/sessions/<id>/adopt` (point a task at a session ntasker did not start),
  `POST /api/claude/sessions/live` (a terminal session reporting itself, from `ntasker hook session`),
  `GET /api/claude/sessions/live` (the sessions running right now, across all projects, plus whether discovery is on),
  `GET /api/claude/session-hook` (`{installed, path, readable}`, re-read by the settings page after the switch),
  `GET /api/claude/permission-rule` (the same for ntasker's allow rule),
  `GET /api/claude/sessions/discovered?project=<name>` (transcripts of a project),
  `POST /api/claude/sessions/discovered/<session id>/end` (SIGTERM, waits for the exit).
* Session discovery (`src/ntasker/sessions.py`): reads the head of each transcript for the working directory and the
  first user message, merges in the live registry filled by the hook, and marks sessions a task already owns. The
  hook itself is managed in `claude_assets.py` (`set_session_hook`), driven by the `session_discovery` setting.
* Permission rule (`claude_assets.py`, `set_permission_rule` / `permission_rule_state`): the same surgical patch of
  the user's settings file, driven by the `claude_permissions` setting.

## Requirements

The feature needs the `claude` CLI on `PATH` and a POSIX pseudo-terminal (Linux/macOS). Without either, the robot
button stays hidden and `GET /api/claude/status` reports the reason. No Python SDK is involved.
