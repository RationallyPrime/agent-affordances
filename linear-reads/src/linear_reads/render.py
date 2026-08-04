"""Compact renderers: aligned plain-text tables, JSONL, JSON.

No box-drawing, no colors, no header rows: output is meant to be cheap for
an agent to read back.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import Enum
from typing import Any


class OutputFormat(str, Enum):
    table = "table"
    jsonl = "jsonl"
    json = "json"


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _cell(value: Any) -> str:
    if value is None or value == []:
        return "-"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def render_rows(
    rows: Sequence[dict[str, Any]],
    fmt: OutputFormat,
    footer: str | None = None,
) -> str:
    if fmt is OutputFormat.jsonl:
        return "\n".join(compact_json(row) for row in rows)
    if fmt is OutputFormat.json:
        return compact_json(list(rows))
    return _table(rows, footer)


def _table(rows: Sequence[dict[str, Any]], footer: str | None) -> str:
    lines: list[str] = []
    if rows:
        columns = list(rows[0])
        cells = [[_cell(row.get(column)) for column in columns] for row in rows]
        widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
        for row in cells:
            padded = [value.ljust(widths[i]) for i, value in enumerate(row[:-1])]
            padded.append(row[-1])
            lines.append("  ".join(padded).rstrip())
    if footer is not None:
        lines.append(footer)
    return "\n".join(lines)


def render_issue_detail(flat: dict[str, Any]) -> str:
    """One issue as a terse header line, title, then the raw markdown body."""
    header_keys = (
        "id",
        "state",
        "assignee",
        "labels",
        "priority",
        "estimate",
        "project",
        "team",
        "created",
        "updated",
        "url",
        "uuid",
    )
    header = [_cell(flat[key]) for key in header_keys if key in flat]
    lines: list[str] = []
    if header:
        lines.append("  ".join(header))
    if flat.get("title"):
        lines.append(str(flat["title"]))
    body = flat.get("body")
    if body:
        lines.extend(["", str(body)])
    return "\n".join(lines)


def render_comments_text(comments: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for comment in comments:
        lines.append(f"{_cell(comment.get("created"))}  {_cell(comment.get("author"))}:")
        lines.append(str(comment.get("body") or ""))
        lines.append("")
    return "\n".join(lines).rstrip()
