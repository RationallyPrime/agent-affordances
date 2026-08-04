"""Thin httpx client for the Linear GraphQL API."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

API_URL = "https://api.linear.app/graphql"
_MAX_PAGE = 100


class LinearError(Exception):
    """Base for all linear-reads errors."""


class MissingAPIKeyError(LinearError):
    pass


class NotFoundError(LinearError):
    pass


class LinearAPIError(LinearError):
    pass


class LinearClient:
    def __init__(
        self,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("LINEAR_API_KEY")
        if not key:
            raise MissingAPIKeyError("LINEAR_API_KEY is not set")
        self._http = httpx.Client(
            headers={"Authorization": key, "Content-Type": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def query(self, document: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self._http.post(
            API_URL, json={"query": document, "variables": dict(variables or {})}
        )
        if response.status_code != 200:
            raise LinearAPIError(f"HTTP {response.status_code} from Linear: {response.text[:300]}")
        payload = response.json()
        if payload.get("errors"):
            messages = "; ".join(str(err.get("message", err)) for err in payload["errors"])
            raise LinearAPIError(messages)
        data = payload.get("data")
        if data is None:
            raise LinearAPIError("response had no data")
        return data

    def paginate(
        self,
        document: str,
        variables: Mapping[str, Any],
        connection_path: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Collect up to ``limit`` nodes from a connection, following cursors."""
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(nodes) < limit:
            first = min(limit - len(nodes), _MAX_PAGE)
            data = self.query(document, {**variables, "first": first, "after": cursor})
            connection = _dig(data, connection_path)
            page = connection["nodes"]
            nodes.extend(page)
            page_info = connection.get("pageInfo") or {}
            cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not cursor or not page:
                break
        return nodes[:limit]


def _dig(data: dict[str, Any], path: str) -> dict[str, Any]:
    node: Any = data
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            raise NotFoundError(f"{part} not found")
    return node
