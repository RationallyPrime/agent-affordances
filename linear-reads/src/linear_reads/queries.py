"""GraphQL document builders.

Selection sets derive from the requested field names, so the API only ever
returns what will be printed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


class UnknownFieldError(ValueError):
    def __init__(self, name: str, allowed: Sequence[str]) -> None:
        super().__init__(f"unknown field {name!r}; valid fields: {", ".join(sorted(allowed))}")


@dataclass(frozen=True)
class FieldSpec:
    selection: str


ISSUE_FIELDS: dict[str, FieldSpec] = {
    "id": FieldSpec("identifier"),
    "uuid": FieldSpec("id"),
    "title": FieldSpec("title"),
    "state": FieldSpec("state { name }"),
    "assignee": FieldSpec("assignee { displayName }"),
    "labels": FieldSpec("labels { nodes { name } }"),
    "project": FieldSpec("project { name }"),
    "team": FieldSpec("team { key }"),
    "priority": FieldSpec("priorityLabel"),
    "estimate": FieldSpec("estimate"),
    "created": FieldSpec("createdAt"),
    "updated": FieldSpec("updatedAt"),
    "url": FieldSpec("url"),
    "body": FieldSpec("description"),
}

COMMENT_SELECTION = "createdAt user { displayName } body"
PAGE_INFO = "pageInfo { hasNextPage endCursor }"

TEAMS_QUERY = "query { teams { nodes { key name } } }"
STATES_QUERY = (
    "query($filter: WorkflowStateFilter) "
    "{ workflowStates(filter: $filter) { nodes { name type team { key } } } }"
)
LABELS_QUERY = (
    "query($filter: IssueLabelFilter) "
    "{ issueLabels(filter: $filter) { nodes { name team { key } } } }"
)
PROJECTS_QUERY = "query { projects { nodes { name state } } }"
USERS_QUERY = "query { users { nodes { displayName name active } } }"


def build_selection(fields: Sequence[str], allowed: dict[str, FieldSpec]) -> str:
    parts: list[str] = []
    for name in fields:
        spec = allowed.get(name)
        if spec is None:
            raise UnknownFieldError(name, list(allowed))
        if spec.selection not in parts:
            parts.append(spec.selection)
    return " ".join(parts)


def issue_query(fields: Sequence[str]) -> str:
    selection = build_selection(fields, ISSUE_FIELDS)
    return f"query($id: String!) {{ issue(id: $id) {{ {selection} }} }}"


def issues_query(fields: Sequence[str]) -> str:
    selection = build_selection(fields, ISSUE_FIELDS)
    return (
        "query($filter: IssueFilter, $first: Int!, $after: String) "
        "{ issues(filter: $filter, first: $first, after: $after, orderBy: updatedAt) "
        f"{{ nodes {{ {selection} }} {PAGE_INFO} }} }}"
    )


def comments_query() -> str:
    return (
        "query($id: String!, $first: Int!, $after: String) "
        "{ issue(id: $id) { comments(first: $first, after: $after) "
        f"{{ nodes {{ {COMMENT_SELECTION} }} {PAGE_INFO} }} }} }}"
    )
