# CLI reference

Every command honours the global flags below and resolves the database via the
[DB path precedence](configuration.md#db-path-resolution).

| Command                     | What it does                                                  |
|-----------------------------|---------------------------------------------------------------|
| `ntasker init`              | Create / migrate the schema at the active DB path             |
| `ntasker serve` / `start`   | Run the FastAPI server (defaults: 127.0.0.1:8766); `start` is an alias |
| `ntasker stop`              | Shut down a running server over HTTP (`POST /shutdown`)       |
| `ntasker restart`           | Restart the server: via the installed service if there is one, else stop + start **detached**. `--foreground` keeps it in your terminal |
| `ntasker list [filters]`    | List tasks; supports `--project`, `--tag`, `--phase`, ...     |
| `ntasker show <id>`         | Show a single task; pair with `--json` for raw output         |
| `ntasker add --title=...`   | Create a task; optional `--project --phase --priority --tag --agent --model` |
| `ntasker done <id>`         | Mark a task as done                                           |
| `ntasker patch <id> [...]`  | Patch arbitrary fields (`--title`, `--phase`, `--status`, ...)|
| `ntasker tag-add <id> <t>`  | Append a tag                                                  |
| `ntasker tag-rm  <id> <t>`  | Remove a tag                                                  |
| `ntasker stats [filters]`   | Tab counts (open/done/archive) honoring filters               |
| `ntasker run <id...>`       | Start tasks like the board's run button; `--open` opens the run view |
| `ntasker queue list`        | Show the auto-run queue in run order, plus its on/off state   |
| `ntasker queue add <id...>` | Queue tasks (`--top` inserts at the front); an already-queued id moves |
| `ntasker queue rm <id...>`  | Take tasks out of the queue                                   |
| `ntasker queue clear`       | Empty the queue                                               |
| `ntasker queue start / pause` | Let the queue work through its tasks, or stop starting new ones |
| `ntasker config list`       | Show all settings                                             |
| `ntasker config get <k>`    | Read a setting                                                |
| `ntasker config set <k> <v>`| Write a setting (validated)                                   |
| `ntasker config unset <k>`  | Remove a setting                                              |
| `ntasker enable <plugin>`   | Switch a plugin on; installs its extra's packages if missing  |
| `ntasker disable <plugin>`  | Switch a plugin off                                           |
| `ntasker agent list`        | List agents with CLI availability + `/task` integration status |
| `ntasker agent install <key>` | Install / check an agent's skill + `/task` slash-command (`claude`/`opencode`/`pi`) |
| `ntasker assets fetch / status / remove` | Manage the optional local vendor-asset cache     |
| `ntasker service install / uninstall / status / start / stop` | Run ntasker as an OS service (systemd / launchd) |
| `ntasker self-update`       | Upgrade the package from PyPI, then restart the service       |
| `ntasker completion <shell>` | Print the bash/zsh completion script; `--install` / `--uninstall` hook it into the shell rc |

Global flags:

- `--db <path>` -- override the resolved DB path for this invocation.
- `--version` -- print the package version and exit.

Most listing commands accept `--json` for machine-readable output.

## Shell completion

`ntasker completion bash --install` (or `zsh`) writes a completion script to the user-data dir and sources it from
`~/.bashrc` (`~/.bash_profile` on macOS) / `~/.zshrc`; the same switch lives under *Settings -> Maintenance*. The
script is generated from the CLI's own argument tree and refreshed on every server start, so new subcommands
(including plugin ones) complete without a reinstall. Without installing: `eval "$(ntasker completion bash)"`.
