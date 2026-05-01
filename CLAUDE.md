# nerdocs Tracker — Workflow Rules

Lightweight local task tracker. Single-user, no auth, bound to `127.0.0.1:8766`.
**This file = workflow only.** API and schema knowledge live in
`~/.claude/skills/nerdocs-tracker/SKILL.md`.

## Stack

- **Backend:** FastAPI + Python stdlib `sqlite3` (no ORM, no Alembic).
- **Frontend:** AlpineJS + Tabler.io, vendored to `static/vendor/`. No build step, no CDN at runtime.
- **Templating:** Jinja2.
- **Bind:** `127.0.0.1:8766`, hardcoded in `Makefile`. Single-user.

## Versioning — single source of truth

`VERSION` constant in `app.py` is canonical. It is:

1. Returned by FastAPI startup logs.
2. Injected into the Jinja template context as `version`.
3. Appended to every `<link>` / `<script>` URL as `?v={{ version }}` (cache-buster).

`pyproject.toml`'s `version` field MUST be kept in sync with `app.py`.

`templates/index.html` is served with `Cache-Control: no-store` so a version
bump reliably forces clients to refetch CSS/JS.

## Schema migrations

Idempotent on app boot inside `_init_db()`. Pattern: try the new column,
catch `sqlite3.OperationalError` on "no such column" / "duplicate column",
no-op. **No Alembic. No migration files.**

## Workflow for changes

1. Implement the feature or fix.
2. Ask Christian: **"shall I commit as v<x.y.z>?"** — wait for explicit OK.
3. On OK:
   - Bump `VERSION` in `app.py`.
   - Bump `version` in `pyproject.toml`.
   - Prepend a one-liner to `CHANGELOG.md` under a new version section.
4. **Always invoke git as `git -C /home/christian/nerdocs/tracker <cmd>`** —
   never `cd && git`. Permission rules in `.claude/settings.local.json` are
   pinned to this exact pattern.
5. Commit + tag sequence:
   ```
   git -C /home/christian/nerdocs/tracker add <files>
   git -C /home/christian/nerdocs/tracker commit -m "release: v<x.y.z> — <one-liner>"
   git -C /home/christian/nerdocs/tracker tag -a v<x.y.z> -m "v<x.y.z>"
   ```
6. SemVer:
   - **PATCH** = bugfix.
   - **MINOR** = feature, no breaking change.
   - **MAJOR** = breaking change.

## Hard NOs

- Never `--no-verify`, `--no-gpg-sign`, force-push.
- Never `git config`, `git commit --amend`, `git rebase -i`.
- Never `git push` — there is no remote.
- Never `cd /home/christian/nerdocs/tracker && git ...` — permission pattern only matches `-C` form.

## Skill crosslink

API endpoints, schema, sentinels, and tracker-specific UI conventions:
**`~/.claude/skills/nerdocs-tracker/SKILL.md`**.
