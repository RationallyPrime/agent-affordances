from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from linear_reads import cli
from linear_reads.client import PROFILE_DIR_ENVS, LinearClient

Responder = Callable[[dict[str, Any]], dict[str, Any]]


class FakeLinear:
    """Serves canned GraphQL responses and records every request payload."""

    def __init__(self, responder: Responder) -> None:
        self.requests: list[dict[str, Any]] = []
        self._responder = responder

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        return httpx.Response(200, json=self._responder(payload))

    def client(self) -> LinearClient:
        return LinearClient(api_key="test-key", transport=httpx.MockTransport(self._handle))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No key from the developer's own environment or profile reaches a test.

    The key can come from a file under the home or profile directory, so HOME
    is pointed at an empty temp dir and the profile pins are cleared; a test
    that wants a key file builds one under ``tmp_path``.
    """
    for var in ("LINEAR_API_KEY", "LINEAR_API_KEY_FILE", "LINEAR_TEAM", *PROFILE_DIR_ENVS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def fake_linear(monkeypatch: pytest.MonkeyPatch) -> Callable[[Responder], FakeLinear]:
    def install(responder: Responder) -> FakeLinear:
        fake = FakeLinear(responder)
        monkeypatch.setattr(cli, "_make_client", fake.client)
        return fake

    return install


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()
