from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from linear_reads import cli
from linear_reads.client import LinearClient

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
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LINEAR_TEAM", raising=False)


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
