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
