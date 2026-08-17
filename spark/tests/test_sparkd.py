"""Falsifiers for the warm-daemon transport. The 30 oneshot tests stay in
``test_spark.py`` and are not edited by this slice.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from afford_spark.engine import (
    SparkProtocolError,
    SparkUnavailableError,
    _telemetry_path,
    run_spark,
)
from afford_spark.models import LocateResult
from afford_spark.protocol import DAEMON_PROTOCOL, DaemonRequest, DaemonResponse, socket_path


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
    proc, _ = _start_daemon(tmp_path, monkeypatch)
    try:
        children = _descendant_pids(proc.pid)
        assert children, "daemon spawned no app-server child"
        os.kill(children[0], signal.SIGKILL)
        code = proc.wait(timeout=5)
        assert code != 0
    finally:
        if proc.poll() is None:
            _stop(proc)


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
