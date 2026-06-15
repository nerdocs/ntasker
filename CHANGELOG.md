# Changelog

All notable changes to ntasker.

Format: [Keep a Changelog](https://keepachangelog.com), SemVer.

## [2.1.0] — 2026-06-15
Projekte werden aus `~/.claude/projects` erkannt (`/`-Pfadnamen); `projects list|migrate` CLI; `/api/projects` als Union.

## [2.0.0] — 2026-05-18
**Breaking:** Projekte sind keine Filesystem-Symlinks mehr, sondern werden aus den Tasks abgeleitet.
- breaking: Setting `projects_dir` + ENV `NTASKER_PROJECTS_DIR` + die Filesystem-Scan-Logik in `list_projects()` ersatzlos entfernt. `validate_projects_dir`-Validator weg; `init_db()` löscht bestehende `projects_dir`-DB-Rows idempotent beim Boot, damit keine Karteileiche im Settings-UI verbleibt.
- breaking: Response-Header `X-Settings-Missing: projects_dir` ist weg. Frontend-Banner „Bitte Projekte-Verzeichnis konfigurieren..." entfernt.
- feat: `/api/projects` baut die Projektliste jetzt aus `SELECT DISTINCT project FROM tasks`. Projekte entstehen implizit beim Anlegen eines Tasks mit beliebigem `project`-Namen und werden automatisch entfernt, sobald der letzte Task das Projekt nicht mehr referenziert. Keine Projekt-Leichen.
- feat: Server normalisiert `project`-Werte (trim; leerer String → NULL) bei POST und PATCH, damit kein Phantom-Eintrag in der Sidebar entsteht.
- feat: Frontend: `<select>` für Projekte → freie Texteingabe mit `<datalist>`-Autocomplete (alle bisherigen Projektnamen). Anlegen eines neuen Projekts ist ein Side-Effect des Task-Speicherns.
- feat: Task-Löschung direkt aus dem Edit-Modal (btn-ghost-danger im Footer, Confirm-Dialog). Backend hatte die Restriktion nie -- jetzt auch in der UI verfügbar. Der Listen-Delete-Button bleibt aus Sicherheit archived-only.
- feat: Neuer CLI-Befehl `ntasker delete <id>` mit `--yes` für Skripte. Funktioniert unabhängig vom archived-State.
- feat: Cache-Buster für `/static/`-Dateien jetzt `<__version__>-<mtime>` statt nur `<__version__>` -- Browser laden geänderte `app.js`/`style.css` zuverlässig neu, auch innerhalb derselben Release-Periode.
- feat: page-wrapper auf `container-fluid` umgestellt; Inhalt nutzt jetzt die volle Browser-Breite, statt bei `container-xl` (≈1320 px) abzuschneiden.
- feat: SKILL.md instruiert Claude, beim autonomen Task-Anlegen (auf User-Aufforderung) einen sinnvollen Projektnamen aus dem Working-Directory-Kontext zu wählen und vorhandene Namen wiederzuverwenden.

## [1.5.0] — 2026-05-18
- feat: Kanban-View neben der klassischen Aufgabenliste. View-Toggle im Page-Header (Aufgabenliste / Kanban). 4 Spalten Geplant -> In Arbeit -> Zu prüfen + kollabierbare Erledigt-Spalte (HTML5-Drag&Drop, PATCH bei Drop). Klick auf Card-Titel toggelt die Beschreibung (analog zur Liste).
- feat: neues Setting `default_view` (`list` | `kanban`, Default `list`; ENV `NTASKER_DEFAULT_VIEW`) bestimmt die Start-Ansicht bei einem frischen Browser; `localStorage` überlebt danach die User-Wahl.
- feat: Phase-Vokabular neu definiert -- `planned` -> `wip` -> `review`. `later` und NULL werden idempotent in `init_db()` zu `planned` migriert; `phase` ist jetzt `NOT NULL DEFAULT 'planned'`. API akzeptiert weiterhin `phase=null` (coerce zu `planned`) für Pre-1.5-Clients.
- feat: SKILL.md + `/task <id>` umgestellt auf Review-Handoff -- bei Fertigstellung eines zugewiesenen Tasks setzt Claude jetzt automatisch `phase=review`. Status=done und Archivierung bleiben User-only. Voll out-of-the-box: `ntasker install-claude-assets` deployt die neue Skill-Version.
- docs: `docs/kanban.md` -- Phasen-Mapping, DnD-Semantik, default_view Resolution-Order, Migrationshinweise.

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
