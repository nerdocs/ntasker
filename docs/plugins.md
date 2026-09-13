# Plugins

ntasker's core is the task store, the sidebar, the kanban and the run infrastructure. Everything a user may not want
-- a particular AI coding agent, and (in later releases) the workspace browser or task-context attachments -- is a
**plugin** that can be switched off individually. Built-ins live under `src/ntasker/plugins/<name>/`; today these are
the three agent plugins `claude`, `opencode` and `pi`.

## Switching plugins on and off

- **UI:** `/settings` -> *Plugins* card, one switch per plugin. Takes effect immediately, no restart.
- **CLI:** `ntasker config set plugins_disabled '["pi"]'` -- a JSON array of plugin names to switch *off*.
- **ENV:** `NTASKER_PLUGINS_DISABLED=pi,opencode` (comma-separated) wins over the stored setting.

The list is a *disabled* list so that a plugin added in a later release is on by default and an existing DB needs no
migration. The validator rejects unknown names and any list that would leave no agent plugin enabled: every task needs
an agent to resolve to. (The ENV path is not validated; a value that disables every agent is ignored for the agents.)

What "disabled" means:

| Surface | Disabled plugin |
|---|---|
| Routes (`ctx.add_router`) | answer `404 plugin disabled` |
| Template slots, scripts, `window.__i18n` keys | not rendered / not merged |
| Agent (`ctx.add_agent`) | not listed by `/api/agents`, not a valid `agent` value, `default_agent` falls back |
| CLI subcommand (`ctx.add_cli`) / `ntasker agent install <key>` | refuses with exit 2 |
| Tables and migrations (`ctx.add_schema` / `add_migration`) | **still run** -- additive only, data survives a toggle |

## Two rules

**Registration is static, enablement is dynamic.** `ntasker.plugins.load_all()` runs once per process -- at import of
`ntasker.app`, in `ntasker.cli.build_parser()`, or lazily from `ntasker.agents` -- and wires every built-in plugin
regardless of its switch: routes, settings validators, agent specs, CLI subparsers, template slots. Whether a plugin
*acts* is decided per request by `is_enabled(name)`, which reads the setting. So there is no start-up ordering
problem (routes exist before the DB is bound) and no restart to toggle.

**Disabled means invisible, never destructive.** See the table above.

## The contract

A plugin is a package `ntasker/plugins/<name>/` with two module attributes:

```python
from ntasker.i18n import _lazy
from ntasker.plugins import PluginContext, PluginSpec

SPEC = PluginSpec(
    name="example",                       # package name; also the settings token
    label=_lazy("Example"),               # shown on the Plugins card
    description=_lazy("What it adds."),
    kind="feature",                       # "feature" | "agent"
)

def register(ctx: PluginContext) -> None:
    ...
```

`register()` is called once at load and hands the plugin's pieces to the context:

| Method | What it contributes |
|---|---|
| `add_router(router)` | FastAPI `APIRouter`; every route carries a `require_enabled` dependency |
| `add_setting(key, validator, hint=None)` | settings key: validator into `VALIDATORS`, hint into `HINTS` |
| `add_schema(sql)` | `CREATE TABLE IF NOT EXISTS ...` script, run on every `init_db()` |
| `add_migration(fn)` | idempotent `fn(conn)`, run after the schema on `init_db()` |
| `add_agent(spec)` | an `AgentSpec` (see [agents.md](agents.md)) |
| `add_cli(fn)` | `fn(subparsers)` adds subcommands to the top-level parser |
| `add_template_slot(slot, template)` | Jinja template rendered inside a core page slot (below) |
| `add_js_strings(fn)` | `fn() -> {key: translated}` merged into `window.__i18n` per request |
| `static_url(filename)` | URL of a file under the plugin's `static/` (`/static/plugins/<name>/<file>`) |

Slot templates are addressed relative to `plugins/`, e.g. `"example/templates/sidebar.html"`, and render with the
page's full context. Slots:

| Slot | Where |
|---|---|
| `head` | `index.html` `<head>`, after `style.css` |
| `sidebar` | `index.html`, below the tags section |
| `task_form` | `index.html`, create form, after the description |
| `task_edit` | `index.html`, edit modal, after the description |
| `modals` | `index.html`, before the toast container |
| `scripts` | `index.html`, before `app.js` |
| `settings` | `settings.html`, below the Plugins card |

Frontend: a plugin's `scripts` slot loads a script that pushes a factory onto `window.ntaskerPlugins`. The object it
returns (state + methods) is merged into the `tracker()` Alpine component; a `pluginInit()` method, if present, is
awaited at the end of `init()`. `window.__plugins` holds the enabled plugin names for JS-side gating.

i18n: plugin Python and templates go through the one catalog (`babel.cfg` covers `plugins/**`); run `make i18n` as
usual.

## API

`GET /api/plugins` lists every built-in with `name`, `label`, `description`, `kind` and `enabled`. Toggling goes
through `PUT /api/settings/plugins_disabled`.

## Not included

Third-party discovery via entry points is deliberately absent -- one loop in `load_all()` would add it, but there are
no such plugins yet and entry points bring packaging and version-skew concerns for nothing.
