"""Thin httpx client for the Linear GraphQL API."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.linear.app/graphql"
_MAX_PAGE = 100

KEY_ENV = "LINEAR_API_KEY"
KEY_FILE_ENV = "LINEAR_API_KEY_FILE"
# The Weave profile-secrets convention: `<profile>/secrets/<name>`, owner-only.
# The profile dir is whatever the seat's harness pins — CLAUDE_CONFIG_DIR for
# Claude Code, CODEX_HOME for Codex — and `~/.claude` where nothing is pinned.
KEY_FILE_RELATIVE = Path("secrets") / "linear_api_key"
PROFILE_DIR_ENVS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
_LOOSE_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO


class LinearError(Exception):
    """Base for all linear-reads errors."""


class MissingAPIKeyError(LinearError):
    pass


class NotFoundError(LinearError):
    pass


class LinearAPIError(LinearError):
    pass


def key_file_candidates(environ: Mapping[str, str], home: Path) -> list[Path]:
    """Where a key file may live, most specific first, without duplicates."""
    candidates: list[Path] = []
    explicit = environ.get(KEY_FILE_ENV)
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for var in PROFILE_DIR_ENVS:
        profile = environ.get(var)
        if profile:
            candidates.append(Path(profile).expanduser() / KEY_FILE_RELATIVE)
    candidates.append(home / ".claude" / KEY_FILE_RELATIVE)
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def resolve_api_key(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str:
    """The API key from ``LINEAR_API_KEY``, else from the first key file found.

    An explicit ``LINEAR_API_KEY_FILE`` that does not exist is an error, not a
    fallthrough: a stated path that is wrong should say so. A key file readable
    by group or world is refused rather than used — the file is a real secret
    and 600 is the contract. Every refusal is a ``MissingAPIKeyError``, which is
    what the CLI translates into its actionable exit 2.
    """
    env = os.environ if environ is None else environ
    key = env.get(KEY_ENV)
    if key:
        return key
    home_dir = Path.home() if home is None else home
    candidates = key_file_candidates(env, home_dir)
    explicit = env.get(KEY_FILE_ENV)
    for path in candidates:
        if not path.is_file():
            if explicit and path == Path(explicit).expanduser():
                raise MissingAPIKeyError(f"{KEY_FILE_ENV}={path} does not exist")
            continue
        try:
            mode = path.stat().st_mode
            if mode & _LOOSE_MODE_BITS:
                raise MissingAPIKeyError(
                    f"{path} is {stat.filemode(mode)}; a Linear key file must be readable by its "
                    "owner only (chmod 600)"
                )
            key = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            # Mode 600 is the ceiling, not the floor: 000 and 200 also carry no
            # group or world bits, so the check above passes and the read raises
            # PermissionError. Bytes that are not UTF-8 raise too. Both are
            # configuration mistakes, and only a LinearError reaches the CLI's
            # actionable exit 2.
            raise MissingAPIKeyError(f"{path} cannot be read: {exc}") from exc
        if not key:
            raise MissingAPIKeyError(f"{path} is empty")
        return key
    looked = ", ".join(str(path) for path in candidates)
    raise MissingAPIKeyError(f"{KEY_ENV} is not set and no key file exists (looked at: {looked})")


class LinearClient:
    def __init__(
        self,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or resolve_api_key()
        self._http = httpx.Client(
            headers={"Authorization": key, "Content-Type": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def query(self, document: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._http.post(
                API_URL, json={"query": document, "variables": dict(variables or {})}
            )
        except httpx.RequestError as exc:
            raise LinearAPIError(f"request to Linear failed: {exc}") from exc
        if response.status_code != 200:
            raise LinearAPIError(f"HTTP {response.status_code} from Linear: {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LinearAPIError("Linear returned an invalid JSON response") from exc
        if not isinstance(payload, dict):
            raise LinearAPIError("Linear returned a non-object JSON response")
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
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """Collect up to ``limit`` nodes, or the complete connection when it is ``None``."""
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while limit is None or len(nodes) < limit:
            first = _MAX_PAGE if limit is None else min(limit - len(nodes), _MAX_PAGE)
            data = self.query(document, {**variables, "first": first, "after": cursor})
            connection = _dig(data, connection_path)
            page = connection["nodes"]
            nodes.extend(page)
            page_info = connection.get("pageInfo") or {}
            next_cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not next_cursor or not page:
                break
            if next_cursor in seen_cursors:
                raise LinearAPIError("Linear pagination returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return nodes if limit is None else nodes[:limit]


def _dig(data: dict[str, Any], path: str) -> dict[str, Any]:
    node: Any = data
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            raise NotFoundError(f"{part} not found")
    return node
