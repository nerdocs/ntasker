"""Plugin registry -- optional features and agent integrations as switchable units.

ntasker's core is the task store, the sidebar, the kanban and the run
infrastructure. Everything a user may not want -- a particular AI coding
agent, the workspace browser, task-context attachments -- is a *plugin*
under ``ntasker/plugins/<name>/``. A plugin is a package with two module
attributes:

* ``SPEC`` -- a :class:`PluginSpec` (name, label, description, kind).
* ``register(ctx)`` -- called once at load; it hands the plugin's pieces
  to a :class:`PluginContext`: FastAPI routers, settings keys, schema SQL,
  agent specs, CLI subparsers, template slots, JS strings.

Two rules keep this simple and free of start-up ordering problems:

**Registration is static, enablement is dynamic.** :func:`load_all` runs
once per process (at import of :mod:`ntasker.app`, in
``ntasker.cli.build_parser``, or lazily from :mod:`ntasker.agents`) and
wires every built-in plugin regardless of its switch. Whether a plugin
*acts* is decided per call by :func:`is_enabled`, which reads the
``plugins_disabled`` setting (ENV ``NTASKER_PLUGINS_DISABLED`` wins) --
or, for an opt-in plugin (``PluginSpec.default_on=False``), the
``plugins_enabled`` setting (ENV ``NTASKER_PLUGINS_ENABLED``). So
toggling a plugin in /settings needs no restart, and routes exist before
the DB is even bound.

**Disabled means invisible, never destructive.** A disabled plugin's
routes answer 404, its template slots and scripts are not rendered, its
agent is not listed and not resolvable, its CLI subcommand refuses. Its
tables and migrations still run (additive only), so data survives a
toggle in either direction.

Built-ins are listed in :data:`BUILTIN`; third-party discovery (entry
points) is deliberately not implemented -- one loop here would add it.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from fastapi import APIRouter

    from ntasker.agents import AgentSpec

#: Built-in plugins, in load order. The order is also the UI order.
BUILTIN: tuple[str, ...] = ("claude", "opencode", "pi", "task_context", "workspace", "voice")

#: Where plugin packages (and their ``templates/`` + ``static/``) live.
PLUGINS_DIR = files("ntasker") / "plugins"

#: Template slots a plugin may fill. Each is a ``{% for %}`` loop in the
#: core templates; a slot template is included with the page's context.
SLOTS: tuple[str, ...] = (
    "head",  # index.html <head>: extra <link>/<script>
    "sidebar",  # index.html: below the tags section
    "task_form",  # index.html: create form, after the description
    "task_edit",  # index.html: edit modal, after the description
    "task_card",  # index.html: list-view card, after the tag chips (``task`` in scope)
    "board_card",  # index.html: kanban card, after the tag chips (``task`` in scope)
    "modals",  # index.html: before the toast container
    "scripts",  # index.html: before app.js
    "settings",  # settings.html: below the Plugins card
)

#: Settings key holding the JSON array of disabled plugin names (default-on plugins).
SETTING_DISABLED = "plugins_disabled"
#: Settings key holding the JSON array of enabled plugin names (opt-in plugins).
SETTING_ENABLED = "plugins_enabled"
#: ENV overrides: comma-separated plugin names. Each wins over its setting.
ENV_DISABLED = "NTASKER_PLUGINS_DISABLED"
ENV_ENABLED = "NTASKER_PLUGINS_ENABLED"


@dataclass(frozen=True)
class PluginSpec:
    """Identity of one plugin, shown on the /settings Plugins card."""

    name: str
    """Package name under ``ntasker/plugins/``; also the settings token."""

    label: Any
    """Human-facing name (LazyString)."""

    description: Any
    """One sentence on what the plugin adds (LazyString)."""

    kind: str = "feature"
    """``"feature"`` or ``"agent"``. At least one agent plugin stays enabled."""

    default_on: bool = True
    """Enabled unless listed in ``plugins_disabled`` (True), or only when
    listed in ``plugins_enabled`` (False -- opt-in, e.g. needs an extra)."""

    extra: str | None = None
    """Name of the ``ntasker[<extra>]`` whose packages the plugin needs;
    ``ntasker enable`` installs what is missing (:func:`missing_requirements`)."""


@dataclass
class PluginContext:
    """What a plugin hands to the core in ``register()``.

    A pure collector: nothing here imports :mod:`ntasker.app`, so plugin
    modules stay importable in isolation (tests, CLI, ``--version``).
    """

    spec: PluginSpec
    routers: list[APIRouter] = field(default_factory=list)
    settings: list[tuple[str, Callable[[str], str], Any]] = field(default_factory=list)
    schema: list[str] = field(default_factory=list)
    migrations: list[Callable[[sqlite3.Connection], None]] = field(default_factory=list)
    agents: list[AgentSpec] = field(default_factory=list)
    cli: list[Callable[[Any], None]] = field(default_factory=list)
    slots: dict[str, list[str]] = field(default_factory=dict)
    js_strings: list[Callable[[], dict[str, str]]] = field(default_factory=list)
    task_hooks: list[Callable[[sqlite3.Connection, list[dict]], None]] = field(
        default_factory=list
    )
    create_hooks: list[Callable[[dict], Callable[[sqlite3.Connection, int], None]]] = field(
        default_factory=list
    )
    briefings: list[Callable[[int], list[str]]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.spec.name

    def add_router(self, router: APIRouter) -> None:
        """Mount an ``APIRouter``; every route 404s while the plugin is disabled."""
        self.routers.append(router)

    def add_setting(self, key: str, validator: Callable[[str], str], hint: Any = None) -> None:
        """Register a settings key (validator + optional /settings hint)."""
        self.settings.append((key, validator, hint))

    def add_schema(self, sql: str) -> None:
        """``CREATE TABLE IF NOT EXISTS ...`` script, run on every ``init_db()``."""
        self.schema.append(sql)

    def add_migration(self, fn: Callable[[sqlite3.Connection], None]) -> None:
        """Idempotent migration callable, run after the schema on ``init_db()``."""
        self.migrations.append(fn)

    def add_agent(self, spec: AgentSpec) -> None:
        """Contribute an AI coding agent (see :mod:`ntasker.agents`)."""
        self.agents.append(spec)

    def add_cli(self, fn: Callable[[argparse._SubParsersAction], None]) -> None:
        """Add subcommands; ``fn`` receives the top-level subparsers action."""
        self.cli.append(fn)

    def add_template_slot(self, slot: str, template: str) -> None:
        """Render ``template`` (path relative to ``plugins/``) inside ``slot``."""
        if slot not in SLOTS:
            raise ValueError(f"unknown template slot {slot!r}; known: {', '.join(SLOTS)}")
        self.slots.setdefault(slot, []).append(template)

    def add_js_strings(self, fn: Callable[[], dict[str, str]]) -> None:
        """Contribute translated keys to ``window.__i18n`` (called per request)."""
        self.js_strings.append(fn)

    def add_task_hook(self, fn: Callable[[sqlite3.Connection, list[dict]], None]) -> None:
        """Enrich task dicts in place (``fn(conn, tasks)``) wherever the core
        serialises tasks -- list/get/create/update endpoints and ``ntasker show``.
        Bulk by design: the list endpoint is the hot path."""
        self.task_hooks.append(fn)

    def add_create_hook(
        self, fn: Callable[[dict], Callable[[sqlite3.Connection, int], None]]
    ) -> None:
        """Take part in ``POST /api/tasks``: ``fn(payload)`` validates the
        plugin's part of the request *before* the insert (raise
        ``HTTPException`` to abort) and returns ``after(conn, task_id)``,
        run inside the same transaction once the row exists."""
        self.create_hooks.append(fn)

    def add_briefing(self, fn: Callable[[int], list[str]]) -> None:
        """Extra Markdown lines for a task's agent briefing (``fn(task_id)``)."""
        self.briefings.append(fn)

    def static_url(self, filename: str) -> str:
        """URL of a file under this plugin's ``static/`` directory."""
        return f"/static/plugins/{self.name}/{filename}"

    def static_dir(self) -> Any | None:
        """The plugin's ``static/`` directory (Traversable), or ``None``."""
        d = PLUGINS_DIR / self.name / "static"
        return d if d.is_dir() else None


#: name -> loaded plugin, in :data:`BUILTIN` order. Filled by :func:`load_all`.
REGISTRY: dict[str, PluginContext] = {}

_loaded = False


def load_all() -> None:
    """Import and register every built-in plugin. Idempotent, import-cheap.

    After collecting, the plugins' agent specs and settings keys are
    applied to :data:`ntasker.agents.AGENTS` and
    :data:`ntasker.settings.VALIDATORS` / ``HINTS`` -- the two core
    registries other modules read directly.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in BUILTIN:
        mod = importlib.import_module(f"ntasker.plugins.{name}")
        ctx = PluginContext(spec=mod.SPEC)
        mod.register(ctx)
        REGISTRY[name] = ctx
    _apply_to_core()


def _apply_to_core() -> None:
    # Lazy: ``settings`` imports ``db`` and ``agents``; importing it at the
    # top of this module would make ``ntasker.agents`` -> plugins -> settings
    # a cycle at import time.
    from ntasker import agents, settings  # noqa: PLC0415

    for ctx in REGISTRY.values():
        for spec in ctx.agents:
            agents.AGENTS[spec.key] = spec
        for key, validator, hint in ctx.settings:
            settings.VALIDATORS[key] = validator
            if hint is not None:
                settings.HINTS[key] = hint


def _names_from(env_var: str, setting: str) -> set[str]:
    """Plugin names listed in ``env_var`` (comma list) or else in ``setting`` (JSON array).

    Reads the DB only when the ENV override is absent; an unbound DB
    (``ntasker --version``, parser construction) counts as an empty list.
    """
    raw_env = os.environ.get(env_var)
    if raw_env is not None:
        return {n.strip() for n in raw_env.split(",") if n.strip()}
    from ntasker.settings import get_setting  # noqa: PLC0415

    try:
        parsed = json.loads(get_setting(setting) or "[]")
    except Exception:  # noqa: BLE001 -- unbound DB or bad JSON: empty list
        parsed = []
    return {str(n) for n in parsed} if isinstance(parsed, list) else set()


def disabled_plugins() -> set[str]:
    """Names currently switched off. ENV wins over the setting.

    A default-on plugin is off when listed in ``plugins_disabled``; an
    opt-in plugin is off unless listed in ``plugins_enabled``. If the
    result would leave no agent plugin enabled, the agent names are
    dropped from it -- a task must always have an agent to resolve to, and
    the setting validator already refuses that state; this guard covers
    the unvalidated ENV path.
    """
    load_all()
    names = _names_from(ENV_DISABLED, SETTING_DISABLED)
    opted_in = _names_from(ENV_ENABLED, SETTING_ENABLED)
    names |= {n for n, c in REGISTRY.items() if not c.spec.default_on and n not in opted_in}
    agent_names = {n for n, c in REGISTRY.items() if c.spec.kind == "agent"}
    if agent_names and agent_names <= names:
        names -= agent_names
    return names & set(REGISTRY)


def is_enabled(name: str) -> bool:
    """Whether the plugin exists and is switched on right now."""
    load_all()
    return name in REGISTRY and name not in disabled_plugins()


def enabled() -> list[PluginContext]:
    """Enabled plugins in load order. One settings read per call."""
    load_all()
    off = disabled_plugins()
    return [ctx for name, ctx in REGISTRY.items() if name not in off]


def enabled_names() -> list[str]:
    """Names of the enabled plugins (for ``window.__plugins``)."""
    return [ctx.name for ctx in enabled()]


def require_enabled(name: str) -> Callable[[], None]:
    """FastAPI dependency factory: 404 while ``name`` is disabled."""

    def _dep() -> None:
        if not is_enabled(name):
            from fastapi import HTTPException  # noqa: PLC0415

            raise HTTPException(status_code=404, detail="plugin disabled")

    return _dep


def enabled_slots() -> dict[str, list[str]]:
    """``slot -> [template, ...]`` over the enabled plugins; every slot is present."""
    out: dict[str, list[str]] = {slot: [] for slot in SLOTS}
    for ctx in enabled():
        for slot, templates in ctx.slots.items():
            for tpl in templates:
                if tpl not in out[slot]:
                    out[slot].append(tpl)
    return out


def enabled_js_strings() -> dict[str, str]:
    """Merged ``window.__i18n`` contributions of the enabled plugins."""
    out: dict[str, str] = {}
    for ctx in enabled():
        for fn in ctx.js_strings:
            out.update(fn())
    return out


def apply_task_hooks(conn: sqlite3.Connection, tasks: list[dict]) -> None:
    """Run every enabled plugin's task hooks over ``tasks`` (in place)."""
    if not tasks:
        return
    for ctx in enabled():
        for fn in ctx.task_hooks:
            fn(conn, tasks)


def run_create_hooks(payload: dict) -> list[Callable[[sqlite3.Connection, int], None]]:
    """Validate the plugin parts of a create payload; return the after-insert steps."""
    return [fn(payload) for ctx in enabled() for fn in ctx.create_hooks]


def run_briefings(task_id: int) -> list[str]:
    """Concatenated briefing lines of every enabled plugin for ``task_id``."""
    lines: list[str] = []
    for ctx in enabled():
        for fn in ctx.briefings:
            lines += fn(task_id)
    return lines


def missing_requirements(extra: str) -> list[str]:
    """Requirement specs of the ``ntasker[extra]`` extra whose distribution
    is not installed in this interpreter (empty = the extra is complete)."""
    import importlib.metadata as md  # noqa: PLC0415
    import re  # noqa: PLC0415

    missing: list[str] = []
    for req in md.requires("ntasker") or []:
        if f'extra == "{extra}"' not in req:
            continue
        spec = req.split(";", 1)[0].strip()
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec)
        try:
            md.distribution(name.group(0) if name else spec)
        except md.PackageNotFoundError:
            missing.append(spec)
    return missing


def init_schema(conn: sqlite3.Connection) -> None:
    """Run every plugin's schema + migrations (enabled or not; additive only)."""
    load_all()
    for ctx in REGISTRY.values():
        for sql in ctx.schema:
            conn.executescript(sql)
        for fn in ctx.migrations:
            fn(conn)


def describe() -> list[dict[str, Any]]:
    """Plugin list for ``GET /api/plugins`` and the /settings card."""
    off = disabled_plugins()
    return [
        {
            "name": ctx.name,
            "label": str(ctx.spec.label),
            "description": str(ctx.spec.description),
            "kind": ctx.spec.kind,
            "default_on": ctx.spec.default_on,
            "enabled": ctx.name not in off,
            "settings": "settings" in ctx.slots,
        }
        for ctx in REGISTRY.values()
    ]
