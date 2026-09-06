# Task Queue

The task queue is a worklist ntasker works through on its own: press a task's queue button, press **Start**, and
ntasker hands each one to its agent, waits for it to finish, and moves on to the next.

It sits in a panel above the board and is visible in both the list and the kanban view.

Tasks get in through the **queue button** on each row / card -- the one next to the agent's run button. It toggles, so
the same button takes a task back out. Dragging inside the panel reorders the queue; dragging a card *into* it is not a
thing, precisely so the button is the one obvious way in.

## Rules in one paragraph

One task runs **per project** at a time, so several projects progress in parallel while a single project stays strictly
sequential. A task leaves the queue the moment its agent session ends -- whether the task was closed or not. Who closed
it is irrelevant: the agent itself, you from the UI, `ntasker done` on the command line, or a direct DB write all count
the same, because the queue only ever reads the DB.

## The panel

| Element | Meaning |
|---|---|
| The rail down the left of the list | Marches while the queue runs, frozen while it is paused. |
| Position bead (`1`, `2`, `3`) | Run order. Indigo = running, orange = waiting for your input. |
| Lock badge on an entry | The queue is passing this one over -- see [Skipped entries](#skipped-entries). |
| **Running** / **Waiting for your input** | Links into that session's terminal. |
| `✕` | Removes the entry. So does the task's own queue button, which toggles. |

Queued tasks also carry an indigo `⧉ n` badge on their board row / kanban card, so you can see a task's queue position
without looking at the panel.

## Start and pause

The switch is **off by default**: queueing tasks and sorting them never launches an agent by accident. It lives
in the `queue_enabled` setting (not in `localStorage`), so the state survives a restart and every open browser tab
agrees on it.

Pausing stops the queue from starting anything new. A task that is already running keeps going, and still leaves the
queue when its session ends.

## What a queued run is told

A queued run does **not** use the `/task <id>` slash command. It gets a self-contained seed
(`claude_runner.queue_seed_for_task`) which inlines the task and, crucially, grants the one thing the normal tracker
rules withhold: closing the task when the work is done.

> That queue placement IS the user's instruction to close the task, so when the work is done, close it yourself --
> do not ask first: `ntasker done "<id>"`

That close is what advances the queue. The seed also tells the agent what to do when it *cannot* finish: leave the
status open, hand the task to `review`, and report the blocker. The session ending is enough to advance the queue
either way, so a task that cannot be finished never wedges the queue behind it -- it simply drops out and stays on the
board with its phase intact.

## Skipped entries

An entry stays queued but is passed over when

* one of its dependencies is still open (the same guard the kanban drag applies), or
* its agent's CLI is not installed.

The queue then looks at the next entry **in the same project** instead of stalling behind it. Both cases are labelled on
the entry, so a queue that looks idle always says why.

A session you start by hand also occupies its project: two agents in one working directory is exactly what the
one-per-project rule exists to prevent. On a *running* queue, a hand-started session on a queued task is adopted -- it
advances the queue like a queued run would. On a paused queue it is left alone.

## Storage

A task is queued when its `queue_order` is not NULL; queued tasks run in `queue_order ASC` order. Unlike `sort_order`
this is not fractional: every edit rewrites the column as a dense `1..n` sequence, which stays cheap because a queue is
short by nature.

## CLI

```
ntasker queue list [--json]        # the queue in run order, plus running/paused
ntasker queue add <id...> [--top]  # append (or prepend); an already-queued id moves
ntasker queue rm <id...>           # take entries out
ntasker queue clear                # empty it
ntasker queue start [--host --port]
ntasker queue pause
```

The CLI only edits the queue and its switch; the running server's worker is what actually starts tasks. `queue start`
therefore probes `/healthz` and points it out when nothing is listening -- otherwise the queue would sit there looking
started while nothing happens. Pass `--host` / `--port` if you run on a non-default bind.

Unlike the API, the CLI refuses a task that is closed, archived or missing instead of dropping it silently: a hand-typed
id deserves to be told.

## API

| Route | What it does |
|---|---|
| `GET /api/queue` | `{enabled, items: [task, ...]}` -- full task rows in run order (a queued task may be filtered off the board and the panel still has to render it). |
| `PUT /api/queue` | Body `{ids: [...]}` replaces the whole queue, head first. Ids that are closed, archived or gone are dropped. An empty list clears the queue. |
| `PUT /api/settings/queue_enabled` | `{"value": "true" \| "false"}` -- the start/pause switch. |

Add, reorder and remove are all the same `PUT`: the frontend owns the ordered list and sends it after every edit, so
there is no partial state to reconcile.

## Where the code lives

| File | Role |
|---|---|
| `src/ntasker/taskqueue.py` | The worker: retire what is finished, start what is next. Ticks every 2s. |
| `src/ntasker/claude_runner.py` | `queue_seed_for_task` (the seed) and `start_detached_session` (spawn with no browser attached). |
| `src/ntasker/app.py` | `/api/queue` routes plus the worker's startup / shutdown hooks. |
| `src/ntasker/cli.py` | `cmd_queue_*` -- the `ntasker queue` subcommands. |
| `src/ntasker/static/app.js` | Panel state, `toggleQueued` (the board button) and the reorder drag handlers. |
| `src/ntasker/static/style.css` | `.task-queue*` -- including the rail. |

A queued run lands in the same session registry as any other run, so it shows up in the busy indicators and the run-view
tab strip. You can open its terminal at any point to watch it or take over.
