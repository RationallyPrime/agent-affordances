"""v1 unix-socket protocol between ``afford`` and ``afford-sparkd``.

Pinned on purpose: a version the daemon does not speak is a loud protocol
error, never a silent downgrade to oneshot ``codex exec``. Isolation lives
in the conversation layer (fresh thread per request), not in this envelope.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DAEMON_PROTOCOL = 1
Transport = Literal["oneshot", "daemon"]
TransportMode = Literal["auto", "oneshot", "daemon"]
DaemonErrorKind = Literal["unavailable", "protocol", "timeout", "auth"]


class SparkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DaemonRequest(SparkModel):
    v: int = DAEMON_PROTOCOL
    id: str
    verb: str
    prompt: str
    workdir: str
    writable: bool = False
    output_schema: dict[str, Any]
    timeout_s: int = Field(default=300, ge=1)


class DaemonResponse(SparkModel):
    v: int = DAEMON_PROTOCOL
    id: str
    ok: bool
    raw: str | None = None
    model: str | None = None
    error: DaemonErrorKind | None = None
    message: str | None = None


def socket_path() -> Path:
    """Resolve at call time so tests can redirect via env / XDG_RUNTIME_DIR."""
    override = os.environ.get("AFFORD_SPARK_SOCKET")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "afford-sparkd" / "sparkd.sock"
    return Path.home() / ".local" / "state" / "afford-spark" / "sparkd.sock"


def transport_mode() -> TransportMode:
    raw = os.environ.get("AFFORD_SPARK_TRANSPORT", "auto").strip().lower()
    if raw not in {"auto", "oneshot", "daemon"}:
        raise ValueError(f"AFFORD_SPARK_TRANSPORT={raw!r} is not auto|oneshot|daemon")
    return raw  # type: ignore[return-value]


def resolve_transport() -> Transport:
    """``auto`` uses the daemon only when its socket already exists."""
    mode = transport_mode()
    if mode == "oneshot":
        return "oneshot"
    if mode == "daemon":
        return "daemon"
    return "daemon" if socket_path().exists() else "oneshot"
