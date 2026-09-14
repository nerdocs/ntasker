"""Settings module - KV-store + validators for ntasker.

Schema lives in :mod:`ntasker.db` (table ``settings``). The store is
intentionally generic (``key TEXT PRIMARY KEY``, ``value TEXT NOT NULL``)
so adding a new setting is one row + one validator entry, no migration.

Read precedence inside :func:`get_setting`:

1. Environment variable (if ``env_var`` is given).
2. DB row.
3. ``None`` -- caller decides on a default.

Every write goes through a registered validator (see :data:`VALIDATORS`).
Validation failures are reported as :class:`ValueError`; the FastAPI layer
maps these to ``400 Bad Request``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import datetime

from ntasker.agents import agent_keys, default_agent_key
from ntasker.assets import ResolvedMode, validate_assets_mode
from ntasker.db import get_conn
from ntasker.i18n import AVAILABLE_LANGUAGES, _, _lazy

Validator = Callable[[str], str]
"""A validator takes the raw value, returns a normalized value, or raises ValueError."""


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
#
# v2.0 removed the ``projects_dir`` setting + its filesystem-scan
# validator: projects now emerge implicitly from ``tasks.project``.
# A stale ``projects_dir`` row left over from v1.x is harmless -- it
# just sits unread under "All settings (DB content)" in the UI and can
# be deleted via the trash icon.


def validate_language(value: str) -> str:
    """Validator for the ``language`` setting.

    Whitelist-only (Zero Trust): accepts ``auto``, ``en``, ``de``. Any
    other value is rejected with a translated error message - never
    silently coerced.
    """
    allowed = ("auto", *AVAILABLE_LANGUAGES)
    norm = (value or "").strip().lower()
    if norm not in allowed:
        raise ValueError(
            _("Invalid language: {value!r}. Allowed: {allowed}").format(
                value=value, allowed=", ".join(allowed)
            )
        )
    return norm


# Allowed values for the ``default_view`` setting. Kept in sync with the
# Alpine state in ``static/app.js`` (VIEW_MODES). Adding a third view
# requires touching both ends.
DEFAULT_VIEW_ALLOWED = ("list", "kanban")
DEFAULT_VIEW_FALLBACK = "list"


def validate_default_view(value: str) -> str:
    """Validator for the ``default_view`` setting.

    Whitelist: ``list`` or ``kanban``. Any other value is rejected so a
    typo in the UI / CLI doesn't silently land in the DB.
    """
    norm = (value or "").strip().lower()
    if norm not in DEFAULT_VIEW_ALLOWED:
        raise ValueError(
            _("Invalid default_view: {value!r}. Allowed: {allowed}").format(
                value=value, allowed=", ".join(DEFAULT_VIEW_ALLOWED)
            )
        )
    return norm


def validate_update_command(value: str) -> str:
    """Validator for the ``update_command`` setting.

    The shell command ``self-update`` runs to upgrade the package (e.g.
    ``uv tool upgrade ntasker``). Must be non-empty and parseable as an
    argument vector; unset/DELETE the key to fall back to auto-detection.
    """
    import shlex  # noqa: PLC0415

    norm = (value or "").strip()
    if not norm:
        raise ValueError(_("update_command must not be empty -- unset it to auto-detect."))
    try:
        parts = shlex.split(norm)
    except ValueError as exc:
        raise ValueError(
            _("update_command is not a valid command line: {value!r}").format(value=value)
        ) from exc
    if not parts:
        raise ValueError(_("update_command must not be empty -- unset it to auto-detect."))
    return norm


def validate_project_groups(value: str) -> str:
    """Validator for the ``project_groups`` setting.

    JSON object ``{project: family}`` overriding the sidebar's automatic
    name-prefix grouping (row menu -> "Group..."). An empty family string
    opts the project out of any family. Keys are trimmed, empty keys dropped.
    """
    import json  # noqa: PLC0415

    try:
        parsed = json.loads(value or "{}")
    except ValueError as exc:
        raise ValueError(_("project_groups must be a JSON object of project -> group.")) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError(_("project_groups must be a JSON object of project -> group."))
    groups = {k.strip(): v.strip() for k, v in parsed.items() if k.strip()}
    return json.dumps(groups)


SIDEBAR_SECTIONS = ("projects", "priority", "phases", "tags", "workspace")


def validate_sidebar_sections(value: str) -> str:
    """Validator for the ``sidebar_sections`` setting.

    JSON object ``{section: open}`` remembering which sidebar sections are
    folded. Unknown sections are dropped; a missing section counts as open
    (see :func:`get_sidebar_sections`).
    """
    import json  # noqa: PLC0415

    try:
        parsed = json.loads(value or "{}")
    except ValueError as exc:
        raise ValueError(_("sidebar_sections must be a JSON object of section -> true/false.")) from exc
    if not isinstance(parsed, dict) or not all(isinstance(v, bool) for v in parsed.values()):
        raise ValueError(_("sidebar_sections must be a JSON object of section -> true/false."))
    return json.dumps({k: v for k, v in parsed.items() if k in SIDEBAR_SECTIONS})


def validate_projects_base(value: str) -> str:
    """Validator for the ``projects_base`` setting.

    A filesystem path used as the base for relativizing discovered Claude
    project names: with ``projects_base = ~/Projekte`` the project at
    ``~/Projekte/medux`` shows up as ``medux`` instead of ``Projekte/medux``.

    ``~`` is kept verbatim (expanded per-machine at read time); the value is
    only required to expand to an *absolute* path. Existence is NOT checked.
    To clear it, unset/DELETE the key rather than storing an empty string.
    """
    norm = (value or "").strip()
    if not norm:
        raise ValueError(_("projects_base must not be empty -- unset it to clear."))
    if not os.path.isabs(os.path.expanduser(norm)):
        raise ValueError(
            _("projects_base must be an absolute path (got {value!r}).").format(value=value)
        )
    return norm


def validate_no_project_dir(value: str) -> str:
    """Validator for the ``no_project_dir`` setting.

    The directory a run starts in when the task has no usable project directory
    (see :func:`ntasker.claude_runner.resolve_run_cwd`). Same rules as
    :func:`validate_projects_base`: ``~`` is kept verbatim and only required to
    expand to an *absolute* path; existence is checked at run time, not here.
    To clear it, unset/DELETE the key rather than storing an empty string.
    """
    norm = (value or "").strip()
    if not norm:
        raise ValueError(_("no_project_dir must not be empty -- unset it to clear."))
    if not os.path.isabs(os.path.expanduser(norm)):
        raise ValueError(
            _("no_project_dir must be an absolute path (got {value!r}).").format(value=value)
        )
    return norm


# Default idle window (seconds): a live Claude session that produced no output
# for at least this long is treated as "waiting for input" (see
# :func:`ntasker.claude_runner.session_states`). The CLI emits no explicit
# "I have a question" signal, so this silence heuristic stands in for it.
CLAUDE_IDLE_SECONDS_DEFAULT = 8.0


def validate_claude_idle_seconds(value: str) -> str:
    """Validator for the ``claude_idle_seconds`` setting.

    A positive number of seconds. Rejects non-numeric or non-positive values so
    a typo can't disable the "waiting" detection by storing garbage.
    """
    norm = (value or "").strip()
    try:
        secs = float(norm)
    except ValueError:
        raise ValueError(
            _("claude_idle_seconds must be a number of seconds (got {value!r}).").format(
                value=value
            )
        ) from None
    if secs <= 0:
        raise ValueError(_("claude_idle_seconds must be greater than 0."))
    return norm


# Boolean-setting spellings. Truthy values arm a flag; everything else clears it.
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off", ""})


def validate_claude_open_terminal(value: str) -> str:
    """Validator for the ``claude_open_terminal`` boolean setting.

    Controls what a "Create + Run" / per-task Claude-run click does: when truthy
    (default) the run view opens immediately; when falsy the session starts in
    the background and the user stays on the board. Normalizes truthy/falsy
    spellings to ``"true"`` / ``"false"``; rejects anything else.
    """
    norm = (value or "").strip().lower()
    if norm in _TRUE_STRINGS:
        return "true"
    if norm in _FALSE_STRINGS:
        return "false"
    raise ValueError(
        _("claude_open_terminal must be a yes/no value (got {value!r}).").format(value=value)
    )


def validate_default_agent(value: str) -> str:
    """Validator for the ``default_agent`` setting.

    The AI coding agent new tasks default to (and the fallback for any task
    without an explicit ``agent``). Whitelist against the *enabled* agent
    keys; empty normalizes to the built-in default. See :mod:`ntasker.agents`.
    """
    norm = (value or "").strip().lower()
    keys = agent_keys()
    if not norm:
        return default_agent_key()
    if norm in keys:
        return norm
    raise ValueError(
        _("default_agent must be one of {keys} (got {value!r}).").format(
            keys=", ".join(keys), value=value
        )
    )


def validate_on_off(value: str) -> str:
    """Validator for the ``on``/``off`` switches (``dir_locks``, ``require_clean``).

    Normalizes every truthy/falsy spelling to ``"on"`` / ``"off"`` -- the
    spelling the /settings radios use -- and rejects anything else.
    """
    norm = (value or "").strip().lower()
    if norm in _TRUE_STRINGS:
        return "on"
    if norm in _FALSE_STRINGS:
        return "off"
    raise ValueError(_("Expected on or off (got {value!r}).").format(value=value))


def validate_plugins_disabled(value: str) -> str:
    """Validator for the ``plugins_disabled`` setting.

    JSON array of plugin names switched off (see :mod:`ntasker.plugins`).
    Unknown names are rejected; so is a list that would leave no agent
    plugin enabled, because every task needs an agent to resolve to. Names
    are de-duplicated and kept in registry order. ENV
    ``NTASKER_PLUGINS_DISABLED`` (comma list) overrides the stored value.
    """
    import json  # noqa: PLC0415

    from ntasker import plugins  # noqa: PLC0415 -- lazy: plugins import settings

    plugins.load_all()
    try:
        parsed = json.loads(value or "[]")
    except ValueError as exc:
        raise ValueError(_("plugins_disabled must be a JSON array of plugin names.")) from exc
    if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
        raise ValueError(_("plugins_disabled must be a JSON array of plugin names."))
    names = {v.strip() for v in parsed if v.strip()}
    unknown = sorted(names - set(plugins.REGISTRY))
    if unknown:
        raise ValueError(
            _("Unknown plugin(s): {names}. Known: {known}").format(
                names=", ".join(unknown), known=", ".join(plugins.REGISTRY)
            )
        )
    agents = {n for n, c in plugins.REGISTRY.items() if c.spec.kind == "agent"}
    if agents and agents <= names:
        raise ValueError(_("At least one agent plugin must stay enabled."))
    return json.dumps([n for n in plugins.REGISTRY if n in names])


def validate_plugins_enabled(value: str) -> str:
    """Validator for the ``plugins_enabled`` setting.

    JSON array of opt-in plugin names switched on (``PluginSpec.default_on``
    is False for those). Unknown names are rejected; names are
    de-duplicated and kept in registry order. ENV
    ``NTASKER_PLUGINS_ENABLED`` (comma list) overrides the stored value.
    """
    import json  # noqa: PLC0415

    from ntasker import plugins  # noqa: PLC0415 -- lazy: plugins import settings

    plugins.load_all()
    try:
        parsed = json.loads(value or "[]")
    except ValueError as exc:
        raise ValueError(_("plugins_enabled must be a JSON array of plugin names.")) from exc
    if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
        raise ValueError(_("plugins_enabled must be a JSON array of plugin names."))
    names = {v.strip() for v in parsed if v.strip()}
    unknown = sorted(names - set(plugins.REGISTRY))
    if unknown:
        raise ValueError(
            _("Unknown plugin(s): {names}. Known: {known}").format(
                names=", ".join(unknown), known=", ".join(plugins.REGISTRY)
            )
        )
    return json.dumps([n for n in plugins.REGISTRY if n in names])


def validate_queue_enabled(value: str) -> str:
    """Validator for the ``queue_enabled`` boolean setting.

    The task queue's pause switch. When truthy (the default) the queue worker
    starts the next queued task as soon as its project is free; when falsy
    queued tasks just sit there -- nothing new starts until the queue is
    resumed. Stored in the DB rather than the browser so the state survives a
    restart and every open tab agrees on it. Normalizes truthy/falsy spellings
    to ``"true"`` / ``"false"``; rejects anything else.
    """
    norm = (value or "").strip().lower()
    if norm in _TRUE_STRINGS:
        return "true"
    if norm in _FALSE_STRINGS:
        return "false"
    raise ValueError(
        _("queue_enabled must be a yes/no value (got {value!r}).").format(value=value)
    )


VALIDATORS: dict[str, Validator] = {
    "assets_mode": validate_assets_mode,
    "language": validate_language,
    "default_view": validate_default_view,
    "default_agent": validate_default_agent,
    "projects_base": validate_projects_base,
    "project_groups": validate_project_groups,
    "sidebar_sections": validate_sidebar_sections,
    "no_project_dir": validate_no_project_dir,
    "claude_idle_seconds": validate_claude_idle_seconds,
    "claude_open_terminal": validate_claude_open_terminal,
    "queue_enabled": validate_queue_enabled,
    "dir_locks": validate_on_off,
    "require_clean": validate_on_off,
    "plugins_disabled": validate_plugins_disabled,
    "plugins_enabled": validate_plugins_enabled,
    "update_command": validate_update_command,
}
"""Registry of known settings keys with their validators.

Unknown keys are still writable (forward-compat for ad-hoc keys via the
CLI / API), but they bypass validation. Keys *with* a validator MUST pass
it before any DB write.
"""


# Hint texts shown next to known keys in the /settings UI. Wrapped in
# :class:`LazyString` so they translate per-request - the dict itself is
# evaluated at import time, but each entry stays bound to its msgid.
HINTS: dict[str, object] = {
    "assets_mode": _lazy(
        "Vendor assets (Tabler/Alpine): cdn (default, jsDelivr + SRI), "
        "local (from user-data dir, populate via `ntasker assets fetch`), "
        "auto (local if cache complete, else cdn)."
    ),
    "language": _lazy(
        "UI language. Leave unset for automatic (follows the browser's "
        "Accept-Language header, fallback English), or pick English or German."
    ),
    "default_view": _lazy(
        "Default view on startup: 'list' (classic task list) or 'kanban' "
        "(4-column board). The frontend remembers the last user choice in "
        "localStorage; this setting drives the initial pick on a fresh browser."
    ),
    "projects_base": _lazy(
        "Base path for project names, e.g. '~/Projekte'. Discovered Claude "
        "projects below it are named relative to it (the folder right under "
        "the base becomes the project name) instead of relative to your home "
        "directory. Unset to fall back to home-relative names. "
        "ENV: NTASKER_PROJECTS_BASE."
    ),
    "no_project_dir": _lazy(
        "Start directory for a run whose task has no project (or whose project "
        "directory does not exist and cannot be created), e.g. '~/Projekte'. "
        "Unset falls back to the projects base, then to your home directory -- "
        "note that Claude Code refuses to work in the home directory until you "
        "answer its trust prompt. ENV: NTASKER_NO_PROJECT_DIR."
    ),
    "default_agent": _lazy(
        "Default AI coding agent for new tasks (and the fallback for any task "
        "without an explicit agent): claude, opencode or pi. ENV: "
        "NTASKER_DEFAULT_AGENT."
    ),
    "claude_open_terminal": _lazy(
        "When starting a Claude session (Create + Run or the per-task run "
        "button), open the terminal immediately (true, default) or start it in "
        "the background and stay on the board (false). ENV: "
        "NTASKER_CLAUDE_OPEN_TERMINAL."
    ),
    "claude_idle_seconds": _lazy(
        "How many seconds a live Claude session may stay silent before ntasker "
        "treats it as waiting for your input (the CLI sends no explicit "
        "'I have a question' signal). Lower = quicker 'waiting' badge but more "
        "false positives; higher = fewer false positives but slower. Default 8."
    ),
    "queue_enabled": _lazy(
        "Pause switch for the task queue -- the only way a session starts. On "
        "by default; off pauses it: run buttons still queue tasks, but nothing "
        "new starts until you press Resume on the queue panel. One task runs "
        "per project at a time. ENV: NTASKER_QUEUE_ENABLED."
    ),
    "dir_locks": _lazy(
        "Directory locks: a queued task waits while another live session holds "
        "one of its directories (its own project's or a locked project's), and "
        "Claude Code sessions refuse edits inside other projects' directories "
        "they do not hold. Off: one task per project lane only."
    ),
    "require_clean": _lazy(
        "Only start a queued task when every directory it holds is git-clean "
        "(no uncommitted changes). Needs directory locks on."
    ),
    "sidebar_sections": _lazy(
        "Which sidebar sections are folded -- written by the fold buttons in the "
        "sidebar. JSON object {section: true|false}; a missing section is open."
    ),
    "update_command": _lazy(
        "Shell command run by 'self-update' to upgrade ntasker "
        "(e.g. `uv tool upgrade ntasker`). Unset to auto-detect how ntasker "
        "was installed."
    ),
    "plugins_disabled": _lazy(
        "JSON array of plugins switched off, e.g. [\"pi\", \"workspace\"]. "
        "Toggle them on the Plugins card above; at least one agent plugin "
        "stays enabled. ENV: NTASKER_PLUGINS_DISABLED (comma-separated)."
    ),
    "plugins_enabled": _lazy(
        "JSON array of opt-in plugins switched on, e.g. [\"voice\"]. Toggle "
        "them on the Plugins card above or with `ntasker enable <plugin>`. "
        "ENV: NTASKER_PLUGINS_ENABLED (comma-separated)."
    ),
}


# Radio-style choice sets for the enum settings rendered in the /settings
# "Known keys" section. Each option is (value, label, description); the label
# is a short caption and the description a one-line per-option hint (or None).
# LazyStrings so they translate per-request. FIELD_DEFAULTS gives the effective
# default so an unset key still shows its active choice pre-selected.
FIELD_CHOICES: dict[str, list[tuple[str, object, object]]] = {
    "assets_mode": [
        ("cdn", _lazy("CDN"), _lazy("Load Tabler/Alpine from jsDelivr (with SRI). Needs internet.")),
        ("local", _lazy("Local"), _lazy("Serve from the user-data cache (fill it via `ntasker assets fetch`).")),
        ("auto", _lazy("Auto"), _lazy("Use the local cache when complete, otherwise fall back to the CDN.")),
    ],
    "language": [
        ("en", _lazy("English"), None),
        ("de", _lazy("Deutsch"), None),
    ],
    "default_view": [
        ("list", _lazy("List"), _lazy("Classic task list.")),
        ("kanban", _lazy("Kanban board"), _lazy("Four-column board.")),
    ],
    "dir_locks": [
        ("on", _lazy("On"), None),
        ("off", _lazy("Off"), None),
    ],
    "require_clean": [
        ("on", _lazy("On"), None),
        ("off", _lazy("Off"), None),
    ],
}

FIELD_DEFAULTS: dict[str, str] = {
    "assets_mode": "auto",
    "default_view": DEFAULT_VIEW_FALLBACK,
    "dir_locks": "on",
    "require_clean": "off",
}


def make_bin_validator(agent_key: str) -> Validator:
    """Build a validator for an agent's ``<key>_bin`` binary-path override.

    Accepts a path (contains ``/`` -> expanded, must be an executable file) or
    a bare command name (must resolve on ``PATH``). Rejects anything that does
    not resolve so the user gets immediate feedback instead of a silent
    "still unavailable". To clear it, DELETE the key (auto-detect on PATH).
    """

    def _validate(value: str) -> str:
        import shutil  # noqa: PLC0415

        norm = (value or "").strip()
        if not norm:
            raise ValueError(
                _("{key} must not be empty -- unset it to auto-detect on PATH.").format(
                    key=f"{agent_key}_bin"
                )
            )
        if "/" in norm:
            expanded = os.path.expanduser(norm)
            if not (os.path.isfile(expanded) and os.access(expanded, os.X_OK)):
                raise ValueError(
                    _("{value!r} is not an executable file.").format(value=norm)
                )
            return norm
        if shutil.which(norm) is None:
            raise ValueError(_("{value!r} was not found on PATH.").format(value=norm))
        return norm

    return _validate


#: Hint shared by every agent plugin's ``<key>_bin`` binary-path override.
#: The override lets the user point ntasker at a CLI that is not on the
#: server's PATH -- e.g. when run as a systemd unit without ``nvm`` /
#: ``~/.opencode/bin``. See :func:`ntasker.agents.resolve_binary`.
BIN_OVERRIDE_HINT = _lazy(
    "Full path to the agent's CLI when it is not on the server's PATH "
    "(e.g. an absolute path under your home). Unset to auto-detect."
)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_settings() -> list[dict]:
    """Return all settings rows ordered by key."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM settings ORDER BY key ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_setting_raw(key: str) -> dict | None:
    """Return the raw DB row for ``key`` (or ``None``). No ENV fallback."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT key, value, updated_at FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return dict(row) if row else None


def get_setting(key: str, env_var: str | None = None) -> str | None:
    """Resolve a setting. ENV first (if ``env_var``), then DB, then ``None``.

    The ENV override is intentional: it lets the user pin a value for one
    shell or one deploy without touching the DB. The Settings-UI shows a
    badge "via ENV" when this happens (see /settings template).
    """
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val
    row = get_setting_raw(key)
    return row["value"] if row else None


def set_setting(key: str, value: str) -> dict:
    """Validate + UPSERT. Returns the persisted row.

    Raises :class:`ValueError` if a registered validator rejects the value.
    """
    validator = VALIDATORS.get(key)
    if validator is not None:
        value = validator(value)
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
    return {"key": key, "value": value, "updated_at": now}


def delete_setting(key: str) -> bool:
    """DELETE the row. Returns ``True`` if a row was removed."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Convenience: typed accessors
# ---------------------------------------------------------------------------


def get_assets_mode_resolved() -> ResolvedMode:
    """Return the *resolved* asset-loading mode (``cdn`` or ``local``).

    Reads the ``assets_mode`` setting (ENV ``NTASKER_ASSETS_MODE`` first),
    defaults to ``auto``, then resolves ``auto`` to a concrete mode based
    on whether the user-data vendor cache is complete.
    """
    # Local import: avoid an import cycle at module load (assets.py
    # imports from ntasker.paths, which is fine; but importing assets
    # at top-level in settings is fine too -- see top of file).
    from ntasker.assets import resolve_mode

    raw = get_setting("assets_mode", env_var="NTASKER_ASSETS_MODE")
    return resolve_mode(raw)


def get_default_view() -> str:
    """Return the configured default view (``list`` or ``kanban``).

    Honours the ``NTASKER_DEFAULT_VIEW`` ENV override. Falls back to
    ``list`` when unset or invalid -- the value is also re-validated here
    so a stale row with an unsupported value (e.g. after a downgrade)
    degrades gracefully instead of pushing the frontend into an unknown
    mode.
    """
    raw = get_setting("default_view", env_var="NTASKER_DEFAULT_VIEW")
    if not raw:
        return DEFAULT_VIEW_FALLBACK
    norm = raw.strip().lower()
    if norm not in DEFAULT_VIEW_ALLOWED:
        return DEFAULT_VIEW_FALLBACK
    return norm


def get_claude_open_terminal() -> bool:
    """Whether a starting Claude session opens its terminal immediately.

    Honours the ``NTASKER_CLAUDE_OPEN_TERMINAL`` ENV override. Defaults to
    ``True`` (the historical behaviour: Create + Run jumps straight into the
    run view). A falsy value makes the session start in the background while
    the user stays on the board.
    """
    raw = get_setting("claude_open_terminal", env_var="NTASKER_CLAUDE_OPEN_TERMINAL")
    if raw is None:
        return True
    return raw.strip().lower() in _TRUE_STRINGS


def get_default_agent() -> str:
    """Return the configured default agent key (``claude`` / ``opencode`` / ``pi``).

    Honours the ``NTASKER_DEFAULT_AGENT`` ENV override. Falls back to
    :func:`ntasker.agents.default_agent_key` when unset, invalid or pointing
    at a disabled agent, so a stale value never pushes a task onto an
    unknown agent.
    """
    raw = get_setting("default_agent", env_var="NTASKER_DEFAULT_AGENT")
    if not raw:
        return default_agent_key()
    norm = raw.strip().lower()
    return norm if norm in agent_keys() else default_agent_key()


def get_queue_enabled() -> bool:
    """Whether the task queue starts queued tasks on its own. Defaults to True.

    Honours the ``NTASKER_QUEUE_ENABLED`` ENV override. See
    :func:`validate_queue_enabled` and :mod:`ntasker.taskqueue`.
    """
    raw = get_setting("queue_enabled", env_var="NTASKER_QUEUE_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in _TRUE_STRINGS


def _get_on_off(key: str, default: bool) -> bool:
    raw = get_setting(key, env_var=f"NTASKER_{key.upper()}")
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_STRINGS


def get_dir_locks() -> bool:
    """Whether the queue honours directory locks (default on). ENV ``NTASKER_DIR_LOCKS``."""
    return _get_on_off("dir_locks", True)


def get_require_clean() -> bool:
    """Whether a queued task needs git-clean directories to start (default off).

    ENV ``NTASKER_REQUIRE_CLEAN``. Only consulted while :func:`get_dir_locks`.
    """
    return _get_on_off("require_clean", False)


def get_sidebar_sections() -> dict[str, bool]:
    """Open/folded state per sidebar section; every section defaults to open."""
    import json  # noqa: PLC0415

    raw = get_setting("sidebar_sections")
    saved = json.loads(raw) if raw else {}
    return {name: saved.get(name, True) for name in SIDEBAR_SECTIONS}


def get_language_setting() -> str:
    """Return the raw ``language`` setting value (default ``auto``).

    Honours the ``NTASKER_LANGUAGE`` ENV override. Used by the i18n
    middleware (HTTP) and the CLI bootstrap; both interpret ``auto`` in
    their own way.

    Wrapped in a try/except so this is safe to call before the DB exists
    (e.g. during module import in test harnesses) - falls back to
    ``auto`` rather than crashing.
    """
    try:
        raw = get_setting("language", env_var="NTASKER_LANGUAGE")
    except Exception:
        return "auto"
    return raw or "auto"


# ---------------------------------------------------------------------------
# Bootstrap helper
# ---------------------------------------------------------------------------


def ensure_settings_table(conn: sqlite3.Connection) -> None:
    """Belt-and-braces: create the settings table if init_db has not run yet.

    Used by the FastAPI startup hook so a fresh boot against a pre-1.0 DB
    file lands in a known state without requiring ``ntasker init``.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
