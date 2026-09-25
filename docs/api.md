# HTTP API and schema

The server speaks plain JSON over HTTP on `127.0.0.1:8766`. There is **no authentication** -- see
[Bind and exposure](configuration.md#bind-and-exposure).

Interactive OpenAPI docs: <http://127.0.0.1:8766/api/docs>

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/` | The single-page task UI |
| GET | `/settings` | The settings UI |
| GET | `/api/changes` | Cheap change token (`{v}` = DB file mtime in ns). The UI polls it and refetches only when it changed, so CLI/API writes surface live. See [live-updates.md](live-updates.md). |
| GET | `/api/projects` | `[{name, open_count}]`, `__none__` first; sets `X-Settings-Missing: projects_dir` if unconfigured |
| GET | `/api/tags` | `[{name, open_count}]`, sorted by `open_count DESC, name ASC` |
| POST | `/api/tags/cleanup` | Delete dangling tags (no `task_tags` row). Returns `{removed, removed_names}`. Idempotent. |
| GET | `/api/phases` | `[{value, label, open_count}]`, fixed workflow order: `wip`, `planned`, `later`, `__none__` |
| GET | `/api/priorities` | `[{value, label, open_count}]`, fixed order: `critical`, `high`, `normal`, `low` |
| GET | `/api/tasks` | Filters: `project` (multi), `tag` (multi, OR), `phase` (multi, OR; `__none__` = phase IS NULL), `priority` (multi), `status`, `archived`, `search`. Filters across params combine with **AND**. |
| GET | `/api/tasks/{id}` | Single task incl. `tags` |
| GET | `/api/stats` | Tab counts (`open`/`done`/`archive`/`inbox`), respects all filters |
| POST | `/api/inbox` | `{text, source?}` -> 201 inbox row; the triage turns it into a proposal ([inbox.md](inbox.md)) |
| GET | `/api/inbox` | `{items, tasks}`: notes not yet triaged + proposals; the only feed serving proposed tasks |
| POST/DELETE | `/api/inbox/{id}[/retry]` | Retry a failed note (409 unless failed) / drop a note |
| POST | `/api/tasks/{id}/accept` | `{project?}` turns a proposal into a task (`null` = cross-project); 409 unless proposed |

| PUT/POST | `/api/projects/summary[/regenerate]` | Edit / regenerate the project summary the triage sees |
| POST | `/api/tasks` | `{project?, title, description?, phase?, priority?, tags?}` |
| PATCH | `/api/tasks/{id}` | Any subset of `{title, description, project, phase, priority, status, archived, tags}` -- `tags` is a **full replace** |
| DELETE | `/api/tasks/{id}` | Hard delete (the UI archives by default) |
| GET | `/api/settings` | List all settings rows |
| GET | `/api/settings/{key}` | Single setting or 404 |
| PUT | `/api/settings/{key}` | `{value: "..."}` -- 200 on accept, 400 if a registered validator rejects |
| DELETE | `/api/settings/{key}` | 204 on success, 404 if not present |
| GET | `/api/queue` | `{enabled, items[]}` -- the auto-run task queue in run order. See [task-queue.md](task-queue.md). |
| PUT | `/api/queue` | `{ids: [...]}` replaces the whole queue, head first. Closed / archived / missing ids are dropped. |
| GET | `/api/agents` | Read-only registry feed: per-agent availability + `/task` integration status, plus the default |
| GET | `/api/plugins` | Built-in plugins + `enabled` flag; toggle via `PUT /api/settings/plugins_disabled` / `plugins_enabled` |
| WS | `/api/voice/ws` | Voice plugin: 16 kHz PCM in, `partial` / `final` text out ([voice.md](voice.md)) |
| GET/POST | `/api/voice/models[/{name}\|/job]` | Voice plugin: installed models + catalog, background model download |
| GET/POST/DELETE | `/api/tasks/{id}/context[/{cid}]` | Attachments ([task-context.md](task-context.md)) |
| GET/PUT/POST | `/api/workspace[/file\|browse\|entry\|rename\|delete\|reveal]` | Workspace ([workspace.md](workspace.md)) |
| GET | `/api/claude-assets/status` | Read-only: `{installed, drift, package_version, claude_home, files[]}` |

## Schema

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    phase TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    agent TEXT,                      -- AI agent for this task; NULL = default_agent setting (then claude)
    proposed INTEGER NOT NULL DEFAULT 0,  -- inbox proposal awaiting the user; never listed, queued or run
    triage TEXT                      -- JSON: the triage output + the raw note; NULL for hand-made tasks
);
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE task_tags (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (task_id, tag_id)
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`status`: `open` | `done`. `phase`: `wip` | `planned` | `later` | NULL.
`priority`: `critical` | `high` | `normal` | `low` (NOT NULL, default `normal`).
Tag names are normalised to lowercase on write; `UNIQUE COLLATE NOCASE` keeps it tidy.
`GET /api/tasks` never returns a task with `proposed = 1` -- see [inbox.md](inbox.md) for the `inbox`,
`project_summaries` and `triage_examples` tables.


## Design notes

- DB init on startup; pure idempotent `CREATE TABLE IF NOT EXISTS`. Legacy columns are dropped or added in
  `try/except OperationalError` blocks -- no Alembic, no migration files.
- All SQL parameterised (`?`); no string interpolation.
- Project list is read live each request from the symlinks under the configured `projects_dir` -- no caching.
- Sidebar `open_count` values are absolute (always count all open + non-archived tasks), so toggling filters does not
  flicker the sidebar.
- Hard-delete is intentionally rare; archive is the default. Deleting a task cascades through `task_tags` but leaves
  `tags` rows in place (zero-cost dangling).
- Project / phase / tag / priority badges in a task row are clickable: each one toggles the matching filter.
  `@click.stop` prevents the parent row interactions.
- Dates stored as UTC ISO strings, rendered locally via `Intl.RelativeTimeFormat`.
