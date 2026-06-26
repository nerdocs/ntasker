# ntasker — Workflow Rules

Lightweight local task tracker. Single-user, no auth, bound to `127.0.0.1:8766`.
**This file = workflow only.** API and schema knowledge live in
`~/.claude/skills/ntasker/SKILL.md`.

## Stack

- **Backend:** FastAPI + Python stdlib `sqlite3` (no ORM, no Alembic).
- **Frontend:** AlpineJS + Tabler.io. **Default = jsDelivr CDN with SRI hashes** (manifest in `src/ntasker/assets.py`).
  Opt-in local cache via `ntasker assets fetch` -- downloads into the user-data dir
  (`platformdirs.user_data_dir("nTasker")/vendor`) and verifies every file against the
  manifest's SRI hash. Setting `assets_mode` (`cdn` | `local` | `auto`, default `auto`)
  picks the resolved mode at request time. No build step.
- **Templating:** Jinja2; templates use `{{ asset('<name>') }}` + `{{ asset_sri('<name>') }}` helpers (registered as Jinja globals in `app.py`).
- **Layout:** PyPA src-layout; package = `src/ntasker/`. CLI entry `ntasker = ntasker.cli:main`.
- **Bind:** `127.0.0.1:8766` (default; overridable via `ntasker serve --host --port`).
- **i18n:** stdlib `gettext` + Babel for extract/compile. Catalogs at `src/ntasker/locale/<lang>/LC_MESSAGES/ntasker.{po,mo}`. Setting `language = auto|en|de` (default `auto` -> `Accept-Language` parse, fallback `en`). Frontend reads `window.__i18n` populated server-side; Alpine `$i18n('key')` magic property. CLI honours setting > `LANG`/`LC_MESSAGES` env > `en`. `N_()` is the no-op marker for module-level constants (see `PHASE_ORDER` / `PRIORITY_ORDER` in `app.py`).

## DB path -- precedence

1. CLI flag `--db <path>`.
2. ENV `NTASKER_DB`.
3. `platformdirs.user_data_dir("nTasker") / "tasks.db"` (Linux: `~/.local/share/nTasker/tasks.db`).

Resolution lives in `src/ntasker/paths.py`. Parent dir is created on demand.

## Versioning -- single source of truth

`__version__` in `src/ntasker/__init__.py` is canonical. Sources that read it:

1. FastAPI `version=` (OpenAPI / startup logs).
2. Jinja template context as `version`.
3. CLI `--version`.
4. `?v={{ version }}` cache-buster on every `<link>` / `<script>` URL in templates.

`pyproject.toml`'s `version` field MUST be kept in sync with `__version__`.

`templates/index.html` and `templates/settings.html` are served with
`Cache-Control: no-store` so a version bump reliably forces clients to refetch CSS/JS.

## Schema migrations

Idempotent on app boot inside `ntasker.db.init_db()`. Pattern: try the new column,
catch `sqlite3.OperationalError` on "no such column" / "duplicate column",
no-op. **No Alembic. No migration files.**

## Settings module

KV-store with validators (`src/ntasker/settings.py`). Validators registered
in `VALIDATORS` dict; unknown keys are still writable but bypass validation.
`get_setting(key, env_var=...)` checks ENV first, then DB, then `None`.

UI: `/settings` (Tabler page, AlpineJS).
API: `GET/PUT/DELETE /api/settings[/<key>]`.
CLI: `ntasker config (list|get|set|unset)`.

## i18n workflow

After touching any translatable string in Python or Jinja templates:

```
make i18n          # extract -> update -> compile (.pot, .po, .mo)
make i18n-init-de  # one-time bootstrap of de.po (idempotent)
```

Extraction keywords: `_`, `_lazy`, `t` (template alias), `N_` (no-op marker).
The compiled `.mo` MUST exist before `uv build`, otherwise the wheel ships
without binary catalogs. `make i18n-compile` is the safe pre-build step.

## Workflow for changes

1. Implement the feature or fix.
2. Commit only when asked. A plain commit is **just** a commit: a short descriptive message and
   **no** version/CHANGELOG bump. Do not propose a version bump -- release is the separate, opt-in
   step below.
3. **Always invoke git as `git -C path/to/ntasker <cmd>`** -- never `cd && git`. Permission rules in
   `.claude/settings.local.json` are pinned to this exact pattern.

### Release -- only when the user explicitly names a version

Trigger: the user explicitly asks for it ("committe als v2.5.0", "bump version"). Then, and only then:

1. Bump `__version__` in `src/ntasker/__init__.py`.
2. Bump `version` in `pyproject.toml`.
3. Prepend a one-liner to `CHANGELOG.md` under a new version section.
4. Commit + tag sequence:
   ```
   git -C path/to/ntasker/ntasker add <files>
   git -C path/to/ntasker/ntasker commit -m "release: v<x.y.z> -- <one-liner>"
   git -C path/to/ntasker/ntasker tag -a v<x.y.z> -m "v<x.y.z>"
   ```

SemVer: **PATCH** = bugfix, **MINOR** = feature (no breaking change), **MAJOR** = breaking change.

## Hard NOs

- Never `--no-verify`, `--no-gpg-sign`, force-push to main/master.
- Never `git config`, `git commit --amend`, `git rebase -i`.
- Never `cd path/to/ntasker/ntasker && git ...` -- permission pattern only matches `-C` form.
- `git push` to GitHub is allowed but **only on user's explicit OK** (Memory `feedback_tracker_repo_commits`).

## Skill crosslink

API endpoints, schema, sentinels, and tracker-specific UI conventions:
**`~/.claude/skills/ntasker/SKILL.md`**.
