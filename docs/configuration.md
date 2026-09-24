# Configuration

Settings are a key/value store in the database with per-key validators. Every key can be set three ways, highest
precedence first: **environment variable** (`NTASKER_<KEY>`) > **database value** (UI or CLI) > built-in default.

```bash
ntasker config list                  # show all settings
ntasker config get projects_dir      # read one
ntasker config set projects_dir ~/Projekte
ntasker config unset language        # back to the default
```

The same values are editable in the browser under `/settings`.

## Bind and exposure

Default `127.0.0.1:8766`. **Do not expose this on a network -- there is no authentication.** Override via
`ntasker serve --host <h> --port <p>` if you really need to. This is a personal local tool, not a multi-user service.

## DB path resolution

Highest precedence wins:

1. `--db <path>` flag on the CLI invocation.
2. Environment variable `NTASKER_DB`.
3. `platformdirs.user_data_dir("nTasker") / "tasks.db"` (default).

Per-OS defaults:

| OS      | Path                                                     |
|---------|----------------------------------------------------------|
| Linux   | `~/.local/share/nTasker/tasks.db`                        |
| macOS   | `~/Library/Application Support/nTasker/tasks.db`         |
| Windows | `%LOCALAPPDATA%\nTasker\tasks.db`                        |

Only the Linux path is regularly tested; the others are derived via `platformdirs`.

## `projects_dir` -- required for the sidebar

ntasker tracks a single directory. Each immediate subdirectory (or symlink to a project repo) inside `projects_dir`
becomes a selectable project in the sidebar and the `project=` API filter. Tasks are assigned to one of these projects
(by folder / symlink name) or stay cross-project (`null`).

```bash
ntasker config set projects_dir ~/Projekte
```

The validator requires an absolute path that exists, is a directory and is readable. The listing is read on demand on
every request -- there is no scan job and no cached project list, so adding or removing a folder shows up on the next
reload.

Projects sharing a name prefix (`thrito`, `thrito-meta`, ...) fold into one family; the row menu and drag-and-drop
override the rule, and projects can be hidden. `misc_project` names an optional catch-all project for one-off questions
that belong to no project -- it is pinned under *Cross-project* and its tasks carry no dependencies or locks;
`misc_no_memory` (`on`/`off`) starts its sessions with auto memory disabled. Full reference: [projects.md](projects.md).

## Language

ntasker ships English (default) and German UI strings. Pick the UI language via the `language` setting:

| Value  | Behaviour                                                              |
|--------|------------------------------------------------------------------------|
| `auto` | Parse the `Accept-Language` HTTP header; fallback English. **Default.** |
| `en`   | Always English.                                                        |
| `de`   | Always German.                                                         |

```bash
ntasker config set language de       # pin to German
NTASKER_LANGUAGE=en ntasker serve    # one-shot ENV override
```

The CLI follows: setting > `LANG` / `LC_MESSAGES` env > English. Translation uses the stdlib `gettext` module;
catalogs live at `src/ntasker/locale/<lang>/LC_MESSAGES/ntasker.{po,mo}`. Adding a language:
[development.md](development.md#translations).

## Vendor assets -- CDN by default, offline on request

Tabler core CSS, the Tabler-Icons webfont and Alpine.js load from [jsDelivr](https://www.jsdelivr.com/) with
[SRI](https://developer.mozilla.org/en-US/docs/Web/Security/Subresource_Integrity) hashes pinned in
`src/ntasker/assets.py`. The wheel ships **no** vendor binaries -- it stays under 100 KB.

For offline use, populate the user-data cache once:

```bash
ntasker assets fetch         # downloads + verifies SRI for each manifest entry
ntasker assets status        # shows mode + per-asset state
ntasker assets remove --yes  # wipes the cache
```

The cache lives at `platformdirs.user_data_dir("nTasker") / "vendor"` (Linux: `~/.local/share/nTasker/vendor`). Mode
selection is via the `assets_mode` setting:

| Value   | Behaviour                                                        |
|---------|------------------------------------------------------------------|
| `cdn`   | always load from jsDelivr (with SRI)                             |
| `local` | always load from the user-data cache (must run `assets fetch`)   |
| `auto`  | local if the cache is complete, else CDN. **Default.**           |

ENV override: `NTASKER_ASSETS_MODE=cdn ntasker serve`. SRI is emitted in both modes (it catches on-disk tampering for
`local` too).
