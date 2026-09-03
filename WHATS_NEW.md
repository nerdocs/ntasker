# What's new in v2.20.0 (compared to v2.19.4)

v2.20.0 turns ntasker from a task tracker into the entry point of a whole
working environment: the tracker now knows about the **workspace** around your
tasks -- team personas, skills, a knowledge base and generated documents --
and lets you hand that context straight to the agent that runs a task.

## Workspace integration

- **New `/workspace` page** with cards for four configurable directories:
  - **Team** -- agent-persona Markdown files (one file per persona, role from
    front matter or a bold `Role:` line).
  - **Skills** -- your Claude Code skills, each with a load/broken verdict so a
    silently failing skill is visible at a glance. Defaults to `~/.claude/skills`.
  - **Knowledge base** -- the areas of a Markdown wiki (e.g. an Obsidian vault)
    with per-area note counts.
  - **Documents** -- generated output files, newest first, with in-place
    preview for Markdown, text and CSV.
- **Sidebar sections** for all four areas, collapsible like every other sidebar
  block, with search and a "show all" hand-off to the workspace page.
- **Read/write, safely bounded:** viewing, editing, renaming, creating and
  deleting workspace files works from the UI. Every operation is confined to
  the configured workspace roots, and deletes go to the OS trash -- nothing is
  ever destroyed outright.
- **Configurable in Settings:** `workspace_skills_dir`, `workspace_wiki_dir`,
  `workspace_team_dir`, `workspace_docs_dir` are ordinary settings with
  validation (absolute path, `~` allowed, existence deliberately not enforced
  so cloud folders may be temporarily absent). Unset directories simply hide
  their card -- a fresh install looks exactly like 2.19.4.

## Task context attachments

- **Attach workspace files to a task**: the persona to think as, the skill
  that applies, the note holding the prior art, the document being produced.
  Attachments are pointers, not copies -- the file stays the single source of
  truth.
- Attach from the **edit modal** or -- new -- **directly in the create form**,
  so a task can start life with its context. `POST /api/tasks` accepts a
  `context` list; entries are validated against the workspace roots *before*
  the task is created.
- Each attachment can carry a one-line **note** ("why is this here"), and the
  agent receives all attached context in its briefing at spawn.
- Attachment chips show a warning when the underlying file has moved away.

## Project organisation in the sidebar

- **Free-form project categories**: assign any category ("Coding", "Lab",
  "Home", ...) to a project via the pencil that appears on hover. The sidebar
  groups projects under collapsible category headers; uncategorized projects
  form a trailing group. While no category exists the list renders flat,
  exactly as before. Autocomplete offers your existing categories.
- **One-click project hiding**: the eye-off button removes a project from the
  sidebar entirely -- it stays hidden even with "show empty projects" on.
  Hiding is a persisted veto, not a delete: tasks keep their project value,
  discovered directories stay on disk. A "show hidden projects" switch reveals
  them greyed-out for one-click restore.
- **Resizable sidebar**: drag the splitter between sidebar and content
  (200-600 px); the width is remembered.

## API additions

| Endpoint | Purpose |
|---|---|
| `GET /api/workspace` | Full workspace inventory (team, skills, wiki, docs) |
| `GET/POST /api/workspace/file` | Read / write one workspace file |
| `GET /api/workspace/browse` | Directory listing inside the roots |
| `POST /api/workspace/rename` / `delete` / `entry` / `reveal` | File management |
| `GET/POST/DELETE /api/tasks/{id}/context` | Task context attachments |
| `PUT /api/projects/category` | Assign / clear a project's category |
| `PUT /api/projects/hidden` | Hide / restore a project |

`GET /api/projects` now returns `category` and `hidden` per project, and
`POST /api/tasks` accepts `context`.

## Schema

Three additive tables, migrated automatically on first boot (no action
needed, older versions ignore them): `task_context`, `project_categories`,
`hidden_projects`.

## Fixes and polish

- Localization pass for all new UI strings (English/German).
- The `/task` loader, agent briefing and run spawning honour attached context.
- Sidebar layout styles moved from inline attributes into the stylesheet.
