"""Remote mode of the task CLI: the same commands against a team server.

A machine is in remote mode when it holds a bearer token -- ``NTASKER_TOKEN``
(every session a worker spawns has it) or the ``server_token`` setting (a team
member's own machine). Then ``list/show/add/patch/done/delete/report/in/
tag-add/tag-rm`` talk to the server's HTTP API instead of the local DB, which
on such a machine only holds the worker's settings.

Each handler mirrors its DB-direct twin in :mod:`ntasker.cli` and prints the
same lines, so agents and scripts see no difference.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from urllib.parse import urlencode

from ntasker import cli
from ntasker.i18n import _

COMMANDS = {"list", "show", "add", "patch", "done", "delete", "report", "in", "tag-add", "tag-rm"}


def active() -> bool:
    """Whether this CLI run talks to a team server."""
    return bool(cli._api_token())


class _Failed(Exception):
    """A request failed; the message is already printed."""


def _call(args: argparse.Namespace, method: str, path: str, body: dict | None = None) -> dict | list:
    try:
        status, data = cli._api_call(cli._server_base(args), method, path, body, timeout=15.0)
    except OSError as exc:
        print(_("nTasker: server not reachable ({err})").format(err=exc), file=sys.stderr)
        raise _Failed from exc
    if status >= 400:
        detail = data.get("detail", status) if isinstance(data, dict) else status
        print(_("nTasker: {detail}").format(detail=detail), file=sys.stderr)
        raise _Failed
    return data


def _user_id(args: argparse.Namespace, name: str) -> int:
    for u in _call(args, "GET", "/api/users"):
        if u["username"].lower() == name.strip().lower():
            return u["id"]
    print(_("nTasker: no user {name!r}").format(name=name), file=sys.stderr)
    raise _Failed


def _no_worktree(args: argparse.Namespace) -> None:
    if getattr(args, "worktree", None) is not None:
        print(_("nTasker: --worktree is not available on a team server yet"), file=sys.stderr)
        raise _Failed


def _list(args):
    query = [("project", p) for p in args.project] + [("tag", t) for t in args.tag]
    query += [("phase", p) for p in args.phase] + [("priority", p) for p in args.priority]
    if args.status:
        query.append(("status", args.status))
    if args.archived is not None:
        query.append(("archived", "true" if args.archived else "false"))
    if args.search:
        query.append(("search", args.search))
    tasks = _call(args, "GET", "/api/tasks" + ("?" + urlencode(query) if query else ""))
    if args.json:
        cli._print_json(tasks)
    else:
        cli._print_tasks_table(tasks)


def _show(args):
    task = _call(args, "GET", f"/api/tasks/{args.task_id}")
    if args.json:
        cli._print_json(task)
    else:
        cli._print_task_detail(task)


def _add(args):
    _no_worktree(args)
    body = {
        "title": args.title or "",
        "project": args.project,
        "description": args.description,
        "phase": args.phase,
        "priority": args.priority,
        "agent": args.agent,
        "model": args.model,
        "tags": args.tag or [],
        "depends": cli._parse_depends(args.depends) or [],
        "locks": cli._parse_locks(args.locks),
        "draft": args.draft,
        "fasttrack": args.fasttrack,
        "fail_continue": args.fail_continue,
    }
    if args.assignee:
        body["assignee_id"] = _user_id(args, args.assignee)
    task = _call(args, "POST", "/api/tasks", body)
    print(_("#{id} created: {title}").format(id=task["id"], title=task["title"]))


def _patch(args):
    _no_worktree(args)
    body: dict = {}
    for key in ("title", "description", "phase", "priority", "status", "archived", "draft",
                "fasttrack", "fail_continue", "report"):
        if getattr(args, key) is not None:
            body[key] = getattr(args, key)
    if args.project is not None:
        body["project"] = args.project or None
    if args.agent is not None:
        body["agent"] = args.agent.strip() or None
    if args.model is not None:
        body["model"] = args.model.strip() or None
    if args.locks is not None:
        body["locks"] = cli._parse_locks(args.locks)
    if args.depends is not None:
        body["depends"] = cli._parse_depends(args.depends) or []
    if args.assignee:
        body["assignee_id"] = _user_id(args, args.assignee)
    if not body:
        print(_("nTasker: nothing to change (specify at least one field)"), file=sys.stderr)
        raise _Failed
    _call(args, "PATCH", f"/api/tasks/{args.task_id}", body)
    print(_("#{id} updated ({fields})").format(id=args.task_id, fields=", ".join(body)))


def _done(args):
    _call(args, "PATCH", f"/api/tasks/{args.task_id}", {"status": "done"})
    print(_("#{id} -> done").format(id=args.task_id))


def _delete(args):
    task = _call(args, "GET", f"/api/tasks/{args.task_id}")
    if not args.yes:
        prompt = _("nTasker: delete #{id} {title!r} (archived={archived})? [y/N] ").format(
            id=args.task_id, title=task["title"], archived=task["archived"]
        )
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"j", "ja", "y", "yes"}:
            print(_("nTasker: aborted."))
            raise _Failed
    _call(args, "DELETE", f"/api/tasks/{args.task_id}")
    print(_("#{id} deleted").format(id=args.task_id))


def _report(args):
    text = pathlib.Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    _call(args, "PATCH", f"/api/tasks/{args.task_id}", {"report": text})
    if text.strip():
        print(_("#{id} report written").format(id=args.task_id))
    else:
        print(_("#{id} report cleared").format(id=args.task_id))


def _inbox(args):
    text = args.text if args.text not in (None, "-") else sys.stdin.read()
    if not (text or "").strip():
        print(_("nTasker: nothing to store -- pass a note or pipe it in"), file=sys.stderr)
        raise _Failed
    row = _call(args, "POST", "/api/inbox", {"text": text.strip(), "source": "cli"})
    print(_("inbox #{id} stored -- the server triages it").format(id=row["id"]))


def _tag(args, add: bool):
    tag = args.tag.strip().lower()
    task = _call(args, "GET", f"/api/tasks/{args.task_id}")
    tags = list(task["tags"])
    if add:
        tags = list(dict.fromkeys([*tags, tag]))
    elif tag not in tags:
        print(_("nTasker: tag {tag!r} not present on #{id}").format(tag=tag, id=args.task_id),
              file=sys.stderr)
        raise _Failed
    else:
        tags.remove(tag)
    _call(args, "PATCH", f"/api/tasks/{args.task_id}", {"tags": tags})
    if add:
        print(_("#{id} tag +{tag}").format(id=args.task_id, tag=tag))
    else:
        print(_("#{id} tag -{tag}").format(id=args.task_id, tag=tag))


_HANDLERS = {
    "list": _list, "show": _show, "add": _add, "patch": _patch, "done": _done,
    "delete": _delete, "report": _report, "in": _inbox,
    "tag-add": lambda a: _tag(a, True), "tag-rm": lambda a: _tag(a, False),
}


def run(args: argparse.Namespace) -> int:
    """Run ``args.command`` against the team server; the CLI exit code."""
    try:
        _HANDLERS[args.command](args)
    except _Failed:
        return 1
    return 0
