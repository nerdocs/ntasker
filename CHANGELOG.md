# Changelog

All notable changes to ntasker.

Format: [Keep a Changelog](https://keepachangelog.com), SemVer.

## [2.14.0] — 2026-07-01
Agent-agnostic runs (Claude/OpenCode/Pi); drag-and-drop sortable tasks; new project dirs on run; project chips into sessions; settings radio/number widgets.

## [2.13.0] — 2026-06-29
Run-view session tabs + project quick-add; ranked project-field autocomplete & running-project chips; ENTER-to-save dialogs.

## [2.12.0] — 2026-06-28
`ntasker service restart` (deploy hook); defers while task sessions run; settings restart button disables + warns.

## [2.11.0] — 2026-06-28
`ntasker service install/uninstall/status` (systemd + launchd) and `ntasker self-update`; `update_command` setting.

## [2.10.0] — 2026-06-28
Tag management page + chip caret-nav, card mark-done icon, Tab tag-complete; fixes: kill Claude on done, tag pane layout.

## [2.9.0] — 2026-06-28
Search box moved to the top-right of the page-header (page-actions), compact width.

## [2.8.0] — 2026-06-27
Claude sessions are flagged per task: amber "waiting for input", tinted "running"; the busy indicator self-heals.
Run button shows the Claude logomark and is hidden on done tasks; `/task` can `/cd` into the project dir.
New Settings switch: auto mode runs Claude without permission prompts (with a clear danger warning).

## [2.7.0] — 2026-06-27
Run with Claude: a per-task button opens the real interactive Claude Code TUI (PTY + xterm.js) right in the web UI.
Sessions run in the background and reattach; marking a task done ends its session.

## [2.6.0] — 2026-06-26
Projects sidebar hides projects with no open tasks by default; a "Show empty projects" switch brings them back.

## [2.5.3] — 2026-06-25
Live updates: the web UI refreshes itself on any CLI/API change (polls `/api/changes`, reloads only on change).
New-task form is a collapsed accordion; Tags + "Depends on" share one row; focus jumps to the Project field on open.

## [2.5.2] — 2026-06-25
CLI accepts task IDs with a leading `#` (`ntasker patch #311`); skill/`/task` loader clarify ID form for CLI vs. HTTP.

## [2.5.1] — 2026-06-25
Skill: loads on "open tasks" questions and suggests the current project's next tasks ranked by urgency.

## [2.5.0] — 2026-06-25
`/task` warns on a project mismatch (cwd not in the task's project), asks, and sets `phase=wip` only after confirmation.
New setting `projects_base`: discovered Claude projects appear relative to it (`~/Projekte/medux` → `medux`).
Fixes: `~/.claude` symlink no longer resolved; depends badge only when there are dependencies; dependency-dropdown spacing.

## [2.4.0] — 2026-06-25
Skill/command: Claude may set tasks to `status=done` when the user explicitly asks (still never autonomously).

## [2.3.0] — 2026-06-23
Task dependencies: `depends` (n:m, acyclic) with autocomplete input, blocked badge, API and CLI support.

## [2.2.0] — 2026-06-22
`/task <id>` automatically sets the task to `phase=wip` ("In Progress") on start; skipped for archived/done tasks.

## [2.1.1] — 2026-06-22
Fix: a task created via the UI under an active filter was silently hidden -- now a success toast or a "hidden by filter" hint.

## [2.1.0] — 2026-06-15
Projects are discovered from `~/.claude/projects` (`/`-path names); `projects list|migrate` CLI; `/api/projects` as a union.

## [2.0.0] — 2026-05-18
**Breaking:** projects are no longer filesystem symlinks -- they are derived from the tasks themselves.
- breaking: setting `projects_dir` + ENV `NTASKER_PROJECTS_DIR` + the filesystem-scan logic in `list_projects()` removed outright. `validate_projects_dir` validator gone; `init_db()` idempotently deletes existing `projects_dir` DB rows on boot so no stale entry lingers in the settings UI.
- breaking: response header `X-Settings-Missing: projects_dir` is gone. Frontend banner "Please configure the projects directory..." removed.
- feat: `/api/projects` now builds the project list from `SELECT DISTINCT project FROM tasks`. Projects come into being implicitly when a task is created with any `project` name and disappear automatically once the last task stops referencing them. No project leftovers.
- feat: the server normalizes `project` values (trim; empty string → NULL) on POST and PATCH so no phantom entry appears in the sidebar.
- feat: frontend: project `<select>` → free-text input with `<datalist>` autocomplete (all existing project names). Creating a new project is a side-effect of saving the task.
- feat: delete a task straight from the edit modal (btn-ghost-danger in the footer, confirm dialog). The backend never had the restriction -- now available in the UI too. The list delete button stays archived-only for safety.
- feat: new CLI command `ntasker delete <id>` with `--yes` for scripts. Works regardless of archived state.
- feat: cache-buster for `/static/` files is now `<__version__>-<mtime>` instead of just `<__version__>` -- browsers reliably reload changed `app.js`/`style.css`, even within the same release window.
- feat: page wrapper switched to `container-fluid`; content now uses the full browser width instead of being cut off at `container-xl` (≈1320 px).
- feat: SKILL.md instructs Claude to pick a sensible project name from the working-directory context when creating tasks (on user request) and to reuse existing names.

## [1.5.0] — 2026-05-18
- feat: kanban view alongside the classic task list. View toggle in the page header (Task list / Kanban). 4 columns Planned -> In Progress -> Review + a collapsible Done column (HTML5 drag & drop, PATCH on drop). Clicking a card title toggles the description (as in the list).
- feat: new setting `default_view` (`list` | `kanban`, default `list`; ENV `NTASKER_DEFAULT_VIEW`) sets the initial view on a fresh browser; `localStorage` keeps the user's choice afterwards.
- feat: phase vocabulary redefined -- `planned` -> `wip` -> `review`. `later` and NULL are idempotently migrated to `planned` in `init_db()`; `phase` is now `NOT NULL DEFAULT 'planned'`. The API still accepts `phase=null` (coerced to `planned`) for pre-1.5 clients.
- feat: SKILL.md + `/task <id>` switched to a review handoff -- on finishing an assigned task Claude now sets `phase=review` automatically. Status=done and archiving stay user-only. Fully out-of-the-box: `ntasker install-claude-assets` deploys the new skill version.
- docs: `docs/kanban.md` -- phase mapping, DnD semantics, default_view resolution order, migration notes.

## [1.4.1] — 2026-05-18
- feat: `/task` loader auto-starts the server via `ntasker serve --detach` when the API call fails; web UI becomes available as a side-effect. SKILL.md documents the same pre-probe pattern for direct curl calls.

## [1.4.0] — 2026-05-17
- feat: `ntasker serve --detach` + `GET /healthz` enable lazy auto-start from the Claude Code skill (cross-platform: POSIX `start_new_session`, Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`); idempotent -- probes `/healthz` before spawning. New `python -m ntasker` entry.

## [1.3.0] — 2026-05-02
- feat: full i18n (en/de) for templates, CLI, and Alpine frontend; setting `language = auto|en|de` (default `auto`) reads `Accept-Language` when `auto`. `gettext` + Babel; catalogs under `src/ntasker/locale/`. Make targets `i18n-extract / i18n-update / i18n-compile / i18n`.
- fix: `ntasker serve --reload` (and direct `uvicorn ntasker.app:app` imports) crashed at startup with `init_db called without DB_PATH set` -- lifespan now re-resolves the DB path itself; CLI propagates `--db` via `NTASKER_DB` so the reload-worker subprocess inherits it.

## [1.2.3] — 2026-05-02
- feat: serve vendor assets via CDN by default with SRI; opt-in local cache via `ntasker assets fetch`.

## [1.2.2] — 2026-05-02
- Fixed: /task with #-prefix argument was eaten by bash comment parsing -- $ARGUMENTS now quoted in slash-command template.

## [1.2.1] — 2026-05-01
- Changed: /task workflow now asks for explicit user confirmation before marking a task as done (no autonomous status writes).

## [1.2.0] — 2026-05-01
- Changed: generalised packaged Claude Code assets (skill + slash-command template) -- removed user-specific routing and paths.
- Changed: documentation polished -- "nerdocs Tracker" -> "nTasker", path examples generic.
- Added: AGPL-3.0-or-later license (LICENSE file + pyproject metadata).
- Docs: README explains how `projects_dir` is interpreted (subdirs/symlinks become Projects, on-demand read).
- Fixed: `/task` accepts both "187" and "#187" as argument.
- Added: clicking a task ID copies "/task #<id>" to clipboard for direct paste into Claude Code.
- Note: existing installs see drift in `task.md` after upgrade; run `ntasker install-claude-assets --force` to apply (backup is created automatically).

## [1.1.0] — 2026-05-01
- Added: `install-claude-assets` CLI for installing Claude Code skill and slash-command (`--command-name`, `--force`, `--dry-run`, `--check`, `--claude-home`).
- Added: `GET /api/claude-assets/status` endpoint and read-only Settings UI card.
- Added: drift warning at server boot when installed Claude assets are stale.

## [1.0.0] — 2026-05-01
- BREAKING: Renamed package from nerdocs-tracker to ntasker.
- BREAKING: DB path moved to platformdirs default — see migration note in README.
- Added: CLI with subcommands (init, serve, list, show, add, done, patch, tag-add, tag-rm, stats, config).
- Added: Settings KV-store with /settings UI page and /api/settings endpoints.
- Added: projects_dir is now configurable (Settings UI or NTASKER_PROJECTS_DIR env).
- Changed: src-Layout (PyPA standard).

## [0.4.0] — 2026-05-01
- New field `priority` (low/normal/high/critical) with sidebar filter and badge.

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
