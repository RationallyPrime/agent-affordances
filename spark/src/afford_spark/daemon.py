"""``afford-sparkd`` — socket-activated warm Codex app-server.

One process, one app-server, one request at a time. Each request is a
fresh ephemeral conversation that is dropped after delivery. Auth expiry
and a protocol other than v1 fail loud.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import sys
import threading
import time
from pathlib import Path

from pydantic import ValidationError

from afford_spark.appserver import CodexAppServer
from afford_spark.engine import SPARK_MODEL, SparkProtocolError, SparkUnavailableError
from afford_spark.protocol import DAEMON_PROTOCOL, DaemonRequest, DaemonResponse, socket_path

SD_LISTEN_FDS_START = 3


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="afford-sparkd")
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Listen path (default: $AFFORD_SPARK_SOCKET or XDG runtime dir)",
    )
    parser.add_argument(
        "--systemd",
        action="store_true",
        help="Inherit the listening socket from systemd (LISTEN_FDS).",
    )
    parser.add_argument(
        "--codex",
        default=os.environ.get("AFFORD_SPARK_CODEX", "codex"),
        help="codex binary that provides app-server",
    )
    args = parser.parse_args(argv)
    try:
        serve(socket_file=args.socket, systemd=args.systemd, codex=args.codex)
    except SparkUnavailableError as exc:
        print(f"afford-sparkd: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except SparkProtocolError as exc:
        print(f"afford-sparkd: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def serve(*, socket_file: Path | None, systemd: bool, codex: str) -> None:
    listener = _listen(socket_file, systemd=systemd)
    app_server = CodexAppServer.spawn(codex=codex)
    try:
        app_server.handshake()
        app_server.check_auth()
        threading.Thread(
            target=_watch_child,
            args=(app_server,),
            name="sparkd-child",
            daemon=True,
        ).start()
        lock = threading.Lock()
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                break
            threading.Thread(
                target=_handle_conn,
                args=(conn, app_server, lock),
                name="sparkd-conn",
                daemon=True,
            ).start()
    finally:
        app_server.close()
        with contextlib.suppress(OSError):
            listener.close()


def _watch_child(app_server: CodexAppServer) -> None:
    """Child death is terminal. systemd ``Restart=on-failure`` replaces us."""
    app_server.wait_child()
    if app_server.closed:
        return
    os._exit(1)


def _handle_conn(conn: socket.socket, app_server: CodexAppServer, lock: threading.Lock) -> None:
    try:
        line = _recv_line(conn)
        response = _dispatch(line, app_server, lock)
        conn.sendall(response.model_dump_json().encode() + b"\n")
    except OSError:
        return
    finally:
        try:
            conn.close()
        except OSError:
            return


def _dispatch(line: str, app_server: CodexAppServer, lock: threading.Lock) -> DaemonResponse:
    ident = "unknown"
    try:
        req = DaemonRequest.model_validate_json(line)
    except ValidationError as exc:
        return DaemonResponse(
            v=DAEMON_PROTOCOL,
            id=ident,
            ok=False,
            error="protocol",
            message=f"request is not pinned v{DAEMON_PROTOCOL}: {exc.error_count()} errors",
        )
    ident = req.id
    if req.v != DAEMON_PROTOCOL:
        return DaemonResponse(
            v=DAEMON_PROTOCOL,
            id=ident,
            ok=False,
            error="protocol",
            message=f"request protocol {req.v} is not pinned v{DAEMON_PROTOCOL}",
        )
    deadline = time.monotonic() + req.timeout_s
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not lock.acquire(timeout=remaining):
        return DaemonResponse(
            v=DAEMON_PROTOCOL,
            id=ident,
            ok=False,
            error="timeout",
            message=f"spark {req.verb} timed out after {req.timeout_s}s",
        )
    try:
        raw = app_server.invoke(
            prompt=req.prompt,
            workdir=Path(req.workdir),
            writable=req.writable,
            schema=req.output_schema,
            timeout_s=max(0.05, deadline - time.monotonic()),
        )
    except TimeoutError as exc:
        return DaemonResponse(
            v=DAEMON_PROTOCOL, id=ident, ok=False, error="timeout", message=str(exc)
        )
    except SparkUnavailableError as exc:
        kind = "auth" if "auth" in str(exc).lower() else "unavailable"
        return DaemonResponse(v=DAEMON_PROTOCOL, id=ident, ok=False, error=kind, message=str(exc))
    except SparkProtocolError as exc:
        text = str(exc)
        error = "timeout" if "timed out" in text.lower() else "protocol"
        return DaemonResponse(v=DAEMON_PROTOCOL, id=ident, ok=False, error=error, message=text)
    except Exception as exc:
        return DaemonResponse(
            v=DAEMON_PROTOCOL, id=ident, ok=False, error="protocol", message=str(exc)
        )
    else:
        return DaemonResponse(v=DAEMON_PROTOCOL, id=ident, ok=True, raw=raw, model=SPARK_MODEL)
    finally:
        lock.release()


def _listen(socket_file: Path | None, *, systemd: bool) -> socket.socket:
    if systemd:
        fds = int(os.environ.get("LISTEN_FDS", "0"))
        pid = int(os.environ.get("LISTEN_PID", "0"))
        if fds < 1 or (pid not in {0, os.getpid()}):
            raise SparkProtocolError(
                "systemd socket activation required (--systemd) but LISTEN_FDS is unset"
            )
        sock = socket.fromfd(SD_LISTEN_FDS_START, socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(True)
        return sock
    path = socket_file or socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_socket():
            raise SparkProtocolError(f"refusing to replace non-socket path {path}")
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, 0o600)
    sock.listen(16)
    return sock


def _recv_line(conn: socket.socket) -> str:
    chunks: list[bytes] = []
    conn.settimeout(30.0)
    while True:
        piece = conn.recv(65536)
        if not piece:
            break
        chunks.append(piece)
        if b"\n" in piece:
            break
    return b"".join(chunks).split(b"\n", 1)[0].decode()


if __name__ == "__main__":
    main()
