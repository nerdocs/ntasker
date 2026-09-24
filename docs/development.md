# Development

```bash
git clone https://github.com/nerdocs/ntasker
cd ntasker
make install   # uv sync
make run       # uv run ntasker serve --reload
```

For a global `ntasker` command that runs live from your working tree -- edits take effect immediately, no rebuild --
install it editable as a uv tool:

```bash
uv tool install -e .   # global `ntasker`, live from src/
```

This is independent of a PyPI install; the two compete for the same `~/.local/bin/ntasker` symlink and the same
`ntasker.service` unit, so use one or the other as your active setup. To validate the real PyPI install without
disturbing your repo setup, install it into a throwaway venv instead.

## Layout

PyPA src-layout, package `src/ntasker/`, entry point `ntasker = ntasker.cli:main`. Backend is FastAPI + uvicorn on the
Python stdlib `sqlite3` -- no ORM, no Alembic. Frontend is HTML + AlpineJS + Tabler.io with no build step; see
[api.md](api.md#design-notes) for the schema-migration pattern.

## Smoke test

```bash
make smoke
```

Runs an in-process FastAPI test client against a temp DB *and* exercises a couple of CLI subcommands via subprocess.

## Translations

Regenerate the catalogs after touching any translatable string in Python or Jinja templates:

```bash
make i18n          # extract + update + compile (.pot, .po, .mo)
make i18n-init-de  # bootstrap a fresh language (idempotent)
```

Extraction uses [Babel](https://babel.pocoo.org/) (a dev-only dependency; the runtime needs only the stdlib). Catalog
keywords: `_`, `_lazy`, `t` (Jinja shorthand), `N_` (no-op marker for module-level constants). The compiled `.mo` is
gitignored and must exist before `uv build` / `uv tool install`, otherwise the wheel ships without binary catalogs --
run `make i18n-compile` first.

## Plugin contract

Agent integrations and optional features are plugins under `src/ntasker/plugins/<key>/`. Contract and extension slots:
[plugins.md](plugins.md).
