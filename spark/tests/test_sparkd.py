"""Falsifiers for the warm-daemon transport. The 30 oneshot tests stay in
``test_spark.py`` and are not edited by this slice.
"""

from __future__ import annotations

import json
import os
import signal
import socket as socklib
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from afford_spark.appserver import _notification_belongs, _rpc_error
from afford_spark.auth import classify_refusal, refusal_message
from afford_spark.client import request_daemon
from afford_spark.engine import (
    SparkProtocolError,
    SparkUnavailableError,
    _telemetry_path,
    run_spark,
)
from afford_spark.models import LocateResult
from afford_spark.protocol import (
    DAEMON_PROTOCOL,
    DaemonRequest,
    DaemonResponse,
    resolve_transport,
    socket_path,
)


def _install_fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    fake = Path(__file__).resolve().parent / "fake_codex_app_server.py"
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$1" != "app-server" ]; then echo "unexpected: $*" >&2; exit 1; fi\n'
        f"exec {sys.executable} {fake}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return script


def _start_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.Popen[str], Path]:
    _install_fake_codex(tmp_path, monkeypatch)
    sock = tmp_path / "sparkd.sock"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(sock))
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "daemon")
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
        for key, value in extra_env.items():
            monkeypatch.setenv(key, value)
    proc = subprocess.Popen(
        [sys.executable, "-m", "afford_spark.daemon", "--socket", str(sock)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sock.exists():
            # Give handshake a beat so the first accept is ready.
            time.sleep(0.05)
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                raise AssertionError(f"daemon exited {proc.returncode}: {err}")
            return proc, sock
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(f"daemon exited {proc.returncode} before bind: {err}")
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("daemon never created its socket")


def _await_boot_rpcs(rpc_log: Path, timeout_s: float = 5.0) -> list[str]:
    """Boot's last RPC is ``check_auth``'s ``account/read``; wait for it.

    ``serve()`` binds the socket *before* the handshake, so socket existence
    is not boot completion. Sleeping on the difference makes the exact-list
    assertion flake under a loaded runner.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        seen = rpc_log.read_text().splitlines() if rpc_log.is_file() else []
        if "account/read" in seen:
            return seen
        time.sleep(0.02)
    raise AssertionError("daemon never completed its boot RPCs")


def _stop(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def _descendant_pids(pid: int) -> list[int]:
    kids: list[int] = []
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        raw = path.read_text().split()
    except FileNotFoundError:
        return kids
    for item in raw:
        child = int(item)
        kids.append(child)
        kids.extend(_descendant_pids(child))
    return kids


def _read_telemetry() -> list[dict]:
    path = _telemetry_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_invalid_transport_is_typed_protocol_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "Daemon!")
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(tmp_path / "no.sock"))
    with pytest.raises(SparkProtocolError, match="AFFORD_SPARK_TRANSPORT"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    rec = _read_telemetry()[-1]
    assert rec["status"] == "protocol_error"


def test_oneshot_when_no_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(tmp_path / "no-such.sock"))
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "auto")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    # No daemon, no fake app-server — this is the exec path. A missing
    # ``codex exec`` is unavailable, which proves we did not require the socket.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(SparkUnavailableError, match="codex CLI not found"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    rec = _read_telemetry()[-1]
    assert rec["transport"] == "oneshot"


def test_forced_daemon_without_socket_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "daemon")
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(tmp_path / "missing.sock"))
    with pytest.raises(SparkUnavailableError, match="socket missing"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    rec = _read_telemetry()[-1]
    assert rec["transport"] == "daemon"
    assert rec["status"] == "unavailable"


def test_protocol_pin_rejects_v2_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, sock = _start_daemon(tmp_path, monkeypatch)
    try:
        # Bypass the frozen model so we can send a real v2 frame.
        payload = DaemonRequest(
            id="req-v2",
            verb="locate",
            prompt="q",
            workdir=str(tmp_path),
            output_schema={"type": "object"},
        ).model_dump()
        payload["v"] = 2
        import socket as socklib

        s = socklib.socket(socklib.AF_UNIX, socklib.SOCK_STREAM)
        s.settimeout(2)
        s.connect(str(sock))
        s.sendall((json.dumps(payload) + "\n").encode())
        line = s.recv(65536).decode().strip()
        s.close()
        response = DaemonResponse.model_validate_json(line)
        assert response.ok is False
        assert response.error == "protocol"
        assert "pinned" in (response.message or "")
        assert response.v == DAEMON_PROTOCOL
    finally:
        _stop(proc)


def test_auth_classification_uses_structured_code() -> None:
    misclassified = _rpc_error(
        "thread/start", {"code": -32403, "message": "cwd is not a directory"}
    )
    assert isinstance(misclassified, SparkProtocolError)
    assert not isinstance(misclassified, SparkUnavailableError)

    path_lookalike = _rpc_error(
        "turn/start", {"code": -32000, "message": "see src/http/error403.py"}
    )
    assert isinstance(path_lookalike, SparkProtocolError)
    assert not isinstance(path_lookalike, SparkUnavailableError)

    path_segment = _rpc_error(
        "turn/start",
        {"code": -32602, "message": "cwd /repo/tests/fixtures/403/ is not a directory"},
    )
    assert isinstance(path_segment, SparkProtocolError)

    file_line = _rpc_error(
        "turn/start",
        {"code": -32603, "message": "read failed at src/engine.py:403: permission denied"},
    )
    assert isinstance(file_line, SparkProtocolError)

    count = _rpc_error(
        "turn/start",
        {"code": -32000, "message": "tool call failed after scanning 401 files"},
    )
    assert isinstance(count, SparkProtocolError)

    by_code = _rpc_error("account/read", {"code": 401, "message": "nope"})
    assert isinstance(by_code, SparkUnavailableError)
    assert "re-authenticate" in str(by_code)

    by_pool_code = _rpc_error("turn/start", {"code": 429, "message": "nope"})
    assert isinstance(by_pool_code, SparkUnavailableError)
    assert "usage or rate limit" in str(by_pool_code)
    assert "re-authenticate" not in str(by_pool_code)

    by_message = _rpc_error(
        "account/read", {"code": -32000, "message": "unauthorized: token expired"}
    )
    assert isinstance(by_message, SparkUnavailableError)
    assert "re-authenticate" in str(by_message)

    wrapped_status = _rpc_error(
        "turn/start", {"code": -32000, "message": "Error: request failed with status 401"}
    )
    assert isinstance(wrapped_status, SparkUnavailableError)
    assert "re-authenticate" in str(wrapped_status)

    pool = _rpc_error(
        "turn/start", {"code": -32000, "message": "You have reached your usage limit."}
    )
    assert isinstance(pool, SparkUnavailableError)
    assert "usage or rate limit" in str(pool)
    assert "re-authenticate" not in str(pool)
    assert "auth" not in str(pool).lower()

    unstructured_401 = _rpc_error("turn/start", "Error: request failed with status 401")
    assert isinstance(unstructured_401, SparkUnavailableError)
    assert "re-authenticate" in str(unstructured_401)

    unstructured_count = _rpc_error("turn/start", "scanning 401 files")
    assert isinstance(unstructured_count, SparkProtocolError)


def test_classify_refusal_agrees_across_transports() -> None:
    # One predicate: oneshot and daemon cannot diverge.
    want = {
        "Error: request failed with status 401": "auth",
        "HTTP 403 Forbidden": "auth",
        "401 Unauthorized": "auth",
        "server returned 403": "auth",
        "response 401": "auth",
        "auth expired, run codex login": "auth",
        "token expired": "auth",
        "not logged in": "auth",
        "login required": "auth",
        "unauthenticated": "auth",
        "You have reached your usage limit for Codex.": "unavailable",
        "rate limit exceeded, retry after 60s": "unavailable",
        "stream error: server returned 429 Too Many Reqs": "unavailable",
        "cwd /repo/tests/fixtures/403/ is not a directory": None,
        "read failed at src/engine.py:403: permission denied": None,
        "tool call failed after scanning 401 files": None,
        "turn took 403 ms": None,
        "src/http/error403.py not found": None,
        "cwd is not a directory": None,
    }
    for text, expected in want.items():
        assert classify_refusal(text) == expected, text


def test_oneshot_status_401_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Oneshot has no structured RPC code. ``401`` in stderr must stay an
    # entitlement refusal (status=unavailable), as it is on main.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text("#!/bin/bash\necho 'Error: request failed with status 401' >&2\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "oneshot")
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(tmp_path / "no.sock"))
    with pytest.raises(SparkUnavailableError, match="re-authenticate"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    rec = _read_telemetry()[-1]
    assert rec["status"] == "unavailable"
    assert rec["transport"] == "oneshot"


def test_oneshot_error403_path_is_not_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text("#!/bin/bash\necho 'see src/http/error403.py' >&2\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "oneshot")
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(tmp_path / "no.sock"))
    with pytest.raises(SparkProtocolError, match="codex exec failed"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    rec = _read_telemetry()[-1]
    assert rec["status"] == "protocol_error"


def test_daemon_usage_limit_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_USAGE_LIMIT": "1"})
    try:
        with pytest.raises(SparkUnavailableError, match="usage or rate limit"):
            run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
        rec = _read_telemetry()[-1]
        assert rec["transport"] == "daemon"
        assert rec["status"] == "unavailable"
    finally:
        _stop(proc)


def test_warm_call_is_five_rpcs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rpc_log = tmp_path / "rpc.log"
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_RPC_LOG": str(rpc_log)})
    try:
        before = _await_boot_rpcs(rpc_log)
        result = run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
        assert result.status == "complete"
        after = rpc_log.read_text().splitlines()
        assert after[len(before) :] == [
            "account/read",
            "thread/start",
            "turn/start",
            "thread/archive",
            "thread/unsubscribe",
        ]
    finally:
        _stop(proc)


def test_auth_expiry_fails_loud_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Startup handshake consumes one account/read; the first invoke consumes
    # the second. The third call (second invoke) must fail loud.
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_AUTH_AFTER": "2"})
    try:
        first = run_spark("first", verb="locate", workdir=tmp_path, schema=LocateResult)
        assert first.status == "complete"
        with pytest.raises(SparkUnavailableError, match="auth expired"):
            run_spark("second", verb="locate", workdir=tmp_path, schema=LocateResult)
        rec = _read_telemetry()[-1]
        assert rec["transport"] == "daemon"
        assert rec["status"] == "unavailable"
        # Still alive — expiry is a request failure, not a crash-loop retry.
        assert proc.poll() is None
    finally:
        _stop(proc)


def test_per_request_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_TURN_SLEEP": "1.2"})
    try:
        with pytest.raises(SparkProtocolError, match="timed out"):
            run_spark(
                "q", verb="locate", workdir=tmp_path, schema=LocateResult, timeout_s=1
            )  # fake sleeps 1.2s; per-request budget is 1s
        rec = _read_telemetry()[-1]
        assert rec["transport"] == "daemon"
        assert rec["status"] == "timeout"
    finally:
        _stop(proc)


def test_dead_appserver_exits_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, sock = _start_daemon(tmp_path, monkeypatch)
    try:
        children = _descendant_pids(proc.pid)
        assert children, "daemon spawned no app-server child"
        os.kill(children[0], signal.SIGKILL)
        code = proc.wait(timeout=5)
        assert code != 0
        assert not sock.exists()
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_auto_treats_unconnectable_socket_as_oneshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "stale.sock"
    leftover = socklib.socket(socklib.AF_UNIX, socklib.SOCK_STREAM)
    leftover.bind(str(stale))
    leftover.close()
    assert stale.exists()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(stale))
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "auto")
    assert resolve_transport() == "oneshot"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(SparkUnavailableError, match="codex CLI not found"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    rec = _read_telemetry()[-1]
    assert rec["transport"] == "oneshot"


def test_queued_request_times_out_within_own_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two concurrent callers, each with a 4s budget, against a 3.5s turn.
    # The predecessor must not stall the queued caller past its own budget
    # (the client's blind deadline is budget+2 ≈ 6s).
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_TURN_SLEEP": "3.5"})
    try:
        outcomes: list[tuple[float, BaseException | None]] = []
        barrier = threading.Barrier(2)

        def call() -> None:
            barrier.wait()
            started = time.monotonic()
            try:
                run_spark(
                    "q",
                    verb="locate",
                    workdir=tmp_path,
                    schema=LocateResult,
                    timeout_s=4,
                )
                outcomes.append((time.monotonic() - started, None))
            except BaseException as exc:
                outcomes.append((time.monotonic() - started, exc))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=12)
        assert all(not thread.is_alive() for thread in threads)
        oks = [item for item in outcomes if item[1] is None]
        timeouts = [
            item
            for item in outcomes
            if isinstance(item[1], SparkProtocolError) and "timed out" in str(item[1])
        ]
        assert len(oks) == 1
        assert len(timeouts) == 1
        assert timeouts[0][0] < 5.5
    finally:
        _stop(proc)


def test_auth_hop_is_inside_the_request_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hop 1 must not restart the clock.

    Budget 3s, a 2s ``account/read`` and a 2s turn. If the auth hop is charged
    to its own ``min(10, timeout_s)`` window and ``invoke`` then starts a fresh
    ``timeout_s``, the daemon services the call in ~4s — past the caller's own
    budget, and (with a slower auth hop) past the client's blind deadline, so a
    metered turn completes for nobody. One deadline means the call fails inside
    its budget instead.
    """
    # The stall is on the per-request re-check, not boot's account/read.
    proc, _ = _start_daemon(
        tmp_path,
        monkeypatch,
        extra_env={"AFFORD_FAKE_ACCOUNT_SLEEP": "2", "AFFORD_FAKE_TURN_SLEEP": "2"},
    )
    try:
        started = time.monotonic()
        with pytest.raises(SparkProtocolError, match="timed out"):
            run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult, timeout_s=3)
        elapsed = time.monotonic() - started
        assert elapsed < 3 + 0.75, f"daemon ran {elapsed:.2f}s past a 3s budget"
        rec = _read_telemetry()[-1]
        assert rec["transport"] == "daemon"
        assert rec["status"] == "timeout"
    finally:
        _stop(proc)


def test_teardown_is_inside_the_request_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drop runs before the response is written, so it is caller latency.

    A fixed 5s-per-hop teardown against a 2s budget puts delivery ~6s out,
    past the client's ``timeout_s + 2`` grace — the turn completed and the
    caller sees a timeout. The reserved slice keeps teardown inside the
    budget; a slow archive costs the drop, never the answer.
    """
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_DROP_SLEEP": "3"})
    try:
        started = time.monotonic()
        result = run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult, timeout_s=2)
        elapsed = time.monotonic() - started
        assert result.status == "complete"
        assert elapsed < 2, f"teardown pushed delivery to {elapsed:.2f}s of a 2s budget"
        rec = _read_telemetry()[-1]
        assert rec["status"] == "complete"
        assert rec["transport"] == "daemon"
    finally:
        _stop(proc)


def test_auth_hop_error_is_not_reclassified_from_rendered_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``check_auth`` must not re-run the classifier on the rendered error.

    ``_rpc_error`` already ruled on ``{-403, "cwd is not a directory"}`` — not
    a refusal. Re-classifying ``"codex account/read error -403: ..."`` finds
    the ``403`` the rendering itself introduced, so the same error object came
    back as auth from ``account/read`` and as protocol from ``turn/start``.
    """
    error = json.dumps({"code": -403, "message": "cwd is not a directory"})
    # Boot's account/read succeeds; the invoke's re-check gets the error.
    proc, _ = _start_daemon(
        tmp_path,
        monkeypatch,
        extra_env={"AFFORD_FAKE_AUTH_ERROR": error, "AFFORD_FAKE_AUTH_AFTER": "1"},
    )
    try:
        with pytest.raises(SparkProtocolError) as caught:
            run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult, timeout_s=10)
        assert not isinstance(caught.value, SparkUnavailableError)
        assert "re-authenticate" not in str(caught.value).lower()
        assert "cwd is not a directory" in str(caught.value)
        # Same object through turn/start: protocol on both sides, one verdict.
        assert isinstance(
            _rpc_error("turn/start", {"code": -403, "message": "cwd is not a directory"}),
            SparkProtocolError,
        )
        assert proc.poll() is None
    finally:
        _stop(proc)


def test_classify_refusal_ranges_over_its_own_tokens() -> None:
    """Range over the predicate's tokens, not over a reviewer's examples.

    Every context word x every code, then the same words carrying a payload
    that is a count rather than a status. The last block is the decided
    accept: a bare ``<context word> <code>`` is a refusal even when the
    number is a count, and ``exit code 429`` is the case that costs.
    """
    for word in ("status", "http", "code", "returned", "response", "error"):
        for token, expected in (("401", "auth"), ("403", "auth"), ("429", "unavailable")):
            text = f"request failed with {word} {token}"
            assert classify_refusal(text) == expected, text

    not_a_status = {
        "internal error, code 403 chunks pending": None,
        "returned 401 rows": None,
        "error at line 401 of the schema": None,
        "response body was 403 bytes": None,
        "error: cannot open src/403/handler.rs": None,
        "error scanning 401 files": None,
        "code 429 handlers registered": None,
    }
    for text, expected in not_a_status.items():
        assert classify_refusal(text) == expected, text

    # Word markers are the reliable half and are not gated on a number.
    assert classify_refusal("Too Many Requests (429)") == "unavailable"
    assert classify_refusal("429 Client Error: Too Many Requests for url") == "unavailable"

    # Decided accept (see auth.py): terminal code after a context word is a
    # refusal even when it is a count. False refusal beats silent fall-through.
    assert classify_refusal("exit code 429") == "unavailable"


def test_daemon_error_kind_is_the_typed_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire kind comes from the classifier, not from matching the prose."""
    auth = SparkUnavailableError(refusal_message("auth"), kind="auth")
    pool = SparkUnavailableError(refusal_message("unavailable"), kind="unavailable")
    assert auth.kind == "auth"
    assert pool.kind == "unavailable"
    # The refusal prose no longer carries the word the old derivation keyed on.
    assert "auth" not in str(pool).lower()

    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_USAGE_LIMIT": "1"})
    try:
        req = DaemonRequest(
            id="kind-1",
            verb="locate",
            prompt="q",
            workdir=str(tmp_path),
            output_schema={"type": "object"},
            timeout_s=10,
        )
        response = request_daemon(req, socket_path())
        assert response.ok is False
        assert response.error == "unavailable"
    finally:
        _stop(proc)


def test_notification_without_identity_is_not_this_turn() -> None:
    anonymous = {
        "method": "turn/completed",
        "params": {"turn": {"status": "completed"}},
    }
    assert _notification_belongs(anonymous, turn_id="turn-9", thread_id="thread-9") is False
    identified = {
        "method": "turn/completed",
        "params": {"threadId": "thread-9", "turn": {"id": "turn-9", "status": "completed"}},
    }
    assert _notification_belongs(identified, turn_id="turn-9", thread_id="thread-9") is True


def test_unidentified_completion_does_not_leak_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Interrupt is missing and the abandoned completion carries no ids —
    # the identity filter is the only remaining boundary. It must not
    # return the predecessor's payload as a successful answer.
    proc, _ = _start_daemon(
        tmp_path,
        monkeypatch,
        extra_env={
            "AFFORD_FAKE_TURN_SLEEP": "3",
            "AFFORD_FAKE_INTERRUPT": "missing",
            "AFFORD_FAKE_STRIP_IDS_ON_INTERRUPT": "1",
            "AFFORD_FAKE_ECHO_PROMPTS": "1",
        },
    )
    try:
        with pytest.raises(SparkProtocolError, match="timed out"):
            run_spark(
                "SECRET-FIRST",
                verb="locate",
                workdir=tmp_path,
                schema=LocateResult,
                timeout_s=1,
            )
        try:
            result = run_spark(
                "SECRET-SECOND",
                verb="locate",
                workdir=tmp_path,
                schema=LocateResult,
                timeout_s=10,
            )
        except SparkProtocolError as exc:
            assert "SECRET-FIRST" not in str(exc)
            assert "no turn or thread id" in str(exc)
        else:
            assert "SECRET-FIRST" not in (result.reason or "")
    finally:
        _stop(proc)


def test_timeout_does_not_poison_subsequent_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A timed-out turn still emits turn/completed afterwards. The next
    # healthy call must not consume that stale notification.
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_TURN_SLEEP": "1.2"})
    try:
        with pytest.raises(SparkProtocolError, match="timed out"):
            run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult, timeout_s=1)
        for _ in range(4):
            result = run_spark(
                "q", verb="locate", workdir=tmp_path, schema=LocateResult, timeout_s=30
            )
            assert result.status == "complete"
    finally:
        _stop(proc)


def test_no_context_leak_between_consecutive_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = tmp_path / "history.jsonl"
    proc, _ = _start_daemon(tmp_path, monkeypatch, extra_env={"AFFORD_FAKE_HISTORY": str(history)})
    try:
        token_a = "SECRET_TOKEN_ALPHA"
        token_b = "SECRET_TOKEN_BRAVO"
        run_spark(token_a, verb="locate", workdir=tmp_path, schema=LocateResult)
        # Second call must not see the first prompt. The fake echoes every
        # prompt the *thread* has seen; a reused conversation would put
        # token_a in the second payload and fail LocateResult validation
        # (extra field) — so we read the history the fake persisted instead.
        run_spark(token_b, verb="locate", workdir=tmp_path, schema=LocateResult)
        events = [json.loads(line) for line in history.read_text().splitlines()]
        turns = [e for e in events if "prompts" in e]
        drops = [e for e in events if "dropped" in e]
        assert len(turns) == 2
        assert turns[0]["prompts"] == [token_a]
        assert turns[1]["prompts"] == [token_b]
        assert turns[0]["thread"] != turns[1]["thread"]
        assert {turns[0]["thread"], turns[1]["thread"]} <= {e["dropped"] for e in drops}
        recs = _read_telemetry()
        assert recs[-1]["transport"] == "daemon"
        assert recs[-2]["transport"] == "daemon"
    finally:
        _stop(proc)


def test_daemon_transport_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _ = _start_daemon(tmp_path, monkeypatch)
    try:
        result = run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
        assert result.status == "complete"
        rec = _read_telemetry()[-1]
        assert rec["transport"] == "daemon"
        assert rec["operation"] == "locate"
        assert rec["status"] == "complete"
        assert rec["verification"] is None
    finally:
        _stop(proc)


def test_socket_path_honors_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(tmp_path / "custom.sock"))
    assert socket_path() == tmp_path / "custom.sock"


def test_appserver_protocol_pin_refuses_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_codex(tmp_path, monkeypatch)
    sock = tmp_path / "sparkd.sock"
    monkeypatch.setenv("AFFORD_FAKE_PROTOCOL", "99")
    env = os.environ.copy()
    env["AFFORD_FAKE_PROTOCOL"] = "99"
    proc = subprocess.Popen(
        [sys.executable, "-m", "afford_spark.daemon", "--socket", str(sock)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        code = proc.wait(timeout=5)
        assert code != 0
        err = proc.stderr.read() if proc.stderr else ""
        assert "pinned" in err
    finally:
        if proc.poll() is None:
            _stop(proc)
