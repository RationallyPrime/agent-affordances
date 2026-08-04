from __future__ import annotations

from linear_reads.render import (
    OutputFormat,
    render_comments_text,
    render_issue_detail,
    render_rows,
)

ROWS = [
    {"id": "KRA-1", "state": "In Progress", "assignee": "gnomon", "title": "Fix dedupe"},
    {"id": "KRA-2", "state": "Todo", "assignee": None, "title": "Token audit"},
]


def test_table_aligns_columns_and_dashes_empty() -> None:
    output = render_rows(ROWS, OutputFormat.table, footer="2 issues")
    assert output.splitlines() == [
        "KRA-1  In Progress  gnomon  Fix dedupe",
        "KRA-2  Todo         -       Token audit",
        "2 issues",
    ]


def test_table_joins_lists() -> None:
    output = render_rows([{"id": "KRA-3", "labels": ["bug", "infra"]}], OutputFormat.table)
    assert output == "KRA-3  bug,infra"


def test_empty_table_still_prints_footer() -> None:
    assert render_rows([], OutputFormat.table, footer="0 issues") == "0 issues"


def test_jsonl_is_compact_and_preserves_null() -> None:
    output = render_rows(ROWS, OutputFormat.jsonl, footer="ignored")
    lines = output.splitlines()
    assert len(lines) == 2
    assert lines[1] == '{"id":"KRA-2","state":"Todo","assignee":null,"title":"Token audit"}'


def test_json_is_a_compact_array() -> None:
    output = render_rows(ROWS, OutputFormat.json)
    assert output.startswith('[{"id":"KRA-1"')
    assert "\n" not in output


def test_issue_detail_block() -> None:
    flat = {
        "id": "KRA-9",
        "title": "Fix wake dedupe race",
        "state": "In Progress",
        "assignee": "gnomon",
        "labels": ["bug", "infra"],
        "body": "Steps:\n1. wake twice",
    }
    output = render_issue_detail(flat)
    assert output.splitlines() == [
        "KRA-9  In Progress  gnomon  bug,infra",
        "Fix wake dedupe race",
        "",
        "Steps:",
        "1. wake twice",
    ]


def test_issue_detail_without_body() -> None:
    output = render_issue_detail({"id": "KRA-9", "title": "T", "state": "Todo"})
    assert output == "KRA-9  Todo\nT"


def test_comments_text() -> None:
    rows = [
        {"created": "2026-08-01T10:00:00.000Z", "author": "hakon", "body": "ack"},
        {"created": "2026-08-01T11:00:00.000Z", "author": None, "body": "done"},
    ]
    output = render_comments_text(rows)
    assert output.splitlines() == [
        "2026-08-01T10:00:00.000Z  hakon:",
        "ack",
        "",
        "2026-08-01T11:00:00.000Z  -:",
        "done",
    ]
