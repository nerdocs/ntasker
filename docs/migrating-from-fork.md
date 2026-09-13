# Migrating from the drfoehn fork

This page is for a database and a Claude Code setup that ran the `feat/workspace-integration` fork (its v2.20.x
line). Upstream absorbed that work as plugins; the fork's data migrates in place on the first boot of an upstream
release. Nothing needs to be rebased or re-entered.

## How to switch

1. Copy `tasks.db` once (`~/.local/share/nTasker/tasks.db` on Linux, `~/Library/Application Support/nTasker/` on
   macOS). That copy is the whole rollback story.
2. Install the upstream release over the fork: `uv tool install --force ntasker==<version>` (or
   `pip install -U ntasker`), then start it once.
3. Refresh the agent integration files -- the `/task` loader and templates changed:
   `ntasker agent install claude --force` (and `opencode` / `pi` if installed). Timestamped backups are written.
4. Open `/settings`: every feature is a plugin now (*Plugins* card). `task_context` and `workspace` are on by
   default; switch off what you do not use.

## What migrates automatically

`init_db()` runs these once, idempotently, on the first boot:

| Fork state | Upstream | Migration |
|---|---|---|
| table `task_context` | same table, same columns | kept as-is |
| table `hidden_projects` | same table | kept as-is |
| table `project_categories` | setting `project_groups` | rows folded in (existing overrides win), table dropped |
| setting `brain_server` | -- | inert row; delete it on `/settings` |

## What changed in behaviour

- **Project categories are families.** A category becomes a manual family override in the sidebar tree
  ([projects.md](projects.md)); the automatic name-prefix rule applies on top. The pencil / eye-off hover buttons are
  gone -- the row's `...` menu has *Group...*, *Hide* and *Board* instead. `PUT /api/projects/category` no longer
  exists.
- **Hiding is unchanged** (`hidden_projects` table, `PUT /api/projects/hidden`), only triggered from the row menu.
- **Team members are Claude Code subagents.** The team directory defaults to `~/.claude/agents`, and a member's role
  comes from the front matter `description` (first line). A bold `Role:` line or a `rolle:` key is no longer read;
  give persona files a Claude-style front matter (`name`, `description`) to keep them listed. Attachments of kind
  `member` are handed to the agent as subagents to delegate to, not as prose to read.
- **No JCBrain.** `brain.py`, `/api/brain/*` and the `brain` context kind were not ported. Rows with `kind='brain'`
  stay in `task_context` but are ignored on read (kind whitelist); the `open-brain` MCP server can still be attached
  as kind `mcp`.
- **Sidebar resize** is upstream's implementation (same drag handle, width stored under a different key).
- **Board** moved from a per-row button into the row menu.
- **Native picker** is unchanged (macOS `osascript`, Linux `zenity`); the Finder-style browser was already dropped in
  the fork.
- **Origin guard** replaces the fork's per-endpoint `_require_local_origin`: every mutating request must carry an
  `Origin` matching the host, enforced once in middleware.

## If the fork keeps developing

Any table or setting the fork adds after v2.20.1 has no migration here. Add one under the same pattern
(`init_db()`, `try/except sqlite3.OperationalError`, idempotent) before pointing that database at upstream.
