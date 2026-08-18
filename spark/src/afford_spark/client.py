"""Thin unix-socket client for ``afford-sparkd``."""

from __future__ import annotations

import socket
from pathlib import Path

from afford_spark.protocol import DAEMON_PROTOCOL, DaemonRequest, DaemonResponse


class DaemonConnectError(OSError):
    """Socket missing, refused, or not a socket.

    Forced ``daemon`` transport fails loud as ``SparkUnavailableError``.
    ``auto`` must not reach this: ``resolve_transport`` treats an
    unconnectable socket as oneshot.
    """


def request_daemon(req: DaemonRequest, path: Path) -> DaemonResponse:
    """Send one v1 request and wait for one v1 response.

    The client timeout is the request's ``timeout_s`` plus a small grace so
    the daemon can return a typed timeout error instead of dropping the
    connection. A framed line that is not v1 is a protocol error, not a
    retry.
    """
    timeout_s = req.timeout_s + 2
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        raise DaemonConnectError(f"cannot create unix socket: {exc}") from exc
    try:
        sock.settimeout(timeout_s)
        try:
            sock.connect(str(path))
        except OSError as exc:
            raise DaemonConnectError(f"afford-sparkd not reachable at {path}: {exc}") from exc
        payload = req.model_dump_json().encode() + b"\n"
        sock.sendall(payload)
        line = _recv_line(sock, timeout_s)
    finally:
        sock.close()
    try:
        response = DaemonResponse.model_validate_json(line)
    except Exception as exc:
        raise ProtocolPinError(f"daemon returned an unreadable frame: {line[:200]!r}") from exc
    if response.v != DAEMON_PROTOCOL:
        raise ProtocolPinError(f"daemon protocol {response.v} is not pinned v{DAEMON_PROTOCOL}")
    if response.id != req.id:
        raise ProtocolPinError(
            f"daemon response id {response.id!r} does not match request {req.id!r}"
        )
    return response


def _recv_line(sock: socket.socket, timeout_s: int) -> str:
    chunks: list[bytes] = []
    sock.settimeout(timeout_s)
    while True:
        try:
            piece = sock.recv(65536)
        except TimeoutError as exc:
            raise TimeoutError(f"timed out waiting for afford-sparkd after {timeout_s}s") from exc
        if not piece:
            raise ConnectionError("afford-sparkd closed the connection before a response")
        chunks.append(piece)
        if b"\n" in piece:
            break
    return b"".join(chunks).split(b"\n", 1)[0].decode()


class ProtocolPinError(RuntimeError):
    """The on-wire frame is not the pinned v1 protocol."""
