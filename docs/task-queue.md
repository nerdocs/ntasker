# Task Queue

The task queue is a worklist ntasker works through on its own -- and **the only way an agent session starts**. Every
run button (board row, kanban card, "Create + Run", the sidebar quick run) puts its task at the head of its project's
lane; the queue worker hands it to its agent as soon as that lane is free, waits for it to finish, and moves on to the
next.

It sits in a panel above the board and is visible in both the list and the kanban view.

Tasks get in through the **run button** on each row / card -- or by dragging a card onto the panel, which appends it
(the whole panel is the drop target, the empty state included). Pressing the run button on an already-queued task
moves it to the front of its lane; pressing it on a task with a live session just opens that session. The panel's `✕`
takes an entry out.

## Rules in one paragraph

One task runs **per project** at a time, so several projects progress in parallel while a single project stays strictly
sequential. Exactly one thing finishes a queued run: the task reaching `status=done` -- closed by you, or by the agent
itself when the task description told it to. `done` is also the **only** thing that makes ntasker end a session: the
done task's session is killed, its tab disappears, and its lane and directory locks are free. The agent's review
hand-off is a phase change like any other -- the task waits in the review column, with its session alive, and the
next task of that project starts once you close it. A session that ends before the task is done leaves its entry
queued, flagged **ended**, blocking its lane until you have looked at it.

**Quicktasks** -- the sidebar's quick run, with or without a prompt -- are the one exception: with
`quicktasks_bypass_lanes` on (the default) a Quicktask starts on the next tick no matter what runs in its project, and
while it runs it neither occupies the lane nor holds its directories for the queue. Everything else (ended, resume,
done) applies as usual. Switch it off and a Quicktask queues like any other run.

## Drafts

A task flagged **Draft** (checkbox in the new-task form and the edit dialog, `--draft` on the CLI, `draft` in the
API) is an idea on file, not a job: it is never started -- not by the queue, not by hand, not via `/task`. It cannot
be queued (drop refused, `queue add` and the run button refuse it, `PUT /api/queue` drops it silently), flagging a
queued task throws it out of the queue at once, resume is refused, and the `/task` loader stops with an `ENTWURF`
message before the agent sees any task text. The spawn itself refuses a draft as the last line of defence. Untick
Draft to release the task.

## The panel

**One column per project.** The worker runs one task per project at a time, so a column is literally one execution lane:
its own rail, its own `1..n` numbering, its own next-up. Cross-project tasks get the first column. Many projects scroll
sideways rather than squeezing every lane flat.

| Element | Meaning |
|---|---|
| The rail down the left of a column | Marches while the queue runs, frozen while it is paused. |
| Position bead (`1`, `2`, `3`) | Run order **within that project**. Indigo = running, orange = waiting for input. |
| Lock badge on an entry | The queue is passing this one over -- see [Skipped entries](#skipped-entries). The tooltip names the blockers, including the project of any blocker from another column. |
| **Running** / **Waiting for your input** | Links into that session's terminal. |
| `✕` | Removes the entry. |

Queued tasks also carry an indigo `⧉ n` badge on their board row / kanban card -- the same within-project position, so
you can see when a task runs without looking at the panel.

Dragging an entry **inside its column** reorders it. Across columns it is refused: that would have to silently reassign
the task's project, which belongs in the edit dialog. The one gesture that legitimately crosses columns is setting a
dependency -- see below. A queue entry never leaves the panel by drag (the board does not accept it), and a running
entry is not draggable at all -- nothing about it can change any more.

## Pause and resume

The queue is **on by default** -- it is the only start path, so an off switch would mean no run ever starts. The header
button pauses it (**Pause queue**) and resumes it (**Resume queue**); the state lives in the `queue_enabled` setting
(not in `localStorage`), so it survives a restart and every open browser tab agrees on it.

Pausing stops the queue from starting anything new. Run buttons still queue tasks while paused; a task that is already
running keeps going, and still leaves the queue when its session ends.

With `claude_open_terminal` on (the default), a run button also opens the task's terminal as soon as the worker has
started the session (the browser polls for up to 15 s). Off, it only toasts and the board stays on screen; the queue
panel's **Running** link opens the terminal later.

## What a queued run is told

A run does **not** use the `/task <id>` slash command. It gets a self-contained seed
(`claude_runner.queue_seed_for_task`) which inlines the task and tells the agent to hand it off when the work is done.

> When the work is done, do two things in this order, without asking:
> 1. write your final report: `ntasker report <id>` (Markdown on stdin -- what you did, verified, left open)
> 2. hand the task over for review, then stop and wait: `ntasker patch <id> --phase review`

The report lands on the task (`report`, `report_at`) and is read from the report icon on its card -- you never have to
open the session for it. The session stays open after the hand-off: you review the task there, and closing it (`done`)
is what retires the entry and lets the next task of that project start. **A queue run never closes a task on its
own** -- the results wait for you in the review column, exactly like a run you started by hand. The seed allows
`done` only when you or the task description explicitly ask for it; a task that says so closes itself and the queue
moves on unattended.

The seed also tells the agent what to do when it *cannot* finish: still write the report (the blocker), leave the phase
as-is and stop. If its session then ends -- see **ended** below.

The rules block is editable per agent: *Settings -> Plugins -> <agent> -> Run rules* (setting `<agent>_run_rules`,
e.g. `claude_run_rules`). `{id}` in the text is replaced by the task id. Unset the key (the *Reset to default*
button, or `ntasker config unset claude_run_rules`) to go back to the built-in text.

## Skipped entries

An entry stays queued but is passed over when

* one of its dependencies is still open (the same guard the kanban drag applies), or
* its agent's CLI is not installed, or
* another task's live session holds one of its directories (badge `#<holder>`), or
* with `require_clean` on, one of its directories has uncommitted changes (badge `dirty: <project>`).

The last two are [directory locks](directory-locks.md). The queue then looks at the next entry **in the same project**
instead of stalling behind it. Every case is labelled on the entry, so a queue that looks idle always says why.

**Ended** is different: an entry whose session ended before the task was done (stopped, crashed, blocker) stays at
the head of its lane with the badge `ended` and **blocks that lane** -- nothing else in the project starts until you
act. Read its report, then either `✕` it out (lane free), set the task done, run it again (the run button /
`ntasker queue add --top` clear the flag and start a fresh session), or **resume** it: the entry carries a resume
button (Claude logo with a rotate glyph) when the task ran at least once and its agent is Claude. The worker then
reopens the stored conversation (`claude --resume <uuid>`, see [claude-runs.md](claude-runs.md)) in the entry's queue
position, under the usual lane rules -- the run continues where it broke off, the phase stays as it was, and the
`require_clean` git check is skipped (the dirt is that run's own unfinished work). Dragging it inside the column does
not restart it. The flag lives in the DB (`session_ended_at`), so a server restart does not silently start the task
again.

**Restart of ntasker.** Going down takes every session with it. The server flags the running queue entries as
`ended` on shutdown, and the worker's first tick after the restart **resumes** every flagged entry that can be
resumed (as above) instead of starting it over. Entries whose agent cannot resume stay flagged for you to act on.

A session you start by hand also occupies its project: two agents in one working directory is exactly what the
one-per-project rule exists to prevent. On a *running* queue, a hand-started session on a queued task is adopted -- it
advances the queue like a queued run would. On a paused queue it is left alone.

## Setting dependencies by drag

A dependency is what makes one task wait for another, across projects included -- so it is the queue's real ordering
tool, and it is a drag gesture on both the board and the panel.

Every drop target splits into three horizontal bands:

| Where you drop | What happens |
|---|---|
| Top / bottom edge | Insert before / after -- reordering, exactly as before. |
| Middle | **A depends on B**: the dragged task waits for the one you dropped it on. |

The middle band is the small one on purpose: reordering is the everyday gesture and has to stay easy to hit, while
linking two tasks is rare and deserves a deliberate aim. It is `clamp(height * 0.2, 10px, 28px)` -- the floor keeps it
reachable on a 2rem queue row, the ceiling keeps it from dominating a tall kanban card.

While the drag is in flight the middle band shows an indigo ring around the whole target plus a label spelling out the
direction ("#12 waits for #7"), and the cursor switches to the browser's link cursor. Nobody has to remember which way
round it goes.

Refused straight away (no ring, no-drop cursor): dropping a task on itself, and a dependency that already exists.
**Cycles are not** pre-checked -- the board only holds the currently filtered tasks, so a check in the browser would
miss edges. The server's `validate_deps` stays the single authority and a rejection comes back as a toast naming the
offending task.

A successful link toasts with an **Undo** button rather than asking first. In the queue panel the middle band works
across columns; that is exactly how a cross-project dependency gets made.

## Storage

A task is queued when its `queue_order` is not NULL; queued tasks run in `queue_order ASC` order. Unlike `sort_order`
this is not fractional: every edit rewrites the column as a dense `1..n` sequence, which stays cheap because a queue is
short by nature.

That sequence stays **global** across all projects -- the columns are a view of it. The worker only ever needs the
relative order inside a project bucket, which a global sequence already induces, so a per-project numbering would be a
second encoding of the same information. Reordering one column substitutes its members back into the slots that column
already held, leaving every other project untouched.

## CLI

```
ntasker queue list [--json]        # the queue in run order, plus running/paused
ntasker queue add <id...> [--top]  # append (or prepend); an already-queued id moves
ntasker queue rm <id...>           # take entries out
ntasker queue clear                # empty it
ntasker queue start [--host --port]  # resume
ntasker queue pause
ntasker report <id> [--file f.md]  # store the agent's final report (Markdown from stdin)
ntasker patch <id> --report "..."  # same; '' clears
```

`ntasker queue add <id>` is the CLI's run button: it appends the task to the queue, exactly like the button on the
board; `--top` jumps the line. There is no separate `ntasker run`.

The CLI only edits the queue and its switch; the running server's worker is what actually starts tasks. `queue start`
therefore probes `/healthz` and points it out when nothing is listening -- otherwise the queue would sit there looking
started while nothing happens. Pass `--host` / `--port` if you run on a non-default bind.

Unlike the API, the CLI refuses a task that is closed, archived or missing instead of dropping it silently: a hand-typed
id deserves to be told.

## API

| Route | What it does |
|---|---|
| `GET /api/queue` | `{enabled, items: [task, ...], skipped: {id: {reason, project, holder}}}` -- full task rows in run order (a queued task may be filtered off the board and the panel still has to render it); `skipped` holds the reasons `lock` / `dirty` / `ended`. |
| `PUT /api/queue` | Body `{ids: [...]}` replaces the whole queue, head first. Ids that are closed, archived or gone are dropped. An empty list clears the queue. Never restarts an `ended` entry. |
| `POST /api/queue/run` | Body `{id}` -- the run button: appends the task to the queue (an already-queued id keeps its place), moves it to `phase=wip` right away and clears its `ended` flag. Returns the queue. |
| `POST /api/queue/resume` | Body `{id}` -- the resume button on an `ended` entry: the worker reopens the task's stored session next tick, in place; the flag clears once it is live. 409 when the task is not queued or has nothing to resume. Returns the queue. |
| `POST /api/projects/quick-run` | Body `{project, prompt?}` -- a Quicktask: creates a `wip` task (placeholder title and blank-prompt run without `prompt`, otherwise the prompt is the task), appends it to the queue. Returns the task. |
| `PUT /api/settings/queue_enabled` | `{"value": "true" \| "false"}` -- the pause switch (default `true`). |
| `PUT /api/settings/quicktasks_bypass_lanes` | `{"value": "on" \| "off"}` -- whether Quicktasks start outside the lanes (default `on`). |

Add, reorder and remove are all the same `PUT`: the frontend owns the ordered list and sends it after every edit, so
there is no partial state to reconcile.

## Where the code lives

| File | Role |
|---|---|
| `src/ntasker/taskqueue.py` | The worker: retire what is finished, start what is next. Ticks every 2s. |
| `src/ntasker/claude_runner.py` | `queue_seed_for_task` (the seed) and `start_detached_session` (spawn with no browser attached). |
| `src/ntasker/app.py` | `/api/queue` routes plus the worker's startup / shutdown hooks. |
| `src/ntasker/cli.py` | `cmd_queue_*` -- the `ntasker queue` subcommands. |
| `src/ntasker/static/app.js` | Panel state, `queueGroups` (the columns), `runNext` + `_openWhenLive`, `_dropZone` + `setDependency`. |
| `src/ntasker/static/style.css` | `.task-queue*` (rail, columns) and `.drop-link` (the dependency drop). |

A queued run lands in the same session registry as any other run, so it shows up in the busy indicators and the run-view
tab strip. You can open its terminal at any point to watch it or take over.
