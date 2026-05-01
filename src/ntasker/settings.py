"""Settings module — KV-store + validators for ntasker.

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
from pathlib import Path

from ntasker.assets import validate_assets_mode
from ntasker.db import get_conn

Validator = Callable[[str], str]
"""A validator takes the raw value, returns a normalized value, or raises ValueError."""


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_projects_dir(value: str) -> str:
    """Validator for ``projects_dir``.

    Requirements: absolute path, exists, is a directory, readable.
    Returns the absolute, expanded path.
    """
    if not value:
        raise ValueError("projects_dir darf nicht leer sein.")
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise ValueError(f"projects_dir muss absolut sein: {value!r}")
    if not os.path.isdir(expanded):
        raise ValueError(f"projects_dir existiert nicht oder ist kein Verzeichnis: {expanded}")
    if not os.access(expanded, os.R_OK):
        raise ValueError(f"projects_dir ist nicht lesbar: {expanded}")
    return expanded


VALIDATORS: dict[str, Validator] = {
    "projects_dir": validate_projects_dir,
    "assets_mode": validate_assets_mode,
}
"""Registry of known settings keys with their validators.

Unknown keys are still writable (forward-compat for ad-hoc keys via the
CLI / API), but they bypass validation. Keys *with* a validator MUST pass
it before any DB write.
"""


# Hint texts shown next to known keys in the /settings UI. Free-form text;
# keep short and German-language for Christian.
HINTS: dict[str, str] = {
    "projects_dir": (
        "Verzeichnis mit Projekt-Symlinks "
        "(z.B. /home/<user>/Projects). Wird für /api/projects gelesen."
    ),
    "assets_mode": (
        "Vendor-Assets (Tabler/Alpine): cdn (default, jsDelivr + SRI), "
        "local (aus User-Data-Dir, vorher mit `ntasker assets fetch` laden), "
        "auto (local wenn Cache vollständig, sonst cdn)."
    ),
}


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
# Convenience: projects_dir helpers
# ---------------------------------------------------------------------------


def get_assets_mode_resolved() -> str:
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


def get_projects_dir() -> Path | None:
    """Return the configured projects directory or ``None``.

    Honours the ``NTASKER_PROJECTS_DIR`` ENV override. Validates the path
    on read so a stale DB row pointing at a deleted directory degrades to
    ``None`` (the UI then shows the "configure projects_dir" banner).
    """
    raw = get_setting("projects_dir", env_var="NTASKER_PROJECTS_DIR")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        return None
    return path


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
