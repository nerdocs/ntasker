---
name: ntasker
description: >
  ntasker -- lightweight local task tracker (FastAPI + SQLite).
  Load when any message contains: #<digits>, Task #N, TODO #N, Tracker #N,
  "tasks.db", "ntasker", "nerdocs-tracker", "Tracker", "Aufgaben-Liste", or
  an explicit Christian write-command ("leg einen Task an", "trag das ein",
  "neuer Task"). Aliase `nerdocs-tracker` und `Tracker` bleiben Trigger-
  Words (alte Memory-Eintraege).
  Hard rule: NO agent writes tasks autonomously -- only on Christian's explicit instruction.
---

# ntasker Skill

## 1. What is ntasker

| Item | Value |
|---|---|
| Package | `ntasker` (PyPI) |
| Repo | `/home/christian/nerdocs/ntasker/` (GitLab: `nerdocs/ntasker`) |
| DB (default) | `~/.local/share/nTasker/tasks.db` (`platformdirs.user_data_dir`) |
| DB precedence | `--db <path>` > `NTASKER_DB` env > platformdirs default |
| API | `http://127.0.0.1:8766` (when server is running) |
| Version | see `src/ntasker/__init__.py:__version__` (currently `1.0.0`) |
| Bind | `127.0.0.1` only -- never expose externally |
| Layout | PyPA src-layout, package `src/ntasker/`; CLI = `ntasker` |

Legacy package name `nerdocs-tracker` is gone (renamed in v1.0.0). Old Memory
references to `nerdocs-tracker` / `Tracker` still resolve via this skill's
trigger words.

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
ntasker list --project medux --phase wip --priority high
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

## 4. Settings (new in v1.0.0)

KV-store with validators. UI: `/settings`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/settings` | Liste aller Settings |
| GET | `/api/settings/{key}` | Einzelner Eintrag oder 404 |
| PUT | `/api/settings/{key}` | Body `{"value": "..."}` -> 200 / 400 bei Validation-Fail |
| DELETE | `/api/settings/{key}` | 204 / 404 |

CLI:
```bash
ntasker config list [--json]
ntasker config get <key>
ntasker config set <key> <value>
ntasker config unset <key>
```

Bekannte Schluessel:
- `projects_dir` -- Pfad zu den nerdocs-Projekt-Symlinks (z.B.
  `/home/christian/nerdocs/Projekte`). Wird von `/api/projects` gelesen.
  ENV-Override: `NTASKER_PROJECTS_DIR`. Validator: absolut + existiert + lesbar.

## 5. Write Rules -- HARD LIMIT

**NO agent may write tasks to the tracker autonomously** -- not from
followups, reports, or self-identified action items. Only an explicit
Christian instruction triggers a write.

Open items from agent reports belong in the report / Christians Inbox,
NOT in the tracker.

Memory cross-link: `feedback_tracker_explicit_only`.

## 6. Status Update on Task Completion

When Christian assigns `#<id>` to an agent and the task is done:

```bash
curl -s -X PATCH http://127.0.0.1:8766/api/tasks/43 \
  -H 'Content-Type: application/json' \
  -d '{"status": "done"}'
```
Or:
```bash
ntasker done 43
```

`completed_at` is set automatically. Archiving (`{"archived": true}`) is
Christian's decision.

## 7. Creating Tasks (only on Christian's explicit instruction)

```bash
curl -s -X POST http://127.0.0.1:8766/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "project": "medux-cashbook",
    "title": "VAT-Rate constants extraction",
    "description": "...",
    "phase": "planned",
    "priority": "high",
    "tags": ["refactoring"]
  }'
```
Or:
```bash
ntasker add --project medux-cashbook --title "VAT-Rate constants extraction" \
  --phase planned --priority high --tag refactoring
```

Field rules: `project` = symlink name from the configured `projects_dir`,
or `null` (cross-project); `title` required; `phase` in `{wip, planned, later, null}`;
`priority` in `{critical, high, normal, low}` (default `normal`); `tags` = List[str].

## 8. Schema

| Field | Type | Notes |
|---|---|---|
| `id` | INT PK | #<id> reference |
| `project` | TEXT NULL | symlink name or NULL |
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

Phase symbols for Inbox reports:
`Wip` wip · `Planned` planned · `Later` later · `Done` done · `?` null/no phase

## 9. Inter-Agent Report Conventions

Always cite tasks as `#<id> <Title>` or `Task #<id>` in every inbox note,
followup, or status report so Christian has a direct anchor into the tracker.

## 10. ntasker Code Maintenance (HERMINE only)

Stack constraints (see `/home/christian/nerdocs/ntasker/src/ntasker/`):
- FastAPI + stdlib `sqlite3` (no ORM), AlpineJS + Tabler.io vendored -- no build step.
- `__version__` in `src/ntasker/__init__.py` is single source of truth;
  assets load with `?v=<__version__>` cache-buster. `pyproject.toml` keeps it in sync.
- Schema migrations: run at boot, idempotent (`try/except` on "no such column").
- Bind: `127.0.0.1:8766` default in `ntasker serve`; CLI flags `--host` / `--port` exist.
- DB path: NEVER hardcode `/home/christian/nerdocs/...` -- always go through
  `ntasker.paths.resolve_db_path()` or `NTASKER_DB`.
- Settings reads: `from ntasker.settings import get_setting` (or
  `get_projects_dir()` for that specific helper).
- Tracker-Repo darf committen + taggen nach Christians OK pro Feature
  (Memory `feedback_tracker_repo_commits`).

## 11. Memory Cross-links

- `feedback_tracker_explicit_only` -- no autonomous writes
- `feedback_no_git_commits` -- never commit other repos
- `feedback_tracker_repo_commits` -- ntasker-Repo darf nach OK committen
- `feedback_tracker_id_reference` -- Christian referenziert Tasks per `#<id>`
- `feedback_doc_writing_style` -- keep docs concise, crosslink over repeat
