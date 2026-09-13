# Workspace

Plugin `workspace` (see [plugins.md](plugins.md)). The tracker's tasks live inside a wider working environment:
the Claude Code subagents that work is delegated to, the skills that automate it, a knowledge base of notes, and
the documents that come out of it. The workspace plugin makes those four one click away and lets a task carry
pointers to them (see [task-context.md](task-context.md)).

## Sections and directories

| Section | Setting | Default | What is listed |
|---|---|---|---|
| Team | `workspace_team_dir` | `~/.claude/agents` | subagent files: `name`, `description` (line 1), `model`, `tools` |
| Skills | `workspace_skills_dir` | `~/.claude/skills` | skill directories with a load / broken verdict and the reason |
| Knowledge base | `workspace_wiki_dir` | -- (hidden) | top-level areas with note counts, index notes, Obsidian links |
| Documents | `workspace_docs_dir` | -- (hidden) | files newest-first with kind, size, date (`~/.claude/plans` fits) |

Settings hold the path verbatim (`~` allowed, expanded per machine). Existence is not enforced: a cloud-synced
folder may be temporarily absent, and the scanners report `exists: false` instead of refusing the write.

**Team is Claude-native.** A member is a Claude Code subagent definition -- the same file Claude reads, with
`name` and `description` in its front matter. There is no separate persona format; a bold `Role:` line is not read.

## Where it shows up

- `/workspace` page (topnav grid icon): tabs Skills, Knowledge base, Team, Documents, Tooling (MCP servers from
  `~/.claude.json` with a resolved-or-not runtime verdict; env values are reduced to key names, never shown).
- Sidebar: collapsible sections under the filters, each capped at twelve entries with a "more" link into the page;
  the block appears only once at least one directory exists.
- Viewer modal (sidebar and task chips): Markdown rendered through marked + DOMPurify, CSV as a table, text as-is;
  edit (Cmd/Ctrl+S), rename, create a note, move to trash, open in the desktop's default app. Editing is limited
  to `.md .markdown .txt .csv .tsv .json .log`.

## Security boundary

Every read and write is confined to the configured roots (`allowed_roots` / `within_roots` in
`plugins/workspace/scan.py`); paths are fully resolved first so `..` and symlinks out of a root are refused, and a
root itself can never be renamed or deleted. Nothing is destroyed: delete moves to the OS trash (macOS Finder) or into
`<root>/.ntasker-trash/<timestamp>/`. Mutating endpoints are additionally covered by the origin guard.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/workspace` | full inventory (`skills`, `wiki`, `team`, `docs`, `tooling`) |
| GET | `/api/workspace/file?path=` | preview payload of one file inside the roots |
| PUT | `/api/workspace/file` | `{path, text}` overwrite an editable text file |
| GET | `/api/workspace/browse?path=` | one directory listing inside the roots |
| POST | `/api/workspace/entry` | `{parent, name, directory?}` create a note (`.md` added) or folder |
| POST | `/api/workspace/rename` | `{path, name}` |
| POST | `/api/workspace/delete` | `{path}` move to trash; reports `method` (`os` / `folder`) and `trashed_to` |
| POST | `/api/workspace/reveal` | `{path}` open in the default app |

Vendor assets `marked-js` and `dompurify-js` join the SRI manifest (`ntasker assets fetch` caches them like the rest).
