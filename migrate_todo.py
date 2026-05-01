"""One-shot migration: TODO.md -> tracker.db.

Reads /home/christian/nerdocs/TODO.md, parses bullet items prefixed by status emojis
and inserts them into the tasks table.

Idempotent: skips rows whose title already exists in `tasks`.

After a successful run the source file is renamed to TODO.md.migrated-<DATE>
to make sure no double-imports happen on subsequent runs.

Usage:
    cd /home/christian/nerdocs/tracker
    .venv/bin/python migrate_todo.py
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

NERDOCS_DIR = Path("/home/christian/nerdocs")
TODO_PATH = NERDOCS_DIR / "TODO.md"
PROJECTS_DIR = NERDOCS_DIR / "Projekte"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tasks.db"

# Status emoji -> (status, phase, archived)
STATUS_MAP: dict[str, tuple[str, str | None, int]] = {
    "✅": ("done", None, 1),
    "⏳": ("open", "wip", 0),
    "⏹️": ("open", "planned", 0),
    "⏹": ("open", "planned", 0),  # without VS-16
    "🔮": ("open", "later", 0),
    "❓": ("open", None, 0),
}

# Match bullet lines, allowing leading whitespace (sub-items).
# Group 1: status emoji. Group 2: rest of line.
BULLET_RE = re.compile(
    r"^\s*-\s+("
    + "|".join(re.escape(e) for e in STATUS_MAP)
    + r")\s+(.*)$"
)

# Match a leading **bold** chunk. Used to strip the "<title>:" preamble.
BOLD_RE = re.compile(r"^\*\*(.+?)\*\*\s*[:.]?\s*(.*)$")


def list_known_projects() -> set[str]:
    """All symlink names under nerdocs/Projekte/, used for project heuristic."""
    if not PROJECTS_DIR.exists():
        return set()
    return {
        p.name
        for p in PROJECTS_DIR.iterdir()
        if p.is_symlink() and not p.name.startswith(".") and not p.name.endswith(".lock")
    }


def split_title_description(rest: str) -> tuple[str, str | None]:
    """Extract a clean title and description from the bullet body.

    Strategy:
      1. If the line starts with **bold**, that's the title (strip parenthetical metadata).
      2. Description = whatever follows the first colon after the bold chunk.
      3. If no bold, take the first sentence (up to a colon or full stop) as title.
    """
    rest = rest.strip()
    m = BOLD_RE.match(rest)
    if m:
        title_raw = m.group(1).strip()
        # Drop trailing parenthetical metadata, e.g. "(HERMINE, 2026-04-30)".
        title = re.sub(r"\s*\([^)]*\)\s*$", "", title_raw).strip()
        description = m.group(2).strip() or None
        return title, description

    # Fallback: take up to first colon or 120 chars as title.
    if ":" in rest:
        head, tail = rest.split(":", 1)
        return head.strip()[:500], tail.strip() or None
    if len(rest) <= 120:
        return rest[:500], None
    return rest[:120].strip() + "…", rest


def detect_project(title: str, description: str | None, known: set[str]) -> str | None:
    """Heuristic: scan title+description for the longest matching known project name.

    Longest-match wins so 'medux-cashbook' beats 'medux'.
    Special case: bare 'GDAPS' / 'gdaps' -> 'gdaps'.
    """
    haystack = f"{title} {description or ''}"
    candidates = sorted(known, key=len, reverse=True)
    for name in candidates:
        # Word-ish boundary: name surrounded by non-alphanumeric or string ends.
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", haystack):
            return name
    if re.search(r"\bgdaps\b", haystack, re.IGNORECASE) and "gdaps" in known:
        return "gdaps"
    return None


def parse_todo(path: Path, known_projects: set[str]) -> list[dict]:
    """Return list of task dicts ready for INSERT."""
    items: list[dict] = []
    if not path.exists():
        return items

    in_legend = False  # Skip lines under a "Status-Legende" header.
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("**Status-Legende"):
            in_legend = True
            continue
        if in_legend:
            # Legend ends at the first horizontal rule or an empty line followed by content.
            if stripped.startswith("---") or stripped.startswith("##"):
                in_legend = False
            else:
                continue

        m = BULLET_RE.match(raw_line)
        if not m:
            continue
        emoji, body = m.group(1), m.group(2)
        status, phase, archived = STATUS_MAP[emoji]
        title, description = split_title_description(body)
        if not title:
            continue
        project = detect_project(title, description, known_projects)
        items.append(
            {
                "title": title,
                "description": description,
                "project": project,
                "status": status,
                "phase": phase,
                "archived": archived,
            }
        )
    return items


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            phase TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_archived ON tasks(archived);
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """
    )


def main() -> int:
    if not TODO_PATH.exists():
        print(f"TODO.md not found at {TODO_PATH} -- nothing to do.")
        return 0

    known_projects = list_known_projects()
    items = parse_todo(TODO_PATH, known_projects)

    if not items:
        print("No bullet items matched the status pattern.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)

        existing = {
            row["title"]
            for row in conn.execute("SELECT title FROM tasks").fetchall()
        }

        migrated = 0
        skipped = 0
        errors = 0
        for it in items:
            if it["title"] in existing:
                skipped += 1
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO tasks
                        (project, title, description, status, phase, archived)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        it["project"],
                        it["title"],
                        it["description"],
                        it["status"],
                        it["phase"],
                        it["archived"],
                    ),
                )
                migrated += 1
            except sqlite3.Error as exc:
                errors += 1
                print(f"  ERROR on '{it['title']}': {exc}", file=sys.stderr)

        conn.commit()
    finally:
        conn.close()

    print(f"Migration: {migrated} migrated, {skipped} skipped, {errors} errors.")

    # Backup TODO.md so a second run doesn't re-import (unless user puts it back).
    if migrated > 0:
        backup = TODO_PATH.with_suffix(f".md.migrated-{date.today().isoformat()}")
        if not backup.exists():
            TODO_PATH.rename(backup)
            print(f"Renamed {TODO_PATH.name} -> {backup.name}")
        else:
            print(f"Backup {backup.name} already exists; left TODO.md untouched.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
