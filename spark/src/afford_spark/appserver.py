"""One long-lived ``codex app-server`` JSON-RPC session (stdio JSONL).

The daemon holds this process so boot is amortized. Isolation is not the
process: every ``invoke`` starts a fresh ephemeral thread and drops it after
the turn completes (or fails). Auth expiry and protocol mismatch fail loud;
they never retry and never fall through to the metered pool.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from afford_spark.engine import SPARK_MODEL, SparkProtocolError, SparkUnavailableError

AUTH_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "not authenticated",
    "auth expired",
    "token expired",
    "login required",
    "re-authenticate",
    "not logged in",
)
# Structured JSON-RPC / HTTP auth codes. Never match these as substrings of a
# rendered error — a code of -32403 or a path like error403.py is not auth.
AUTH_CODES = {401, 403}
# Methods tried in order; a method-not-found is not an auth failure.
AUTH_METHODS = ("account/read", "account/rateLimits/read")
# App-server protocol we speak. A server that *states* a different version is
# refused; a server that states none is accepted (the handshake itself is the
# pin for that generation).
PINNED_APP_SERVER_PROTOCOL = 1


class AppServerDead(SparkProtocolError):
    """The child process exited or closed its pipes."""


class CodexAppServer:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._write_lock = threading.Lock()
        self._dead: str | None = None
        self._closed = False
        self._auth_method: str | bool | None = None  # None=unknown, False=none
        self._reader = threading.Thread(target=self._read_loop, name="sparkd-rpc", daemon=True)
        self._reader.start()

    @classmethod
    def spawn(cls, *, codex: str = "codex", env: dict[str, str] | None = None) -> CodexAppServer:
        child_env = {**(env or os.environ), "NO_COLOR": "1", "RUST_LOG": "error"}
        try:
            proc = subprocess.Popen(
                [codex, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=child_env,
            )
        except FileNotFoundError as exc:
            raise SparkUnavailableError(
                "codex CLI not found on PATH — install codex and authenticate first"
            ) from exc
        if proc.stdin is None or proc.stdout is None:
            raise SparkProtocolError("codex app-server spawned without stdio pipes")
        return cls(proc)

    def handshake(self, timeout_s: float = 15.0) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "afford-sparkd",
                    "title": "afford spark warm daemon",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": False, "requestAttestation": False},
            },
            timeout_s=timeout_s,
        )
        version = result.get("protocolVersion")
        if isinstance(version, int) and version != PINNED_APP_SERVER_PROTOCOL:
            raise SparkProtocolError(
                f"codex app-server protocol {version} is not pinned v{PINNED_APP_SERVER_PROTOCOL}"
            )
        self.notify("initialized")
        return result

    def check_auth(self, timeout_s: float = 10.0) -> None:
        """Fail loud on expiry. A missing auth method is not a pass — we still
        classify 401/unauthorized on the turn itself."""
        if self._auth_method is False:
            return
        methods: Iterator[str]
        if isinstance(self._auth_method, str):
            methods = iter((self._auth_method,))
        else:
            methods = iter(AUTH_METHODS)
        last_missing = False
        for method in methods:
            try:
                self.request(method, {}, timeout_s=timeout_s)
            except SparkUnavailableError:
                raise
            except SparkProtocolError as exc:
                if _is_missing_method(str(exc)):
                    last_missing = True
                    continue
                if _is_auth_text(str(exc)):
                    raise SparkUnavailableError(
                        "Spark auth expired or missing — re-authenticate the "
                        "Codex CLI (`codex login`). The warm daemon will not retry."
                    ) from exc
                raise
            else:
                self._auth_method = method
                return
        if last_missing:
            self._auth_method = False

    def invoke(
        self,
        *,
        prompt: str,
        workdir: Path,
        writable: bool,
        schema: dict[str, object],
        timeout_s: float,
    ) -> str:
        """Fresh ephemeral thread → one turn → drop. Returns the last agent text."""
        self.check_auth(timeout_s=min(10.0, timeout_s))
        # Stale notifications from an abandoned predecessor must not be
        # visible to this turn — the queue is process-global.
        self._drain_events()
        deadline = time.monotonic() + timeout_s
        thread = self.request(
            "thread/start",
            {
                "ephemeral": True,
                "cwd": str(workdir),
                "model": SPARK_MODEL,
                "sandbox": "workspace-write" if writable else "read-only",
            },
            timeout_s=_remaining(deadline),
        )
        thread_id = _thread_id(thread)
        if not thread_id:
            raise SparkProtocolError("thread/start returned no thread id")
        turn_id: str | None = None
        try:
            started = self.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                    "outputSchema": schema,
                    "model": SPARK_MODEL,
                },
                timeout_s=_remaining(deadline),
            )
            turn_id = _turn_id(started)
            completed = self.wait_notification(
                "turn/completed",
                timeout_s=_remaining(deadline),
                turn_id=turn_id,
                thread_id=thread_id,
            )
            params = completed.get("params")
            if not isinstance(params, dict):
                raise SparkProtocolError("turn/completed carried no params")
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise SparkProtocolError("turn/completed carried no turn")
            if turn_id and turn.get("id") not in {None, turn_id}:
                raise SparkProtocolError(
                    f"turn/completed id {turn.get('id')!r} != started {turn_id!r}"
                )
            status = turn.get("status")
            if status and status != "completed":
                raise SparkProtocolError(f"codex turn ended {status}")
            text = _agent_text(turn)
            if text is None:
                raise SparkProtocolError("codex turn completed with no agent message")
            return text
        except Exception:
            self._interrupt_turn(thread_id, turn_id)
            raise
        finally:
            self._drop(thread_id)

    def close(self) -> None:
        self._closed = True
        self._dead = self._dead or "closed"
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def request(self, method: str, params: object, timeout_s: float) -> dict[str, Any]:
        self._raise_if_dead()
        ident = self._next_id
        self._next_id += 1
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._pending[ident] = waiter
        self._send({"id": ident, "method": method, "params": params})
        try:
            message = waiter.get(timeout=max(0.05, timeout_s))
        except queue.Empty as exc:
            self._pending.pop(ident, None)
            raise SparkProtocolError(f"codex app-server {method} timed out") from exc
        if message.get("error"):
            raise _rpc_error(method, message["error"])
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: object | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def wait_notification(
        self,
        method: str,
        timeout_s: float,
        *,
        turn_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            self._raise_if_dead()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SparkProtocolError(f"timed out waiting for {method}")
            try:
                message = self._events.get(timeout=remaining)
            except queue.Empty as exc:
                raise SparkProtocolError(f"timed out waiting for {method}") from exc
            if message.get("method") != method:
                continue
            if not _notification_belongs(message, turn_id=turn_id, thread_id=thread_id):
                continue
            return message

    def _drain_events(self) -> None:
        try:
            while True:
                self._events.get_nowait()
        except queue.Empty:
            return

    def _interrupt_turn(self, thread_id: str, turn_id: str | None) -> None:
        if not turn_id:
            return
        try:
            self.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout_s=2.0,
            )
        except SparkProtocolError:
            return

    def _drop(self, thread_id: str) -> None:
        # Archive and unsubscribe are not a fallback chain: archive may succeed
        # while the connection is still subscribed and still emitting.
        for method in ("thread/archive", "thread/unsubscribe"):
            try:
                self.request(method, {"threadId": thread_id}, timeout_s=5.0)
            except SparkProtocolError:
                continue

    def _send(self, payload: dict[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            raise AppServerDead("codex app-server stdin is closed")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                stdin.write(line)
                stdin.flush()
            except OSError as exc:
                self._dead = f"stdin write failed: {exc}"
                raise AppServerDead(self._dead) from exc

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:
            self._dead = "no stdout"
            return
        try:
            for raw in stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                self._dispatch(message)
        finally:
            if self._dead is None:
                code = self._proc.poll()
                self._dead = f"codex app-server exited ({code})"
            for waiter in self._pending.values():
                waiter.put({"error": {"code": -32000, "message": self._dead}})

    def _dispatch(self, message: dict[str, Any]) -> None:
        ident = message.get("id")
        method = message.get("method")
        if (
            method is not None
            and ident is not None
            and "result" not in message
            and "error" not in message
        ):
            self._answer_server_request(ident, str(method))
            return
        if ident is not None and ident in self._pending:
            self._pending.pop(ident).put(message)
            return
        if method is not None:
            self._events.put(message)

    def _answer_server_request(self, ident: object, method: str) -> None:
        # Never block a turn on an approval prompt — this is a utility, not a seat.
        if "approval" in method.lower() or method.endswith("/requestUserInput"):
            self._send({"id": ident, "result": {"decision": "decline"}})
            return
        self._send(
            {
                "id": ident,
                "error": {"code": -32601, "message": f"afford-sparkd does not handle {method}"},
            }
        )

    def wait_child(self) -> int:
        return int(self._proc.wait())

    @property
    def closed(self) -> bool:
        return self._closed

    def _raise_if_dead(self) -> None:
        if self._dead is not None:
            if _is_auth_text(self._dead):
                raise SparkUnavailableError(
                    "Spark auth expired or missing — re-authenticate the "
                    "Codex CLI (`codex login`). The warm daemon will not retry."
                )
            raise AppServerDead(self._dead)


def _remaining(deadline: float) -> float:
    return max(0.05, deadline - time.monotonic())


def _thread_id(result: dict[str, Any]) -> str | None:
    thread = result.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return thread["id"]
    if isinstance(result.get("id"), str):
        return result["id"]
    if isinstance(result.get("threadId"), str):
        return result["threadId"]
    return None


def _turn_id(result: dict[str, Any]) -> str | None:
    turn = result.get("turn")
    if isinstance(turn, dict):
        if isinstance(turn.get("id"), str):
            return turn["id"]
        if isinstance(turn.get("turnId"), str):
            return turn["turnId"]
    if isinstance(result.get("turnId"), str):
        return result["turnId"]
    if isinstance(result.get("id"), str):
        return result["id"]
    return None


def _event_turn_id(message: dict[str, Any]) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    if isinstance(params.get("turnId"), str):
        return params["turnId"]
    return None


def _event_thread_id(message: dict[str, Any]) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    if isinstance(params.get("threadId"), str):
        return params["threadId"]
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("threadId"), str):
        return turn["threadId"]
    return None


def _notification_belongs(
    message: dict[str, Any],
    *,
    turn_id: str | None,
    thread_id: str | None,
) -> bool:
    got_turn = _event_turn_id(message)
    if turn_id and got_turn and got_turn != turn_id:
        return False
    got_thread = _event_thread_id(message)
    if thread_id and got_thread and got_thread != thread_id:
        return False
    return True


def _agent_text(turn: dict[str, Any]) -> str | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        text = item.get("text")
        if kind in {"agentMessage", "agent_message"} and isinstance(text, str):
            return text
    return None


def _rpc_error(method: str, error: object) -> SparkProtocolError | SparkUnavailableError:
    if isinstance(error, dict):
        message = str(error.get("message", error))
        code = error.get("code")
        text = f"codex {method} error {code}: {message}"
        if _is_auth_code(code) or _is_auth_text(message):
            return SparkUnavailableError(
                "Spark auth expired or missing — re-authenticate the "
                "Codex CLI (`codex login`). The warm daemon will not retry."
            )
        return SparkProtocolError(text)
    text = f"codex {method} error: {error}"
    if _is_auth_text(text):
        return SparkUnavailableError(
            "Spark auth expired or missing — re-authenticate the "
            "Codex CLI (`codex login`). The warm daemon will not retry."
        )
    return SparkProtocolError(text)


def _is_auth_code(code: object) -> bool:
    return isinstance(code, int) and code in AUTH_CODES


def _is_auth_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in AUTH_MARKERS)


def _is_missing_method(text: str) -> bool:
    lowered = text.lower()
    return "not found" in lowered or "unknown method" in lowered or "-32601" in lowered
