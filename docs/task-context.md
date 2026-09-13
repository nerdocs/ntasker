# Task context attachments

Plugin `task_context` (see [plugins.md](plugins.md)). A task can carry **attachments**: pointers to files, notes,
personas, skills and MCP servers the agent should have in hand when it starts. Attachments are pointers, not copies --
the file stays the single source of truth. The agent receives the list in its briefing at spawn, which is the whole
point: otherwise the user re-types the same three paths every run.

## Kinds

| Kind | Points at | Boundary |
|---|---|---|
| `file` | any file or folder on this machine | none -- named explicitly by the user |
| `note`, `doc`, `skill`, `member` | a file under a configured workspace root | `workspace_*_dir` settings |
| `mcp` | an MCP server declared in `~/.claude.json` (`mcp://<name>`) | must exist in that file |

`member` files are Claude Code subagent definitions (`~/.claude/agents/<name>.md`); the briefing tells the agent to
delegate to them via the Agent tool rather than read them as prose. Rows of any other kind (a fork's `brain` notes,
say) stay in the table but are ignored on read.

Because a `file` attachment escapes the workspace roots by design, the write endpoints rely on the origin guard
(`OriginGuardMiddleware`: `Origin` must match the host) so a foreign page cannot plant one.

## Where they show up

- **Create form** -- *Attach* opens the picker; picks collect in the draft and go with `POST /api/tasks` (`context`
  list), validated before the insert so a bad path never leaves a half-created task.
- **Edit modal** -- attach/detach writes straight through to the server, independent of Save.
- **Task cards** (list and board) -- chips; red when the file has moved away. Clicking a chip opens the entry in the
  desktop's default application (or in the workspace viewer when that plugin is enabled).
- **Briefing** -- `## Attached context` block in the compact seed and in the `/task` loader output; a `note` on an
  attachment ("why is this here") is passed verbatim.
- **CLI** -- `ntasker show <id>` prints an *Attached context* block.

## Picker

The *Files* tab opens the OS file dialog on this desktop (`POST /api/fs/pick`; macOS `osascript`, Linux `zenity`,
otherwise 501 and the buttons stay hidden) or takes pasted paths, one per line, each checked via `GET /api/fs/resolve`.
The *MCP servers* tab lists `~/.claude.json`. Team / Skills / Knowledge / Documents tabs appear only with the workspace
plugin enabled and the directory configured.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks/{id}/context` | attachments of one task |
| POST | `/api/tasks/{id}/context` | attach `{kind, path, label?, note?}`; re-attaching a path updates its note |
| DELETE | `/api/tasks/{id}/context/{cid}` | detach (never touches the file) |
| GET | `/api/tasks/{id}/context/{cid}/file` | preview payload of the attached file |
| POST | `/api/tasks/{id}/context/{cid}/reveal` | open the file in the desktop's default app |
| GET | `/api/context/mcp` | MCP servers for the picker (no secrets) |
| GET / POST | `/api/fs/pick` | native dialog availability / open it |
| GET | `/api/fs/resolve?path=` | normalise a typed path, report existence |

`GET /api/tasks[...]` responses carry `context: [...]` per task while the plugin is enabled.

## Schema

Table `task_context(id, task_id, kind, path, label, note, created_at)`, unique per `(task_id, path)`, cascades with
the task. Created by the plugin's schema hook on every `init_db()` -- also while the plugin is disabled, so data
survives a toggle.
