# Live Updates

The web UI updates itself when the data changes underneath it -- whether the change came from another browser
tab, the `ntasker` CLI, or Claude driving the CLI. A `/task 34` that flips a task to *In Progress*, a
`ntasker patch #34 --phase review`, an edit in a second tab: all of them move the card in every open UI within
about a second and a half, no reload.

## Why polling (and not a WebSocket)

SQLite has no cross-process change notification (no `LISTEN`/`NOTIFY` like Postgres). The CLI writes straight to
SQLite in its own process, so *someone* has to poll to notice that a write happened -- a WebSocket would not turn
this into true server push, it would only move the same poll onto the server. For a single-user localhost tool the
extra apparatus (background task, broadcast set, reconnect handling) buys almost nothing, so the UI polls a cheap
endpoint directly instead.

The trick is **what** it polls. Pulling the full task list on a timer would be wasteful. Instead the UI polls a
tiny change token and only refetches when the token actually moved.

## The change token

`GET /api/changes` returns the DB file's modification time in nanoseconds:

```
GET /api/changes  ->  {"v": 1782413264291301581}
```

Both the CLI and the API write straight to the same SQLite file, and in rollback-journal mode (ntasker's default)
every commit rewrites the main DB file -- so its mtime bumps on any write, from any process. The token is therefore
a stateless, cross-process "has anything changed?" signal with no server-side connection state to manage.

```
ntasker patch #34 --phase wip      (separate process, own SQLite connection)
        |  commit  ->  DB file mtime bumps
        v
UI poll (every 1.5s) sees /api/changes token change  ->  refetch task list + sidebar counts
        v
Alpine re-renders the card in place (keyed by task id)
```

Cards re-render *in place* because both the list and the kanban board key their rows by task id
(`:key="task.id"`), so Alpine reuses the existing DOM node instead of rebuilding the list -- the card visibly
transitions between the Planned / In Progress / Review / Done columns.

> **Note:** the mtime token relies on rollback-journal mode. Under WAL, commits land in the `-wal` sidecar and the
> main-file mtime would lag until a checkpoint -- ntasker does not enable WAL. (`PRAGMA data_version` is the
> WAL-safe alternative, but it is a per-connection property and would require a persistent server-side connection,
> which is exactly the state we set out to avoid.)

## Details and edge cases

- **Poll cadence** -- 1.5s, set in `startChangePolling()` in `app.js`. `/api/changes` is a single `stat()`, so the
  cost is negligible.
- **Only refetches on a real change** -- the first poll seeds a baseline; later polls refetch only when the token
  differs. An idle UI makes one tiny request every 1.5s and nothing else.
- **Own actions stay instant** -- a tab that performs a write still refreshes eagerly (see `refreshAll`); the poll
  is what covers changes the tab did not make.
- **Mid-drag** -- a change detected while a kanban card is being dragged is deferred until `dragend`, so the
  re-render never aborts the in-flight drag.
- **Server restart** -- a failed poll (server momentarily down during `stop`/`serve` or `--reload`) is ignored; the
  next tick picks up again and the first successful poll re-establishes the baseline.
