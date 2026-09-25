# Inbox

Since v3.11 a raw idea does not have to become a task by hand. One text field in the topbar (`Ctrl+K`), one CLI
command (`ntasker in`) and one endpoint (`POST /api/inbox`) take a note as you would jot it down; a background worker
turns it into a **proposed task** with a single stateless `claude -p` call that picks title, prompt, priority, tags
and -- from a catalog of one-paragraph project summaries -- the project. You confirm with one click. Nothing you did
not confirm ever becomes a task.

## The flow

1. **Capture.** The note lands in the `inbox` table as `pending` -- from the topbar field, `ntasker in "..."`
   (or piped in), or `POST /api/inbox`. The Inbox column shows it with a spinner at once.
2. **Triage.** The worker (`ntasker.triage.worker`, one tick every 2 s) takes the oldest pending row and calls
   `claude -p` with the catalog and the note. On success it inserts a task with `proposed = 1`, the model's title,
   priority and tags, the generated prompt as description (the raw note is appended under a `## Original` heading)
   and the whole model output in the task's `triage` column; the inbox row becomes `triaged`. On any failure the row
   becomes `failed` with the reason -- **Retry** puts it back, the trash icon drops it.
3. **Confirm.** A proposal card shows the title, the model's candidate projects as checkboxes (its choice pre-ticked,
   its reason as tooltip), priority, tags, the model's confidence and, when the note was too vague, its clarifying
   question. Click the title to see the generated prompt.
   - **Accept** creates the task from the ticked projects: the first ticked is the task's project, every further one
     becomes a [directory lock](directory-locks.md) (a run that touches several repos). Nothing ticked = cross-project.
   - **Discard** deletes the proposed task; the raw note stays in its inbox row. No confirmation -- there is nothing
     to lose.

A proposal is invisible to everything else: it never shows in `GET /api/tasks` or `ntasker list`, does not count as
open anywhere, cannot be queued, run or loaded via `/task` (the loader stops with `ENTWURF`, like a draft). Only
`GET /api/inbox` serves proposals.

## Naming the project yourself

Start the note with the project: `ntasker: add a --json flag` or `#ntasker add a --json flag`. The prefix (matched
case-insensitively against the catalog, slashes allowed: `medux/online: ...`) is stripped before the call and wins over
the model's choice; the card lists it first with reason `prefix`.

## The catalog and project summaries

The triage chooses from the projects ntasker knows -- the ones referenced by tasks plus the Claude Code projects it
discovers -- minus hidden ones (hidden = your veto), stale ones and any name whose directory does not exist. Each
project is presented with a one-paragraph summary from the `project_summaries` table.

A missing summary is generated **lazily** on the first triage that needs it: one `claude -p` call per project that
reads the top of `CLAUDE.md` and `README.md` plus the top-level directory listing (so a repo without docs still gets a
sentence). A project whose summary cannot be generated is left out of that run and retried next time. With many
projects and no summaries yet, the first triage therefore takes a while -- the column shows the spinner meanwhile.

To edit or regenerate a summary: project row menu -> **Summary...** (inline editor, Enter saves, **Regenerate** asks
Claude again), or `ntasker project summary <name> [--regenerate | --set TEXT]`. An empty summary deletes the row.

## The `claude -p` call

```
claude -p --model <triage_model> --tools "" --output-format json --json-schema <schema> \
       --system-prompt <prompt> --no-session-persistence
```

- The note goes on **stdin**; no argv-length or quoting issues.
- `--tools ""` -- nothing executes; the only bound needed is the timeout (120 s per call). `claude` has no
  `--max-turns`, and structured output is delivered through an internal tool round anyway, so a turn cap would break
  it.
- `--json-schema` + `--output-format json` -- the result carries `structured_output` (parsed) next to `result`
  (string). The worker reads the parsed object first, falls back to `json.loads(result)`, and re-validates everything
  (`parse_result`): unknown projects drop, candidates are cut to three, a bad priority becomes `normal`, tags are
  normalised, confidence is clamped.
- `--system-prompt` replaces the Claude Code persona -- with no tools it is noise, and a fully owned prompt is a stable
  cache prefix. The catalog is sorted and the examples are in insertion order, so the prompt only changes when a
  summary or an example changes.
- cwd is the system temp dir and `--no-session-persistence` is set, so no transcript lands under `~/.claude/projects`.
- The environment is the server's minus the agent markers (`CLAUDECODE`, ...) plus `NTASKER_TASK_ID=inbox`: the
  `session_discovery` hooks fire in `-p` mode too, and that variable makes them return early -- no phantom live
  session.

## Output schema

```json
{"title": "...", "prompt": "...", "project": "ntasker" | null,
 "candidates": [{"project": "ntasker", "reason": "..."}],
 "priority": "critical|high|normal|low", "tags": ["..."],
 "confidence": 0.8, "question": "..." | null}
```

Stored on the task (`triage`) together with `raw` (the note) and `prefix` (whether the note named the project).

## Corrections become examples

Accepting a proposal with a project other than the model's choice (another candidate, or none) records
`(raw note, final project)` in `triage_examples`. The last 20 go into every later triage prompt as few-shot examples
-- the triage learns your sorting without any state of its own. Locks are not part of the example.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `triage_enabled` | `on` | Off hides the field, column and tab; the worker pauses. `NTASKER_TRIAGE_ENABLED` |
| `triage_model` | `haiku` | Model alias/id for `claude -p --model`. `NTASKER_TRIAGE_MODEL` |

Both live under *Settings -> Inbox*.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/inbox` | `{text (1..4000), source?: ui\|cli\|api}` -> 201 inbox row; blank text -> 400 |
| GET | `/api/inbox` | `{items: [rows not yet triaged, oldest first], tasks: [proposals, newest first]}` |
| POST | `/api/inbox/{id}/retry` | Failed row back to `pending`; 409 unless failed |
| DELETE | `/api/inbox/{id}` | 204 / 404 |
| POST | `/api/tasks/{id}/accept` | `{project?, locks?}`: omitted = keep, `null` = cross-project; 409 unless proposed |

| DELETE | `/api/tasks/{id}` | Discard a proposal (the ordinary delete) |
| PUT | `/api/projects/summary` | `{project, summary}`; empty summary deletes the row |
| POST | `/api/projects/summary/regenerate` | `{project}` -> `{project, summary}`; 400 no directory, 502 call failed |

`GET /api/projects` rows carry `summary`; `GET /api/stats` carries `inbox` (proposals + rows not yet triaged).

## CLI

| Command | What it does |
|---|---|
| `ntasker in [TEXT]` | Store a note (`-` or no argument reads stdin); direct DB write, the server triages it |
| `ntasker project summary <name>` | Print (generate when missing); `--regenerate` / `--set TEXT` (`""` deletes) |


## Storage

- `tasks.proposed` (0/1) and `tasks.triage` (JSON, NULL for hand-made tasks).
- `inbox(id, text, source, created_at, status, error, task_id)` -- `status` is `pending` | `triaged` | `failed`;
  `task_id` is nulled when the proposal is discarded.
- `project_summaries(project, summary, updated_at)`.
- `triage_examples(id, text, project, created_at)` -- `project` NULL = cross-project.

## Where the code lives

- `src/ntasker/triage.py` -- catalog, summaries, prefix, prompt, the call, validation, `tick` and `worker`.
- `src/ntasker/app.py` -- the endpoints and the worker's startup hook.
- `src/ntasker/cli.py` -- `ntasker in`, `ntasker project summary`.
- `src/ntasker/templates/index.html` (`inbox_column` macro, topbar field, project menu) and `static/app.js`.

## Out of scope for now

- A bearer token or a non-loopback bind for `POST /api/inbox` (the endpoint shape allows adding it later).
- Auto-accepting above a confidence threshold -- every proposal is confirmed.
- Moving the queue planner to `claude -p`.
- Agents feeding the inbox: only you do. The agent skill's write rules are unchanged.
