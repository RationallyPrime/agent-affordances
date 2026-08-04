from __future__ import annotations

import pytest

from linear_reads import queries


def test_build_selection_maps_and_dedupes() -> None:
    selection = queries.build_selection(["id", "state", "id"], queries.ISSUE_FIELDS)
    assert selection == "identifier state { name }"


def test_build_selection_rejects_unknown_field() -> None:
    with pytest.raises(queries.UnknownFieldError, match="unknown field 'bogus'") as excinfo:
        queries.build_selection(["id", "bogus"], queries.ISSUE_FIELDS)
    assert "valid fields:" in str(excinfo.value)
    assert "title" in str(excinfo.value)


def test_build_selection_rejects_an_empty_field_list() -> None:
    with pytest.raises(queries.EmptyFieldSelectionError, match="at least one field"):
        queries.build_selection([], queries.ISSUE_FIELDS)


def test_issue_query_selects_only_requested_fields() -> None:
    document = queries.issue_query(["id", "body"])
    assert "issue(id: $id)" in document
    assert "identifier description" in document
    assert "title" not in document


def test_issues_query_paginates_and_orders() -> None:
    document = queries.issues_query(["id", "title"])
    assert "orderBy: updatedAt" in document
    assert "pageInfo { hasNextPage endCursor }" in document
    assert "nodes { identifier title }" in document


def test_comments_query_shape() -> None:
    document = queries.comments_query()
    assert "issue(id: $id)" in document
    assert "comments(first: $first, after: $after)" in document
    assert "user { displayName }" in document


@pytest.mark.parametrize(
    "document,connection",
    [
        (queries.TEAMS_QUERY, "teams"),
        (queries.STATES_QUERY, "workflowStates"),
        (queries.LABELS_QUERY, "issueLabels"),
        (queries.PROJECTS_QUERY, "projects"),
        (queries.USERS_QUERY, "users"),
    ],
)
def test_metadata_queries_are_cursor_paginated(document: str, connection: str) -> None:
    assert "$first: Int!" in document
    assert "$after: String" in document
    assert f"{connection}(" in document
    assert "pageInfo { hasNextPage endCursor }" in document
