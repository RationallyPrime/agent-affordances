from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from linear_reads.cli import app, parse_since

NODE_A = {
    "identifier": "KRA-1",
    "state": {"name": "In Progress"},
    "assignee": {"displayName": "gnomon"},
    "title": "Fix wake dedupe race",
}
NODE_B = {
    "identifier": "KRA-2",
    "state": {"name": "Todo"},
    "assignee": None,
    "title": "Token audit",
}
ISSUE_NODE = {
    "identifier": "KRA-9",
    "title": "Fix wake dedupe race",
    "state": {"name": "In Progress"},
    "assignee": {"displayName": "gnomon"},
    "labels": {"nodes": [{"name": "bug"}, {"name": "infra"}]},
    "description": "Steps:\n1. wake twice",
}
COMMENT_NODE = {
    "createdAt": "2026-08-01T10:00:00.000Z",
    "user": {"displayName": "hakon"},
    "body": "ack",
}


def issues_page(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {
        "data": {
            "issues": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
            }
        }
    }


def comments_page(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "issue": {
                "comments": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }


# --- issues ---


def test_issues_defaults_to_jsonl_when_piped(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([NODE_A, NODE_B]))
    result = runner.invoke(app, ["issues"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert json.loads(lines[0]) == {
        "id": "KRA-1",
        "state": "In Progress",
        "assignee": "gnomon",
        "title": "Fix wake dedupe race",
    }
    assert json.loads(lines[1])["assignee"] is None
    assert "identifier" in fake.requests[0]["query"]
    assert "state { name }" in fake.requests[0]["query"]


def test_issues_table_format(runner, fake_linear) -> None:
    fake_linear(lambda payload: issues_page([NODE_A, NODE_B]))
    result = runner.invoke(app, ["issues", "--format", "table"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "KRA-1  In Progress  gnomon  Fix wake dedupe race",
        "KRA-2  Todo         -       Token audit",
        "2 issues",
    ]


def test_issues_fields_drive_the_selection_set(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([{"identifier": "KRA-1", "estimate": 3}]))
    result = runner.invoke(app, ["issues", "--fields", "id,estimate"])
    assert result.exit_code == 0
    query = fake.requests[0]["query"]
    assert "estimate" in query
    assert "title" not in query
    assert json.loads(result.output.splitlines()[0]) == {"id": "KRA-1", "estimate": 3}


def test_issues_team_defaults_from_env(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([]))
    result = runner.invoke(app, ["issues"], env={"LINEAR_TEAM": "KRA"})
    assert result.exit_code == 0
    assert fake.requests[0]["variables"]["filter"]["team"] == {"key": {"eq": "KRA"}}


def test_issues_team_flag_overrides_env(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([]))
    result = runner.invoke(app, ["issues", "--team", "OPS"], env={"LINEAR_TEAM": "KRA"})
    assert result.exit_code == 0
    assert fake.requests[0]["variables"]["filter"]["team"] == {"key": {"eq": "OPS"}}


def test_issues_no_filters_sends_null_filter(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([]))
    result = runner.invoke(app, ["issues"])
    assert result.exit_code == 0
    assert fake.requests[0]["variables"]["filter"] is None


def test_issues_assignee_me_uses_is_me(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([]))
    result = runner.invoke(app, ["issues", "--assignee", "me", "--state", "started"])
    assert result.exit_code == 0
    filter_ = fake.requests[0]["variables"]["filter"]
    assert filter_["assignee"] == {"isMe": {"eq": True}}
    assert filter_["state"] == {"name": {"eqIgnoreCase": "started"}}


def test_issues_updated_since_becomes_absolute_timestamp(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([]))
    result = runner.invoke(app, ["issues", "--updated-since", "7d"])
    assert result.exit_code == 0
    gte = fake.requests[0]["variables"]["filter"]["updatedAt"]["gte"]
    moment = datetime.fromisoformat(gte)
    expected = datetime.now(UTC) - timedelta(days=7)
    assert abs((moment - expected).total_seconds()) < 10


def test_issues_paginates_until_limit(runner, fake_linear) -> None:
    def responder(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["variables"].get("after") is None:
            return issues_page([NODE_A, NODE_B], cursor="c1")
        return issues_page([dict(NODE_A, identifier="KRA-3")])

    fake = fake_linear(responder)
    result = runner.invoke(app, ["issues", "--limit", "3"])
    assert result.exit_code == 0
    assert len(result.output.splitlines()) == 3
    assert fake.requests[0]["variables"]["first"] == 3
    assert fake.requests[1]["variables"]["after"] == "c1"
    assert fake.requests[1]["variables"]["first"] == 1


def test_issues_unknown_field_fails_before_any_request(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: issues_page([]))
    result = runner.invoke(app, ["issues", "--fields", "id,bogus"])
    assert result.exit_code == 2
    assert "unknown field 'bogus'" in result.stderr
    assert "valid fields:" in result.stderr
    assert fake.requests == []


def test_missing_api_key_is_a_one_line_error(runner) -> None:
    result = runner.invoke(app, ["issues"])
    assert result.exit_code == 2
    assert "LINEAR_API_KEY" in result.stderr


def test_graphql_errors_surface(runner, fake_linear) -> None:
    fake_linear(lambda payload: {"errors": [{"message": "boom"}]})
    result = runner.invoke(app, ["issues"])
    assert result.exit_code == 2
    assert "boom" in result.stderr


# --- issue ---


def test_issue_detail_table(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: {"data": {"issue": ISSUE_NODE}})
    result = runner.invoke(app, ["issue", "KRA-9", "--format", "table"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "KRA-9  In Progress  gnomon  bug,infra"
    assert lines[1] == "Fix wake dedupe race"
    assert "1. wake twice" in result.output
    assert "description" in fake.requests[0]["query"]


def test_issue_no_body_skips_description_in_query(runner, fake_linear) -> None:
    fake = fake_linear(lambda payload: {"data": {"issue": ISSUE_NODE}})
    result = runner.invoke(app, ["issue", "KRA-9", "--no-body", "--format", "table"])
    assert result.exit_code == 0
    assert "description" not in fake.requests[0]["query"]
    assert "wake twice" not in result.output


def test_issue_jsonl_is_one_object(runner, fake_linear) -> None:
    fake_linear(lambda payload: {"data": {"issue": ISSUE_NODE}})
    result = runner.invoke(app, ["issue", "KRA-9"])
    assert result.exit_code == 0
    flat = json.loads(result.output)
    assert flat["id"] == "KRA-9"
    assert flat["labels"] == ["bug", "infra"]
    assert flat["body"] == "Steps:\n1. wake twice"


def test_issue_not_found(runner, fake_linear) -> None:
    fake_linear(lambda payload: {"data": {"issue": None}})
    result = runner.invoke(app, ["issue", "KRA-999"])
    assert result.exit_code == 2
    assert "not found" in result.stderr


def test_issue_with_comments_appends_thread(runner, fake_linear) -> None:
    def responder(payload: dict[str, Any]) -> dict[str, Any]:
        if "comments(" in payload["query"]:
            return comments_page([COMMENT_NODE])
        return {"data": {"issue": ISSUE_NODE}}

    fake = fake_linear(responder)
    result = runner.invoke(app, ["issue", "KRA-9", "--comments", "--format", "table"])
    assert result.exit_code == 0
    assert "--- comments (1)" in result.output
    assert "hakon:" in result.output
    assert "ack" in result.output
    assert len(fake.requests) == 2


# --- comments ---


def test_comments_jsonl(runner, fake_linear) -> None:
    fake_linear(lambda payload: comments_page([COMMENT_NODE]))
    result = runner.invoke(app, ["comments", "KRA-9"])
    assert result.exit_code == 0
    assert json.loads(result.output.splitlines()[0]) == {
        "created": "2026-08-01T10:00:00.000Z",
        "author": "hakon",
        "body": "ack",
    }


# --- metadata ---


def test_teams_table(runner, fake_linear) -> None:
    nodes = [{"key": "KRA", "name": "Krakkar"}]
    fake_linear(lambda payload: {"data": {"teams": {"nodes": nodes}}})
    result = runner.invoke(app, ["teams", "--format", "table"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["KRA  Krakkar", "1 teams"]


def test_states_team_defaults_from_env(runner, fake_linear) -> None:
    nodes = [{"name": "In Progress", "type": "started", "team": {"key": "KRA"}}]
    fake = fake_linear(lambda payload: {"data": {"workflowStates": {"nodes": nodes}}})
    result = runner.invoke(app, ["states"], env={"LINEAR_TEAM": "KRA"})
    assert result.exit_code == 0
    assert fake.requests[0]["variables"]["filter"] == {"team": {"key": {"eq": "KRA"}}}
    assert json.loads(result.output.splitlines()[0]) == {
        "name": "In Progress",
        "type": "started",
        "team": "KRA",
    }


def test_labels_jsonl(runner, fake_linear) -> None:
    nodes = [{"name": "bug", "team": None}]
    fake_linear(lambda payload: {"data": {"issueLabels": {"nodes": nodes}}})
    result = runner.invoke(app, ["labels"])
    assert result.exit_code == 0
    assert json.loads(result.output.splitlines()[0]) == {"name": "bug", "team": None}


# --- parse_since ---


def test_parse_since_durations() -> None:
    got = datetime.fromisoformat(parse_since("7d"))
    expected = datetime.now(UTC) - timedelta(days=7)
    assert abs((got - expected).total_seconds()) < 10


def test_parse_since_passes_iso_through() -> None:
    assert parse_since("2026-08-01") == "2026-08-01"


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="cannot parse"):
        parse_since("soonish")
