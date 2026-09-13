# Projects in the Sidebar

ntasker has no project table. A project is a *name*, and the sidebar shows every
name that is currently in use. On top of those names sits a second, purely
presentational layer -- *families* -- that folds related projects into a tree.
This page explains both layers and how they interact.

## Layer 1: project names

A project name comes from one of two sources, and both are directory names:

| Source | Where the name comes from |
|---|---|
| Tasks | `tasks.project` -- whatever was typed or passed when the task was created. |
| Claude projects | Folders under `~/.claude/projects`, i.e. every directory an agent session was started in. |

`GET /api/projects` returns the union of both, sorted case-insensitively, with
the cross-project sentinel `__none__` first. Discovered folder names are
relativized against the `projects_base` setting when set (`~/Projekte/thrito`
becomes `thrito`), otherwise against the home directory. A task-derived name
disappears as soon as its last task is gone; a discovered one stays as long as
the folder exists.

The name is therefore *the* identity: it is what the `project=` filter matches,
what a task stores, and what an agent session's working directory resolves to
when a task is run. Nothing in the family layer ever changes it.

## Layer 2: families

The family layer decides only *where a row sits in the sidebar*. It never
touches tasks, filters, or the API -- it is a view over layer 1.

### Automatic: the name prefix

Every project name is split at its first `-`, `_` or `/`; the part before the
separator is the project's *prefix*:

| Name | Prefix |
|---|---|
| `thrito` | `thrito` |
| `thrito-meta` | `thrito` |
| `medspeak_server` | `medspeak` |
| `isenguard/scripts` | `isenguard` |
| `ntasker` | `ntasker` (no separator -- the whole name) |

Projects with the same prefix form a *family* once at least two of them are
visible. A single `cblm-fhir` stays a flat row; `thrito`, `thrito-meta` and
`thrito-medication` fold under `thrito`.

Who heads the family depends on whether the prefix is itself a project:

- **Prefix is a project** (`thrito`): that project's row is the head -- with its
  own checkbox, count and actions -- and a chevron in front of it.
- **Prefix is not a project** (`medspeak`): a label-only header with a folder
  icon and the family's summed open-task count.

Children show their name *minus* `<prefix><separator>` (`meta`,
`medication-pane`). The tree is one level deep: `thrito-medication-pane` sits
next to `thrito-medication`, not under it.

Families start folded. A folded head carries a small dashed pill with the number
of hidden children; it turns primary-coloured when one of those children is part
of the active filter, so a filter never disappears behind a closed fold. Which
families are open is remembered per browser (`localStorage`
`ntasker.expandedProjectGroups`).

### Manual: overrides

The prefix rule is a heuristic and has two blind spots: projects that belong
together but share no prefix (`gdaps` and `conjunto`), and false positives
(`django-oracle11` has nothing to do with a `django` family). Both are fixed by
one setting:

```
project_groups = {"gdaps": "conjunto", "django-oracle11": ""}
```

A JSON object mapping a project name to the family it belongs to. The rule in
the UI is a single line:

```
family(name) = project_groups[name]  if name in project_groups
               prefix(name)          otherwise
```

- The family name is free text. If it matches a project, that project's row is
  the head; otherwise the family gets a label-only header -- exactly as with
  automatic families.
- An empty string opts the project out of every family; it stays a flat row.
- A family that contains a manually placed member is shown even with a single
  project -- the intent is explicit, so the "two members" rule does not apply.
- A manually placed child keeps its full name as label (`gdaps` under
  `conjunto`), because there is no prefix to strip.
- Choosing what the prefix rule would pick anyway removes the override instead
  of storing it, so the map stays minimal.

Two ways to set an override, both in the sidebar:

- **Row menu -> Group...** opens an inline input, prefilled with the current
  family (empty when the project only stands for itself) and offering every
  known family name as a suggestion. Enter saves, Escape cancels.
- **Drag and drop**: drag a project row onto another row. While dragging, every
  existing family shows a faint dashed outline; the row under the cursor fills
  in. Dropping on a family (head or child) joins it; dropping on a lone project
  founds a new family headed by that project. If the target had opted out of
  its own prefix family, it is pulled back in so it really heads the new family.

The setting is server-side (`GET/PUT /api/settings/project_groups`,
`ntasker config set project_groups '{...}'`), so every client sees the same
tree. It is validated: must be a JSON object of string -> string; keys are
trimmed, empty keys dropped.

## Hiding projects

Independent of families, a project can be hidden from the sidebar via the row
menu (**Hide**). Hidden names live in the `hidden_projects` setting (JSON array,
server-side). A hidden project also leaves the active filter, so no invisible
row keeps narrowing the task list. The **Hidden** switch above the list shows
them again, dimmed, with **Unhide** in the menu; the **Empty** switch does the
same for projects without open tasks. Both switches only appear when they have
something to reveal, and their state is per browser.

Hiding and grouping compose naturally: a hidden child simply drops out of its
family's visible members; if that leaves the family with one automatic member,
it falls back to a flat row.

## What families are not

- Not a filter. Checking a family head filters that one project, not its
  children -- the checkbox belongs to the row, the chevron to the family.
- Not stored on tasks. Renaming or regrouping never rewrites `tasks.project`.
- Not nested. One level, by design; the name prefix carries no deeper structure.
