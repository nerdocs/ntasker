# Team server

By default nTasker is a personal tool: it binds to `127.0.0.1`, has no sign-in, and runs the agent sessions on the
same machine. With `auth_mode = password` one nTasker becomes a **team server** for a small team (2-5 people): everyone
signs in, works on shared projects, comments and gets notified -- and every member's tasks run on **their own
computer**, with their own agent subscription, CLIs and checkouts, through `ntasker worker`.

```
browser ──https──> reverse proxy (TLS) ──> nTasker server (127.0.0.1)  <──wss── ntasker worker (alice's laptop)
                                            tasks, users, queue,        <──wss── ntasker worker (bob's desktop)
                                            terminals relayed                      └─ claude / opencode / pi in a PTY
```

The server never runs an agent itself. It decides what to start (the [queue](task-queue.md)), keeps every run's
terminal buffer and state, and relays the browser terminal to the worker that runs it.

## Modes

| | local (default) | team server |
|---|---|---|
| `auth_mode` | `off` | `password` |
| sign-in | none | password (browsers), access key (workers, CLI, agent hooks) |
| who runs agents | the server itself | each assignee's `ntasker worker` |
| sidebar projects | your Claude Code projects + task projects | task projects you are a member of |

Switching is one setting (`ntasker config set auth_mode password`, or *Settings -> Team*). It is refused until an
enabled administrator with a password exists -- otherwise nobody could sign in to switch back.

`ntasker serve --host <address>` refuses to listen anywhere but loopback while sign-in is off; `--allow-no-auth`
overrides that for a trusted network.

## Setting up the server

```bash
ntasker user add alice --admin           # asks for the password
ntasker user add bob
ntasker config set auth_mode password
NTASKER_ALLOWED_HOSTS=tasks.example.org ntasker serve --port 8766   # behind the proxy, still on loopback
```

Terminate TLS in a reverse proxy and forward to `127.0.0.1:8766`, websockets included. A minimal Caddy site:

```
tasks.example.org {
    reverse_proxy 127.0.0.1:8766
}
```

`NTASKER_ALLOWED_HOSTS` adds the public host name to the origin guard (see [configuration](configuration.md)). The
session cookie is marked `Secure` when the request arrived as HTTPS or carries `X-Forwarded-Proto: https`, which the
proxy sets.

Existing tasks of a former local install belong to the implicit user `local`. To keep them as yours, give that user a
password (`ntasker user passwd local`) and sign in as `local`, or reassign them.

## Users, roles, projects

* **Admins** manage users, access keys of everyone (CLI), project members and all server settings; they see every
  project. Everybody else changes their own display name, password and access keys.
* **Project members** see a project's tasks and work on them. A task without a project is visible to everyone. The
  first task in a new project makes its creator a member; assigning a task to someone makes them a member. Admins edit
  the member lists under *Settings -> Team*.
* Every task has a **creator** and an **assignee** (default: the creator). Its runs execute on the assignee's worker.
* The queue is shared: you see and reorder the entries of your projects; other entries keep their places.

Server-wide settings (directory locks, reports, run rules, plugins ...) are admin-only on a team server.

## Workers

On each member's computer:

```bash
ntasker config set server_url https://tasks.example.org
ntasker config set server_token <access key>      # Settings -> Team -> Access keys
ntasker worker                                    # or: ntasker service install --worker
```

`worker_name` (default: the host name) tells two computers of the same person apart. The worker connects *out*
(`wss://<server>/ws/worker`), so it works from behind NAT or a firewall; nothing connects to it. It reconnects with
backoff when the link drops. The same access key also makes this computer's CLI talk to the server: `ntasker add`,
`list`, `show`, `patch`, `done`, `delete`, `report`, `in`, `tag-add`, `tag-rm` then go through the server's API instead
of a local database.

The agent sessions get `NTASKER_URL` and `NTASKER_TOKEN` in their environment, so `ntasker finish`, `ntasker patch` and
the agent hooks inside a session report to the server directly. Project directories are resolved with **this
computer's** settings (`projects_base`, `no_project_dir`, the `<agent>_bin` overrides).

### What runs where

* A queued task starts on the most recently connected worker of its assignee. With none connected the queue entry
  waits and shows *offline*.
* The browser terminal is relayed: the run's owner (the worker's user) types; other members who can see the task
  watch read-only. The resume button reopens a session on the assignee's worker -- only the assignee can.
* The conversation pane and the board's "current step" read a mirror of the session transcript the worker keeps up
  to date (every 2 s). The Diff view shows what the worker computed (every 10 s and at the end of the run).
* A dropped link keeps the runs for 60 s; a worker that reconnects in time takes them back. A worker's runs also
  outlive a **server** restart: the worker reconnects and hands them back with their terminal buffer.

### Protocol

JSON text frames over the websocket; `data` fields are base64.

| Direction | Message | Meaning |
|---|---|---|
| worker -> server | `hello {name, hostname, version, sessions}` | once per connection; `sessions` re-adopts live runs |
| worker -> server | `started {task_id, cwd, baselines}` / `refused {task_id, reason}` | answer to `start` |
| worker -> server | `output {task_id, data}`, `exit {task_id, code}` | the PTY |
| worker -> server | `transcript {task_id, session_id, offset, data}` | transcript bytes from `offset` (0 = full) |
| worker -> server | `diff {task_id, diff}` | the run's Diff view |
| server -> worker | `start {spec}` | everything to spawn the run (`claude_runner.spawn_spec`) |
| server -> worker | `input`, `resize`, `stop`, `upload` | the browser terminal's actions |

## Not available on a team server yet

Git worktree runs, picking up terminal sessions (`/task` in your own shell, `ntasker adopt`), the queue planner, the
inbox triage (it needs an agent on the server), the usage-limit pause for remote runs, directory locks (the server
cannot see the workers' directories -- each user's lanes are their own instead), and the workspace / voice /
task-context file pickers, which read the server's disk.

## Security notes

* Passwords are stored as scrypt hashes; session cookies and access keys only as SHA-256 digests.
* Access keys carry the full authority of their user -- an agent session has its user's key in its environment, just
  like it has that user's agent credentials. Revoke a key under *Settings -> Team* (or `ntasker token revoke`).
* Sign-in is rate-limited (10 attempts per minute and client address).
* The terminal websocket checks sign-in and project membership before it is accepted; only the run's owner can type.
