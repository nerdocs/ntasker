---
name: ntasker
description: >
  ntasker -- lightweight local task tracker (FastAPI + SQLite).
  Load when any message contains: #<digits>, Task #N, TODO #N, Tracker #N,
  "tasks.db", "ntasker", "nerdocs-tracker", "Tracker", "Aufgaben-Liste",
  or an explicit user write-command ("create a task", "add a todo",
  "leg einen Task an", "trag das ein", "neuer Task").
  Also load when the user asks for open tasks or what to do next in the
  current project ("offene Tasks", "open tasks", "was soll ich als
  nächstes machen", "what should I work on next", "what's next", "nächste
  Aufgabe", "woran arbeiten", "todo in diesem Projekt") -- then suggest
  the next tasks ranked by urgency (see section 3.1).
  Note: `nerdocs-tracker` and `Tracker` remain trigger words as legacy
  aliases for installs that migrated from the pre-1.0.0 package name.
  Hard rule: NO agent creates, deletes or closes tasks autonomously. The
  autonomous writes are moving a task to phase=wip when started via /task
  (since v2.2.0) and to phase=review on completion (since v1.5.0);
  status=done is allowed ONLY on the user's explicit instruction, archival
  stays user-only.
---

# ntasker Skill

## 1. What is ntasker

| Item | Value |
|---|---|
| Package | `ntasker` (PyPI) |
| DB (default) | `~/.local/share/nTasker/tasks.db` (`platformdirs.user_data_dir`) |
| DB precedence | `--db <path>` > `NTASKER_DB` env > platformdirs default |
| API | `http://127.0.0.1:8766` (when server is running) |
| Bind | `127.0.0.1` only -- never expose externally |
| Layout | PyPA src-layout, package `src/ntasker/`; CLI = `ntasker` |

Legacy package name `nerdocs-tracker` was renamed to `ntasker` in v1.0.0.
Old memory entries that still say `nerdocs-tracker` / `Tracker` resolve
to this skill via the legacy trigger words above.

### 1.1 Server availability (lazy auto-start, since v1.4.0)

Every HTTP call in this skill assumes the server answers on
`http://127.0.0.1:8766`. If a call fails with connection-refused, start
the server in the background once -- idempotent, no harm if it is
already up:

```bash
curl -sf http://127.0.0.1:8766/healthz >/dev/null \
  || ntasker serve --detach
```

`/healthz` is a DB-free liveness probe (`{"ok": true, "version": "..."}`).
`ntasker serve --detach` spawns a detached background server cross-platform
and exits 0 once `/healthz` answers (or immediately if a server is
already running). If `ntasker` is not on PATH yet, install it first:
`uv tool install ntasker` (or `pip install --user ntasker`).

To stop a running server (e.g. after a version upgrade): `ntasker stop`
posts to `/shutdown`, polls `/healthz` until the server is gone, and
exits 0. Idempotent -- stopping an already-stopped server is a no-op.

The `/task <id>` slash command handles this transparently via its loader
-- only direct `curl` calls in this skill need the pre-probe.

## 2. #ID Resolution (read -- always allowed)

Server first; CLI fallback if it is not running. **No direct SQLite access**
(the DB path is no longer hardcoded).

**Server:**
```bash
curl -s http://127.0.0.1:8766/api/tasks/43
```
Response includes a `tags` list and a `depends` list. Each `depends` entry
is `{id, title, done}` -- the tasks this one depends on. A task is *blocked*
while any dependency has `done=false` (the UI flags it; derive it the same
way).

**CLI fallback (also resolves DB path):**
```bash
ntasker show 43 --json
```

## 3. Filtering / Listing / Stats

`GET /api/tasks` -- query parameters (all optional, multi-value = OR within
param, AND across params):

| Param | Values | Notes |
|---|---|---|
| `project` | any string or `__none__` | `__none__` = project IS NULL. Projects are *derived* from tasks; no whitelist. |
| `phase` | `planned`, `wip`, `review` | NOT NULL since v2.0, default `planned` |
| `tag` | any tag name | task has >=1 matching tag |
| `search` | free text | over title + description; if value is purely digits (with optional leading `#`), also matches `tasks.id` exactly |
| `status` | `open` / `done` | |
| `archived` | `true` / `false` | |
| `priority` | `critical` / `high` / `normal` / `low` | NOT NULL, no `__none__` |

Equivalent CLI:
```bash
ntasker list --project myproject --phase wip --priority high
ntasker list --json   # raw
ntasker stats         # tab-counts
```

Additional endpoints:
- `GET /api/stats?<same filters>` -> `{open, done, archive}`
- `GET /api/projects` -> `[{name, open_count}, ...]` -- `__none__` first;
  the list is the union of `SELECT DISTINCT project FROM tasks` and the
  Claude projects discovered under `~/.claude/projects`. Discovered names
  are relativized against the `projects_base` setting when set (a folder
  right under the base becomes its bare name), else against `~`. A
  task-derived project disappears once its last task is gone.
- `GET /api/phases` -> 3 fixed entries (`planned` / `wip` / `review`)
- `GET /api/priorities` -> 4 fixed entries (`critical` / `high` / `normal` / `low`)
- `GET /api/tags` -> `[{name, open_count}, ...]`

### 3.1 Suggesting the next task (current project)

When the user asks what to work on next, or for the open tasks in the
current project (no explicit `#<id>`), suggest -- never start -- the most
urgent ones:

1. **Resolve the current project** from the working directory. Read the
   base with `ntasker config get projects_base` (or `GET
   /api/settings/projects_base`); the project name is your cwd made
   relative to that base, else to `~`. If you are in a subfolder, use the
   nearest ancestor that appears in `GET /api/projects`. If nothing
   matches, tell the user and stop -- do not guess across projects.
2. **Fetch open, non-archived tasks** for that project:
   ```bash
   curl -s 'http://127.0.0.1:8766/api/tasks?project=<name>&status=open&archived=false'
   ```
   (CLI: `ntasker list --project <name> --status open --json`.) Each task
   carries `priority`, `phase`, and `depends` (see section 2).
3. **Rank by urgency:**
   - A task is **blocked** while any entry in `depends` has `done=false`.
     Blocked tasks can't be started -- list them separately, not as a
     "do next" pick.
   - Order the actionable (un-blocked) tasks by `priority`:
     `critical` > `high` > `normal` > `low`.
   - Tie-break: `wip` (already in progress) before `review` before
     `planned`, then oldest `created_at` first.
4. **Present** the top few as a short ranked list, each cited as
   `#<id> <title>` with its priority and phase, and name any blocked
   tasks (with what they wait on). Recommend the single best next pick.
   Suggesting is a read action -- do not set `phase`/`status` or create
   anything without an explicit instruction (see section 5). Starting one
   is the user's call (often via `/task <id>`).

## 4. Settings

KV-store with validators. UI: `/settings`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/settings` | List of all settings |
| GET | `/api/settings/{key}` | Single entry, or 404 |
| PUT | `/api/settings/{key}` | Body `{"value": "..."}` -> 200 / 400 on validation fail |
| DELETE | `/api/settings/{key}` | 204 / 404 |

CLI:
```bash
ntasker config list [--json]
ntasker config get <key>
ntasker config set <key> <value>
ntasker config unset <key>
```

Known keys:
- `default_view` -- initial view on a fresh browser: `list` or `kanban`.
  ENV override: `NTASKER_DEFAULT_VIEW`. The frontend remembers the last
  user choice in localStorage; this setting only kicks in on first load.
- `language` -- UI language: `auto` | `en` | `de` (default `auto`).
- `assets_mode` -- vendor asset loading: `cdn` | `local` | `auto`.
- `projects_base` -- base path for project names (e.g. `~/Projekte`).
  Discovered Claude projects below it are shown relative to it (the folder
  right under the base becomes the project name); unset = home-relative.
  ENV override: `NTASKER_PROJECTS_BASE`.

(The pre-v2.0 `projects_dir` setting has been removed -- projects now
live in the DB, derived from the tasks themselves.)

## 5. Write Rules -- HARD LIMIT

**Creation, deletion, closing and archival are never autonomous.** No
agent may, *without an explicit user instruction*,
- create a new task (POST /api/tasks),
- delete a task (DELETE /api/tasks/<id>),
- archive a task ({"archived": true}),
- set a task to `status=done`.

Action items the agent identifies itself belong in the agent's report,
not in the tracker.

**Closing on explicit request (since v2.4):** if the user explicitly
tells the agent to close `#<id>` (e.g. "set #43 to done", "mark it
done", "schliess #43 ab"), the agent MAY send `{"status":"done"}` (or
`ntasker done <id>`). What stays forbidden is closing a task on the
agent's own initiative -- the trigger must come from the user.

**Two autonomous writes are allowed.** First, starting a task via `/task
<id>` moves it to `phase=wip` ("In Arbeit") automatically -- the loader
does this on load (skipped for archived / `status=done` tasks, and on a
**project mismatch**: if the cwd is not inside the task's project dir the
loader emits a `WARNUNG -- Projekt-Mismatch` banner, defers `phase=wip`,
and the agent must ask the user before starting). Second,
the review-handoff (see section 6): when the user assigned `#<id>` and the
agent has finished its part, it moves the task to `phase=review` so the
user can validate and close it. Neither is "marking the task done" --
autonomous closing and archival stay forbidden.

## 6. Review-Handoff on Agent-Side Completion (since v1.5.0)

When the user assigned `#<id>` and the agent considers its work done:

```bash
curl -s -X PATCH http://127.0.0.1:8766/api/tasks/43 \
  -H 'Content-Type: application/json' \
  -d '{"phase": "review"}'
```
Or:
```bash
ntasker patch 43 --phase review
```

The card then shows up in the kanban "Review" column (UI: "Zu prüfen")
for the user to verify. The user normally flips the task to `status=done`
themselves -- via the kanban Done column, the list-view checkbox, or:

```bash
ntasker done 43
```

`completed_at` is set on `done`. The agent runs `done` only when the
user explicitly asks it to close the task (see section 5), never as part
of the handoff. Archiving stays a manual decision -- never archive on the
user's behalf.

**When NOT to move to review:** if the agent could not finish (blocker,
missing info, failed verification), leave the phase as-is and report the
blocker. Review is a *handoff*, not a *give-up* signal.

## 7. Creating Tasks (only on the user's explicit instruction)

```bash
curl -s -X POST http://127.0.0.1:8766/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "project": "myproject",
    "title": "Short title",
    "description": "...",
    "phase": "planned",
    "priority": "high",
    "tags": ["refactoring"],
    "depends": [12, 15]
  }'
```
Or:
```bash
ntasker add --project myproject --title "Short title" \
  --phase planned --priority high --tag refactoring --depends 12,15
```

Field rules: `project` = any non-empty trimmed string OR `null`
(cross-project). Projects are not pre-registered -- a new project name
is created implicitly when the task is saved, and disappears
automatically when its last task is deleted. `title` required; `phase`
in `{planned, wip, review}` (default `planned`); `priority` in
`{critical, high, normal, low}` (default `normal`); `tags` = List[str];
`depends` = List[int] of task ids (write sends ids; read returns
`{id, title, done}`). On PATCH, `depends` replaces the whole set (`[]`
clears it). The set is validated server-side: self-reference, missing
targets, and cycles (the graph stays a DAG) are rejected with HTTP 400.

**Picking a project name (autonomous behaviour, since v2.0):** when the
user asks you to create a task and didn't specify a project, infer one
from the working directory context: the basename of the active project
folder, the repo name, or `null` if you're operating cross-project.
Reuse an existing name (see `GET /api/projects`) before inventing a new
one. Empty/whitespace names collapse to `null` server-side, so passing
either `""` or `null` for "no project" both work.

## 8. Schema

| Field | Type | Notes |
|---|---|---|
| `id` | INT PK | #<id> reference |
| `project` | TEXT NULL | free-form string or NULL; defines a project implicitly |
| `title` | TEXT | required |
| `description` | TEXT | Markdown OK |
| `status` | TEXT | `open` / `done` |
| `phase` | TEXT NOT NULL | `planned` (default) / `wip` / `review` |
| `priority` | TEXT NOT NULL | `critical` / `high` / `normal` / `low` (default `normal`) |
| `created_at` | TEXT | UTC ISO |
| `completed_at` | TEXT NULL | UTC ISO, auto-set on done |
| `archived` | INT | 0/1 -- task remains searchable |
| tags | n:m | via `tags` + `task_tags` tables |
| depends | n:m | via `task_deps(task_id, depends_on_id)`, FK CASCADE; kept acyclic |
| settings | KV | `key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT` |

Workflow phases read left-to-right in the kanban view:
`planned` -> `wip` -> `review`. ``done`` is not a phase value but a
``status`` (and a fourth kanban column derived from it).

Phase symbols for inbox-style reports:
`Planned` planned, `Wip` wip, `Review` review, `Done` done

## 10. Migration from pre-v2.0

v2.0 brings two breaking changes:

1. **Phases:** legacy ``later`` + NULL collapse into ``planned``; new
   value ``review`` added. ``init_db`` migrates idempotently on first
   boot. The ``__none__`` phase sentinel is gone from the API.
2. **Projects:** the ``projects_dir`` setting and the filesystem scan
   are gone. Projects are now derived from ``SELECT DISTINCT project
   FROM tasks`` -- a project exists only as long as at least one task
   references it. Existing ``tasks.project`` values are kept as-is;
   no data migration runs. A stale ``projects_dir`` row in ``settings``
   is harmless and can be deleted via the UI.

## 9. Inter-Agent Report Conventions

Always cite tasks as `#<id> <Title>` or `Task #<id>` in any inter-agent
note, follow-up, or status report so the user has a direct anchor back
into the tracker.
