# nerdocs Tracker

Lightweight local task tracker for the nerdocs HQ. Single-user, FastAPI + SQLite, Tabler.io UI.

## Stack

- Backend: FastAPI + uvicorn, Python stdlib `sqlite3`
- Frontend: HTML + AlpineJS + Tabler.io, all assets vendored under
  `static/vendor/` -- offline-capable, no build step, no CDN at runtime
- Storage: `tasks.db` next to `app.py`

Vendored assets and licences: see `static/vendor/LICENSES.md`.

## Bind

Hardcoded to `127.0.0.1:8766`. Do **not** expose this on a network -- there is no auth.
This is a personal local tool, not a multi-user service.

## Setup

```bash
cd /home/christian/nerdocs/tracker
make install      # uv sync
make migrate      # one-shot: import existing TODO.md
make run          # start server -> http://127.0.0.1:8766
```

Open <http://127.0.0.1:8766> in a browser.

## Smoke test

```bash
make smoke
```

Runs an in-process FastAPI test client against a temp DB.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/` | The single-page UI |
| GET | `/api/projects` | `[{name, open_count}]`, `__none__` first, then symlinks |
| GET | `/api/tags` | `[{name, open_count}]`, sorted by `open_count DESC, name ASC` |
| POST | `/api/tags/cleanup` | Delete dangling tags (no `task_tags` row). Returns `{removed, removed_names}`. Idempotent. |
| GET | `/api/phases` | `[{value, label, open_count}]`, fixed workflow order: `wip`, `planned`, `later`, `__none__` |
| GET | `/api/tasks` | Filters: `project` (multi), `tag` (multi, OR), `phase` (multi, OR; `__none__` = phase IS NULL), `status`, `archived`, `search`. project + tag + phase combine with **AND**. |
| GET | `/api/tasks/{id}` | Single task incl. `tags` |
| GET | `/api/stats` | Tab counts (`open`/`done`/`archive`), respects all filters incl. `phase` |
| POST | `/api/tasks` | `{project?, title, description?, phase?, tags?}` |
| PATCH | `/api/tasks/{id}` | Any subset of `{title, description, project, phase, status, archived, tags}` -- `tags` is a **full replace** |
| DELETE | `/api/tasks/{id}` | Hard delete (the UI archives by default) |

OpenAPI: <http://127.0.0.1:8766/api/docs>

## Schema

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    phase TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0
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
```

`status`: `open` | `done`. `phase`: `wip` | `planned` | `later` | NULL.
Tag names are normalised to lowercase on write; `UNIQUE COLLATE NOCASE` keeps it tidy.

## Design notes

- DB init on startup; pure idempotent `CREATE TABLE IF NOT EXISTS`. The legacy
  `source` column on `tasks` is dropped on first run after upgrade
  (SQLite >= 3.35; older runtimes leave the now-unused column in place).
- All SQL parameterised (`?`); no string interpolation.
- Project list is read live from `nerdocs/Projekte/` symlinks each request -- no caching.
- Sidebar `open_count` values are absolute (always count all open + non-archived
  tasks), so toggling filters does not flicker the sidebar.
- Hard-delete is intentionally rare; archive is the default. Deleting a task
  cascades through `task_tags` but leaves `tags` rows in place (zero-cost dangling).
- Project-badge / phase-badge / tag-badge in a task row are clickable: each one
  toggles the matching filter. `@click.stop` prevents the parent row interactions.
- Dates stored as UTC ISO strings, rendered locally via `Intl.RelativeTimeFormat('de-DE')`.

## Migration from TODO.md

`migrate_todo.py` parses the bullet items in `nerdocs/TODO.md` mapping the leading
status emoji to `status` + `phase` + `archived`. Title comes from the leading
`**bold**` chunk; description from the rest of the line. Project is detected by
substring-matching the longest known project symlink name. After a successful run
`TODO.md` is renamed to `TODO.md.migrated-<DATE>` so a re-run does not double-import.

## Changelog

- **0.3.2** -- Fix: cache-bust static assets via `?v={VERSION}` query suffix on every `<link>`/`<script>`; HTML shell served with `Cache-Control: no-store`. After v0.3.1 shipped, browsers were still replaying the pre-fix `app.js`/`index.html` from disk cache, surfacing as the persistent "phantom button" report.
- **0.3.1** -- Fix: delete-button condition rebound from `tab === 'archive'` (UI state) to `task.archived` (data truth); defensive guard in `deleteTask()` rejects non-archived rows.
- **0.3.0** -- Phase filter as multi-select sidebar checkboxes (server-side, OR-combined incl. `__none__`); manual tag-cleanup button in page-header (`POST /api/tags/cleanup`); old phase combobox in body removed.
- **0.2.0** -- Generic per-task tags (multi-OR filter), clickable filter
  badges in task rows, sidebar open-counts per project + tag,
  legacy `source` column removed.
- **0.1.0** -- Initial release: tasks + projects, multi-value project filter,
  one-shot TODO.md migration.
