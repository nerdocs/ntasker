# Kanban View

Since v2.0 ntasker ships a kanban-board view alongside the classic task
list. Both views share the same data, the same filters, and the same
keyboard shortcuts -- the kanban view just re-groups open tasks by
workflow phase.

## Two views, one toggle

The page header carries a pair of pseudo-tabs:

| Toggle | What it shows |
|---|---|
| **Task list** (`ti-list-check`) | Classic flat list with Open / Done / Archive tabs and per-row actions (edit, archive, delete). |
| **Kanban** (`ti-columns`) | 4-column board: Planned -> In Progress -> Review -> Done. |

The active mode is persisted to `localStorage` (`ntasker.viewMode`). On a
fresh browser the server-side `default_view` setting kicks in instead
(see below).

## Phases (since v2.0)

The phase vocabulary changed in v2.0 and is now NOT NULL:

| Old (pre-v2.0) | New (v2.0) | Notes |
|---|---|---|
| `wip` | `wip` | "In Progress" -- unchanged |
| `planned` | `planned` | "Planned" -- also absorbs legacy `later` and NULL on migration |
| `later` | -- | Collapsed into `planned` by `init_db` |
| _NULL_ | -- | Collapsed into `planned` by `init_db` |
| _new_ | `review` | "Review" -- staging column before done |

Workflow direction: `planned -> wip -> review -> (done)`.

Done is **not** a phase value: it's `status='done'`. The kanban view
derives the fourth column from status, so dragging a card from `review`
into `done` issues `PATCH {"status": "done"}` rather than a phase
change. Dragging it back from `done` into any other column flips status
to `open` and (if the column differs) updates phase.

## Drag & drop

- Cards are HTML5-draggable (`draggable="true"`).
- Drop on a column header or empty column body triggers `onColumnDrop`:
  - Same column -> no-op.
  - Phase column -> `PATCH /api/tasks/<id> {"phase": "<col>"}` (and
    `"status": "open"` if coming out of Done).
  - Done column -> `PATCH /api/tasks/<id> {"status": "done"}`; phase
    stays so re-opening lands the card back in its previous column.
- Drop **onto another card** triggers `onCardDrop` (which `.stop`s the
  column handler): the card is inserted above or below the target,
  depending on which half it was dropped over. When the target sits in a
  different column this also applies the phase/status flip above, so a
  cross-column drop moves *and* positions in one go.
- Failure (HTTP non-2xx) shows a toast; the UI state is reloaded from
  the server, so a failed move never leaves the board out of sync.

### Manual order (`sort_order`)

Both views honour a manual drag&drop order stored per task in the
`sort_order` column (`REAL`). Rows are served `sort_order DESC` -- larger
values sit nearer the top. New tasks get `MAX(sort_order)+1`, so they
land on top (matching the previous newest-first default); the migration
backfills existing rows from their `id` to preserve that order.

Reordering uses **fractional indexing**: dropping a card between two
neighbours stores the average of their `sort_order` values, so a single
`PATCH /api/tasks/<id> {"sort_order": <value>}` rewrites only the moved
row -- no renumbering of the whole column. The same mechanism drives the
list view, where each row carries a `ti-grip-vertical` drag handle (only
the handle starts a drag, so the row's click targets keep working).

## Done column

Done renders as a fourth column, collapsed by default so the three
workflow columns get the real estate. Click the chevron in the column
header to expand/collapse; the choice is persisted to localStorage
(`ntasker.kanbanDoneCollapsed`).

Archived tasks are **not** shown in kanban -- archive is a list-view
concept. Switch to the Task list view + Archive tab to see them.

## Filtering

All sidebar filters (project, tag, phase, priority) and the search box
work in both views. The kanban view groups whatever the filter returns
by phase; an empty phase column shows `(empty)` instead of being
hidden.

The Open / Done / Archive status tabs only render in list view -- in
kanban they're redundant (Open lives in the phase columns, Done has its
own column, Archive is intentionally out of scope).

## The `default_view` setting

```bash
# CLI
ntasker config set default_view kanban
ntasker config set default_view list   # back to the classic view

# API
curl -X PUT http://127.0.0.1:8766/api/settings/default_view \
     -H 'Content-Type: application/json' -d '{"value": "kanban"}'

# UI
# /settings -> "default_view" -> kanban
```

Validator whitelist: `list` | `kanban`. Anything else returns HTTP 400
with the validator's error message.

ENV override (per-shell pin): `NTASKER_DEFAULT_VIEW=kanban ntasker serve`.

Resolution order on page load:

1. `localStorage.ntasker.viewMode` (the user's last active choice).
2. `window.__defaultView` injected by the server from the setting.
3. Hard-coded fallback `list`.

This means: once a user has clicked the Kanban tab even once,
`default_view` no longer affects their browser -- localStorage takes
over. To force the server-side default again, clear the
`ntasker.viewMode` key in the browser's storage.

## Migration notes

`init_db()` runs the phase migration on every boot; it's idempotent and
costs one UPDATE on existing rows. There's no separate `ntasker migrate`
command -- the FastAPI lifespan and `ntasker init` both call into
`init_db()`, which carries the migration.

Clients that still send `phase: "later"` get HTTP 422 from FastAPI's
Pydantic Literal check; clients that send `phase: null` are coerced
server-side to `planned` so legacy form posts keep working.
