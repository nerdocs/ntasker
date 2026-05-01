"""DB-path resolver for ntasker.

Precedence (highest first):

1. CLI flag ``--db <path>``  -- explicit per-invocation override.
2. ``NTASKER_DB`` environment variable -- per-shell / per-deploy override.
3. ``platformdirs.user_data_dir("nTasker") / "tasks.db"`` -- the OS-native
   default (``~/.local/share/nTasker/tasks.db`` on Linux).

The display name ``nTasker`` (camelCase) is intentional; the package name
remains lowercase ``ntasker``. The directory is created on demand so a
fresh install can run ``ntasker init`` without manual mkdir.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import platformdirs

APP_NAME = "nTasker"  # display name -> platformdirs folder
DB_FILENAME = "tasks.db"
ENV_VAR = "NTASKER_DB"


def default_db_path() -> Path:
    """Return ``platformdirs.user_data_dir(APP_NAME) / tasks.db``."""
    return Path(platformdirs.user_data_dir(APP_NAME)) / DB_FILENAME


def resolve_db_path(cli_flag: str | None = None) -> Path:
    """Return the active DB path according to the documented precedence.

    Creates the parent directory if it does not yet exist. Does *not*
    create or migrate the DB file itself -- that is :func:`ntasker.db.init_db`'s
    job, called only by ``ntasker init`` or the FastAPI startup hook.
    """
    if cli_flag:
        path = Path(cli_flag).expanduser().resolve()
    else:
        env = os.environ.get(ENV_VAR)
        path = Path(env).expanduser().resolve() if env else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def warn_if_missing(path: Path) -> None:
    """Print a friendly stderr hint if the DB file does not exist yet.

    Called by the CLI before any read-only command (``list``, ``show``, ...)
    so the user gets a nudge towards ``ntasker init`` instead of an opaque
    "no such table" SQLite error. We do *not* raise -- the caller may still
    decide to proceed (init_db is idempotent and creates tables on demand).
    """
    if not path.exists():
        print(
            f"ntasker: DB nicht gefunden bei {path}.\n"
            "         Ausfuehren: `ntasker init`",
            file=sys.stderr,
        )
