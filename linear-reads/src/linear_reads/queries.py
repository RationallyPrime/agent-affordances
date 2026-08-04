"""GraphQL document builders.

Selection sets derive from the requested field names, so the API only ever
returns what will be printed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


class FieldSelectionError(ValueError):
    """Base class for invalid ``--fields`` selections."""


class EmptyFieldSelectionError(FieldSelectionError):
    def __init__(self) -> None:
        super().__init__("at least one field is required")


class UnknownFieldError(FieldSelectionError):
    def __init__(self, name: str, allowed: Sequence[str]) -> None:
        super().__init__(f"unknown field {name!r}; valid fields: {', '.join(sorted(allowed))}")


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

TEAMS_QUERY = (
    "query($first: Int!, $after: String) "
    f"{{ teams(first: $first, after: $after) {{ nodes {{ key name }} {PAGE_INFO} }} }}"
)
STATES_QUERY = (
    "query($filter: WorkflowStateFilter, $first: Int!, $after: String) "
    "{ workflowStates(filter: $filter, first: $first, after: $after) "
    f"{{ nodes {{ name type team {{ key }} }} {PAGE_INFO} }} }}"
)
LABELS_QUERY = (
    "query($filter: IssueLabelFilter, $first: Int!, $after: String) "
    "{ issueLabels(filter: $filter, first: $first, after: $after) "
    f"{{ nodes {{ name team {{ key }} }} {PAGE_INFO} }} }}"
)
PROJECTS_QUERY = (
    "query($first: Int!, $after: String) "
    f"{{ projects(first: $first, after: $after) {{ nodes {{ name state }} {PAGE_INFO} }} }}"
)
USERS_QUERY = (
    "query($first: Int!, $after: String) "
    "{ users(first: $first, after: $after) "
    f"{{ nodes {{ displayName name active }} {PAGE_INFO} }} }}"
)


def build_selection(fields: Sequence[str], allowed: dict[str, FieldSpec]) -> str:
    if not fields:
        raise EmptyFieldSelectionError
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
