"""linear-reads: token-lean read-only Linear CLI for agents.

Reads only; writes stay in the Linear MCP. Auth via the LINEAR_API_KEY env
var, else the profile key file `<profile>/secrets/linear_api_key` (see
`client.resolve_api_key`); LINEAR_TEAM sets the default team for `issues`
and `states`.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import typer

from . import queries
from .client import LinearClient, LinearError
from .models import Comment, Issue
from .render import (
    OutputFormat,
    compact_json,
    render_comments_text,
    render_issue_detail,
    render_rows,
)

app = typer.Typer(
    help="Token-lean read-only Linear CLI for agents. Reads only; writes stay in the MCP.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_LIST_FIELDS = "id,state,assignee,title"
DEFAULT_ISSUE_FIELDS = "id,title,state,assignee,labels"

FormatOpt = Annotated[
    OutputFormat | None,
    typer.Option("--format", help="table|jsonl|json (default: table on a TTY, jsonl when piped)"),
]
FieldsHelp = "Comma-separated fields; maps 1:1 onto the GraphQL selection set"


def _make_client() -> LinearClient:
    return LinearClient()


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


def _resolve_format(fmt: OutputFormat | None) -> OutputFormat:
    if fmt is not None:
        return fmt
    return OutputFormat.table if sys.stdout.isatty() else OutputFormat.jsonl


def _split_fields(raw: str) -> list[str]:
    return [field.strip() for field in raw.split(",") if field.strip()]


def parse_since(text: str) -> str:
    """Parse '30m' / '12h' / '7d' / '2w' into an ISO-8601 UTC timestamp; pass ISO through."""
    match = re.fullmatch(r"(\d+)([mhdw])", text)
    if match:
        unit = {
            "m": timedelta(minutes=1),
            "h": timedelta(hours=1),
            "d": timedelta(days=1),
            "w": timedelta(weeks=1),
        }[match.group(2)]
        moment = datetime.now(UTC) - int(match.group(1)) * unit
        return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cannot parse {text!r} as a duration (7d, 12h, 2w) or ISO date") from exc
    return text


def _echo(output: str) -> None:
    if output:
        typer.echo(output)


@app.command()
def issues(
    team: Annotated[
        str | None, typer.Option(envvar="LINEAR_TEAM", help="Team key (default: $LINEAR_TEAM)")
    ] = None,
    state: Annotated[str | None, typer.Option(help="Workflow state name")] = None,
    assignee: Annotated[str | None, typer.Option(help="Assignee display name, or 'me'")] = None,
    label: Annotated[str | None, typer.Option(help="Label name")] = None,
    project: Annotated[str | None, typer.Option(help="Project name (substring)")] = None,
    query: Annotated[str | None, typer.Option(help="Match in title or description")] = None,
    updated_since: Annotated[str | None, typer.Option(help="30m, 12h, 7d, 2w, or ISO date")] = None,
    limit: Annotated[int, typer.Option(min=1, help="Max issues to return")] = 50,
    fields: Annotated[str, typer.Option(help=FieldsHelp)] = DEFAULT_LIST_FIELDS,
    format: FormatOpt = None,
) -> None:
    """Sweep issues; filters combine with AND. Most recently updated first."""
    field_list = _split_fields(fields)
    try:
        document = queries.issues_query(field_list)
    except queries.FieldSelectionError as exc:
        raise _fail(str(exc)) from exc

    filter_: dict[str, Any] = {}
    if team:
        filter_["team"] = {"key": {"eq": team}}
    if state:
        filter_["state"] = {"name": {"eqIgnoreCase": state}}
    if assignee == "me":
        filter_["assignee"] = {"isMe": {"eq": True}}
    elif assignee:
        filter_["assignee"] = {"displayName": {"containsIgnoreCase": assignee}}
    if label:
        filter_["labels"] = {"some": {"name": {"eqIgnoreCase": label}}}
    if project:
        filter_["project"] = {"name": {"containsIgnoreCase": project}}
    if query:
        filter_["or"] = [
            {"title": {"containsIgnoreCase": query}},
            {"description": {"containsIgnoreCase": query}},
        ]
    if updated_since:
        try:
            filter_["updatedAt"] = {"gte": parse_since(updated_since)}
        except ValueError as exc:
            raise _fail(str(exc)) from exc

    try:
        client = _make_client()
        try:
            nodes = client.paginate(document, {"filter": filter_ or None}, "issues", limit)
        finally:
            client.close()
    except LinearError as exc:
        raise _fail(str(exc)) from exc

    rows = [Issue.model_validate(node).flat(field_list) for node in nodes]
    _echo(render_rows(rows, _resolve_format(format), footer=f"{len(rows)} issues"))


@app.command()
def issue(
    identifier: Annotated[str, typer.Argument(help="Issue id, e.g. KRA-123")],
    no_body: Annotated[bool, typer.Option("--no-body", help="Omit the body")] = False,
    comments: Annotated[bool, typer.Option("--comments", help="Append the comment thread")] = False,
    fields: Annotated[str | None, typer.Option(help=FieldsHelp)] = None,
    format: FormatOpt = None,
) -> None:
    """Show one issue: terse header, title, raw markdown body."""
    default = DEFAULT_ISSUE_FIELDS if no_body else DEFAULT_ISSUE_FIELDS + ",body"
    field_list = _split_fields(fields if fields is not None else default)
    if no_body and "body" in field_list:
        raise _fail("--no-body conflicts with 'body' in --fields")
    try:
        document = queries.issue_query(field_list)
    except queries.FieldSelectionError as exc:
        raise _fail(str(exc)) from exc

    try:
        client = _make_client()
        try:
            data = client.query(document, {"id": identifier})
            node = data.get("issue")
            if node is None:
                raise _fail(f"issue {identifier} not found")
            comment_nodes: list[dict[str, Any]] = []
            if comments:
                comment_nodes = client.paginate(
                    queries.comments_query(), {"id": identifier}, "issue.comments", None
                )
        finally:
            client.close()
    except LinearError as exc:
        raise _fail(str(exc)) from exc

    flat = Issue.model_validate(node).flat(field_list)
    comment_rows = [Comment.model_validate(item).flat() for item in comment_nodes]
    fmt = _resolve_format(format)
    if fmt is OutputFormat.table:
        output = render_issue_detail(flat)
        if comment_rows:
            thread = render_comments_text(comment_rows)
            output += f"\n\n--- comments ({len(comment_rows)})\n{thread}"
        typer.echo(output)
    else:
        if comment_rows:
            flat["comments"] = comment_rows
        typer.echo(compact_json(flat))


@app.command()
def comments(
    identifier: Annotated[str, typer.Argument(help="Issue id, e.g. KRA-123")],
    limit: Annotated[int, typer.Option(min=1, help="Max comments to return")] = 50,
    format: FormatOpt = None,
) -> None:
    """List an issue's comments, oldest first."""
    try:
        client = _make_client()
        try:
            nodes = client.paginate(
                queries.comments_query(),
                {"id": identifier},
                "issue.comments",
                limit,
            )
        finally:
            client.close()
    except LinearError as exc:
        raise _fail(str(exc)) from exc

    rows = [Comment.model_validate(node).flat() for node in nodes]
    fmt = _resolve_format(format)
    if fmt is OutputFormat.table:
        typer.echo(render_comments_text(rows) if rows else "0 comments")
    else:
        _echo(render_rows(rows, fmt))


def _run_meta(
    document: str,
    variables: dict[str, Any],
    connection: str,
    columns: dict[str, str],
    fmt: OutputFormat | None,
    noun: str,
) -> None:
    try:
        client = _make_client()
        try:
            nodes = client.paginate(document, variables, connection, None)
        finally:
            client.close()
    except LinearError as exc:
        raise _fail(str(exc)) from exc

    rows = [{key: _pluck(node, path) for key, path in columns.items()} for node in nodes]
    _echo(render_rows(rows, _resolve_format(fmt), footer=f"{len(rows)} {noun}"))


def _pluck(node: dict[str, Any], path: str) -> Any:
    """Fetch a possibly nested value: 'team.key' digs, 'name' reads directly."""
    value: Any = node
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


@app.command()
def teams(format: FormatOpt = None) -> None:
    """List teams (key, name)."""
    _run_meta(queries.TEAMS_QUERY, {}, "teams", {"key": "key", "name": "name"}, format, "teams")


@app.command()
def states(
    team: Annotated[
        str | None, typer.Option(envvar="LINEAR_TEAM", help="Team key (default: $LINEAR_TEAM)")
    ] = None,
    format: FormatOpt = None,
) -> None:
    """List workflow states, optionally scoped to one team."""
    filter_ = {"team": {"key": {"eq": team}}} if team else None
    _run_meta(
        queries.STATES_QUERY,
        {"filter": filter_},
        "workflowStates",
        {"name": "name", "type": "type", "team": "team.key"},
        format,
        "states",
    )


@app.command()
def labels(
    team: Annotated[str | None, typer.Option(help="Team key; omit for all labels")] = None,
    format: FormatOpt = None,
) -> None:
    """List issue labels (workspace and team)."""
    filter_ = {"team": {"key": {"eq": team}}} if team else None
    _run_meta(
        queries.LABELS_QUERY,
        {"filter": filter_},
        "issueLabels",
        {"name": "name", "team": "team.key"},
        format,
        "labels",
    )


@app.command()
def projects(format: FormatOpt = None) -> None:
    """List projects (name, state)."""
    _run_meta(
        queries.PROJECTS_QUERY,
        {},
        "projects",
        {"name": "name", "state": "state"},
        format,
        "projects",
    )


@app.command()
def users(format: FormatOpt = None) -> None:
    """List users (display name, full name, active)."""
    _run_meta(
        queries.USERS_QUERY,
        {},
        "users",
        {"name": "displayName", "full": "name", "active": "active"},
        format,
        "users",
    )
