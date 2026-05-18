---
name: ntasker
description: >
  ntasker -- lightweight local task tracker (FastAPI + SQLite).
  Load when any message contains: #<digits>, Task #N, TODO #N, Tracker #N,
  "tasks.db", "ntasker", "nerdocs-tracker", "Tracker", "Aufgaben-Liste",
  or an explicit user write-command ("create a task", "add a todo",
  "leg einen Task an", "trag das ein", "neuer Task").
  Note: `nerdocs-tracker` and `Tracker` remain trigger words as legacy
  aliases for installs that migrated from the pre-1.0.0 package name.
  Hard rule: NO agent writes tasks autonomously -- only on the user's
  explicit instruction.
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

The `/task <id>` slash command handles this transparently via its loader
-- only direct `curl` calls in this skill need the pre-probe.

## 2. #ID Resolution (read -- always allowed)

Server first; CLI fallback if it is not running. **No direct SQLite access**
(the DB path is no longer hardcoded).

**Server:**
```bash
curl -s http://127.0.0.1:8766/api/tasks/43
```
Response includes `tags` list.

**CLI fallback (also resolves DB path):**
```bash
ntasker show 43 --json
```

## 3. Filtering / Listing / Stats

`GET /api/tasks` -- query parameters (all optional, multi-value = OR within
param, AND across params):

| Param | Values | Notes |
|---|---|---|
| `project` | symlink-name or `__none__` | `__none__` = project IS NULL |
| `phase` | `wip`, `planned`, `later`, `__none__` | `__none__` = phase IS NULL |
| `tag` | any tag name | task has >=1 matching tag |
| `search` | free text | over title + description |
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
- `GET /api/projects` -> `[{name, open_count}, ...]` -- `__none__` first; sets
  `X-Settings-Missing: projects_dir` header if unconfigured
- `GET /api/phases` -> 4 fixed entries (`wip` / `planned` / `later` / `__none__`)
- `GET /api/priorities` -> 4 fixed entries (`critical` / `high` / `normal` / `low`)
- `GET /api/tags` -> `[{name, open_count}, ...]`

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
- `projects_dir` -- path to a directory whose entries (or symlinks) name your
  projects. Read by `/api/projects`. ENV override: `NTASKER_PROJECTS_DIR`.
  Validator: absolute path, exists, is a directory, readable.

## 5. Write Rules -- HARD LIMIT

**No agent may write tasks to the tracker autonomously** -- not from
follow-ups, reports, or self-identified action items. Only an explicit
user instruction triggers a write.

Open items from agent reports belong in the report itself, not in the tracker.

## 6. Status Update on Task Completion

When the user assigns `#<id>` and the task is done:

```bash
curl -s -X PATCH http://127.0.0.1:8766/api/tasks/43 \
  -H 'Content-Type: application/json' \
  -d '{"status": "done"}'
```
Or:
```bash
ntasker done 43
```

`completed_at` is set automatically. Archiving (`{"archived": true}`) stays
a manual decision -- never archive on the user's behalf.

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
    "tags": ["refactoring"]
  }'
```
Or:
```bash
ntasker add --project myproject --title "Short title" \
  --phase planned --priority high --tag refactoring
```

Field rules: `project` = entry name from the configured `projects_dir`,
or `null` (cross-project); `title` required; `phase` in
`{wip, planned, later, null}`; `priority` in
`{critical, high, normal, low}` (default `normal`); `tags` = List[str].

## 8. Schema

| Field | Type | Notes |
|---|---|---|
| `id` | INT PK | #<id> reference |
| `project` | TEXT NULL | entry name or NULL |
| `title` | TEXT | required |
| `description` | TEXT | Markdown OK |
| `status` | TEXT | `open` / `done` |
| `phase` | TEXT NULL | `wip` / `planned` / `later` / NULL |
| `priority` | TEXT NOT NULL | `critical` / `high` / `normal` / `low` (default `normal`) |
| `created_at` | TEXT | UTC ISO |
| `completed_at` | TEXT NULL | UTC ISO, auto-set on done |
| `archived` | INT | 0/1 -- task remains searchable |
| tags | n:m | via `tags` + `task_tags` tables |
| settings | KV | `key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT` |

Phase symbols for inbox-style reports:
`Wip` wip, `Planned` planned, `Later` later, `Done` done, `?` null/no phase

## 9. Inter-Agent Report Conventions

Always cite tasks as `#<id> <Title>` or `Task #<id>` in any inter-agent
note, follow-up, or status report so the user has a direct anchor back
into the tracker.
