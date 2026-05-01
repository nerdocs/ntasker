# Changelog

All notable changes to ntasker.

Format: [Keep a Changelog](https://keepachangelog.com), SemVer.

## [1.0.0] — 2026-05-01
- BREAKING: Renamed package from nerdocs-tracker to ntasker.
- BREAKING: DB path moved to platformdirs default — see migration note in README.
- Added: CLI with subcommands (init, serve, list, show, add, done, patch, tag-add, tag-rm, stats, config).
- Added: Settings KV-store with /settings UI page and /api/settings endpoints.
- Added: projects_dir is now configurable (Settings UI or NTASKER_PROJECTS_DIR env).
- Changed: src-Layout (PyPA standard).

## [0.4.0] — 2026-05-01
- Neues Feld `priority` (low/normal/high/critical) mit Sidebar-Filter und Badge.

## [0.3.4] — 2026-05-01
- Version badge moved from page-title to navbar-brand (top bar, next to "nerdocs Tracker").

## [0.3.3] — 2026-05-01
- Version badge in page header (next to "Aufgaben" title).

## [0.3.2] — 2026-05-01
- Bugfix: cache-buster `?v=<version>` on all assets + `Cache-Control: no-store`; resolves browser-cache phantom buttons.

## [0.3.1] — 2026-05-01
- Bugfix: trash-button visibility bound to `task.archived` (data truth) instead of `tab === 'archive'`.

## [0.3.0] — 2026-05-01
- Tag-cleanup button (`POST /api/tags/cleanup`) in page header.
- Phase filter as multi-select sidebar checkboxes; new `/api/phases` endpoint.
- Phase combobox removed from page body.

## [0.2.0] — 2026-05-01
- Clickable project / phase / tag badges toggle filters.
- Generic tags per task with autocomplete; `tags` + `task_tags` schema; `/api/tags` endpoint; multi-OR tag filter combined AND with project.
- Sidebar open-task counts per filter entry; `/api/projects` enriched.
- `source` column dropped (DB + UI).

## [0.1.1] — 2026-05-01
- Vendored Tabler / Tabler-Icons / AlpineJS to `static/vendor/`; offline-capable, no CDN at runtime.
- Multi-project filter sidebar (left) with checkboxes, `__none__` sentinel for NULL project.
- Task IDs prominent (`#<id>` monospace badge before title) with click-to-copy + toast.
- New endpoints: `GET /api/tasks/{id}`, `GET /api/stats`.

## [0.1.0] — 2026-05-01
- Initial release: FastAPI + SQLite + AlpineJS + Tabler.
- Schema: `tasks(id, project, title, description, status, phase, created_at, completed_at, archived)`.
- Three tabs: open / done / archive. Project filter, phase filter, full-text search.
- Bind 127.0.0.1:8766, single-user, no auth.
