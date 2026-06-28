# Running ntasker as a service + auto-update

`ntasker serve` is a long-running daemon (unlike the one-shot CLI subcommands), so it benefits from a process
supervisor that starts it at login and restarts it on crash. `ntasker service` generates and installs the native unit
files for you:

- **Linux** -> `systemd --user` units in `~/.config/systemd/user/`.
- **macOS** -> `launchd` LaunchAgents in `~/Library/LaunchAgents/`.

No third-party tooling, no root: everything is user-scoped.

## Install

```bash
ntasker service install                 # service only
ntasker service install --auto-update   # service + daily auto-update
ntasker service install --host 127.0.0.1 --port 8766
```

`install` writes the unit file(s), reloads the manager, and enables + starts the service. With `--auto-update` it also
installs a second unit that runs `ntasker self-update` once a day (see below).

The unit embeds the absolute interpreter path (`<python> -m ntasker serve`) so it survives `uv tool upgrade` /
`pipx upgrade` and does not depend on `PATH`. A `--db <path>` is embedded **only** when you pass one explicitly (or set
`NTASKER_DB`); otherwise the daemon resolves the platform-default DB at runtime.

### Linux: lingering

A `systemd --user` service stops when you log out unless *lingering* is enabled. `install` warns if it is off. Enable it
once with:

```bash
loginctl enable-linger $USER
```

## Status / uninstall

```bash
ntasker service status      # install + active state of the units
ntasker service uninstall   # disable + remove all ntasker units (idempotent)
```

## Auto-update

```bash
ntasker self-update              # upgrade from PyPI, then restart the service
ntasker self-update --no-restart # upgrade only
```

`self-update` runs the package-upgrade command, prints the resulting version, and -- unless `--no-restart` -- restarts
the supervised service so the new code takes effect. The daemon is never overwritten in place: the package is upgraded
first, then the process is restarted cleanly, which is safe for the open SQLite DB.

### Which upgrade command?

`self-update` auto-detects how ntasker was installed:

- a `uv tool` install -> `uv tool upgrade ntasker`
- anything else -> `<python> -m pip install -U ntasker`

Override it explicitly when needed:

```bash
ntasker config set update_command "pipx upgrade ntasker"
ntasker config unset update_command   # back to auto-detection
```

### Scheduling

`service install --auto-update` wires the daily run for you:

- **systemd** -> `ntasker-update.timer` (`OnCalendar=daily`, `Persistent=true`) triggers `ntasker-update.service`.
- **launchd** -> `at.nerdocs.ntasker-update.plist` (`StartCalendarInterval`, 04:00 daily).

Both catch up on a missed run after the machine was asleep/off.
