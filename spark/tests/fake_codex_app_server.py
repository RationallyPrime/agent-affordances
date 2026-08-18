"""Stdio JSON-RPC fake of ``codex app-server`` for wrapper tests.

Env knobs (all optional):

* ``AFFORD_FAKE_PROTOCOL`` — integer ``protocolVersion`` in initialize
* ``AFFORD_FAKE_AUTH`` — ``ok`` (default) or ``expired``
* ``AFFORD_FAKE_AUTH_AFTER`` — succeed this many ``account/read`` calls, then expire
* ``AFFORD_FAKE_TURN_SLEEP`` — seconds to wait before ``turn/completed``
* ``AFFORD_FAKE_HISTORY`` — path of a JSONL file; each turn appends
  ``{thread, prompts}`` so tests can assert isolation
* ``AFFORD_FAKE_INTERRUPT`` — ``ok`` (default) or ``missing`` (``-32601``)
* ``AFFORD_FAKE_STRIP_IDS_ON_INTERRUPT`` — if set, a turn whose interrupt
  was attempted emits ``turn/completed`` with no turn/thread ids
* ``AFFORD_FAKE_ECHO_PROMPTS`` — if set, ``reason`` is the thread's prompt list
* ``AFFORD_FAKE_RPC_LOG`` — path; each received method name is appended
* ``AFFORD_FAKE_USAGE_LIMIT`` — if set, ``turn/start`` returns a pool refusal
* ``AFFORD_FAKE_ACCOUNT_SLEEP`` — seconds to stall every ``account/read``
  after boot's first, i.e. the per-request auth re-check
* ``AFFORD_FAKE_DROP_SLEEP`` — seconds to stall each archive/unsubscribe
* ``AFFORD_FAKE_AUTH_ERROR`` — JSON ``{"code": …, "message": …}`` returned
  verbatim by ``account/read`` once ``AFFORD_FAKE_AUTH_AFTER`` successes are
  spent, so the wrapper's classification of a non-refusal auth-hop error can
  be witnessed without failing the daemon's boot check
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

_send_lock = threading.Lock()


def _send(payload: dict[str, Any]) -> None:
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    with _send_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def main() -> None:
    protocol = int(os.environ.get("AFFORD_FAKE_PROTOCOL", "1"))
    auth = os.environ.get("AFFORD_FAKE_AUTH", "ok")
    auth_after = os.environ.get("AFFORD_FAKE_AUTH_AFTER")
    remaining_ok = int(auth_after) if auth_after is not None else None
    sleep_s = float(os.environ.get("AFFORD_FAKE_TURN_SLEEP", "0"))
    history_path = os.environ.get("AFFORD_FAKE_HISTORY")
    interrupt_mode = os.environ.get("AFFORD_FAKE_INTERRUPT", "ok")
    strip_ids_on_interrupt = bool(os.environ.get("AFFORD_FAKE_STRIP_IDS_ON_INTERRUPT"))
    echo_prompts = bool(os.environ.get("AFFORD_FAKE_ECHO_PROMPTS"))
    rpc_log = os.environ.get("AFFORD_FAKE_RPC_LOG")
    usage_limit = bool(os.environ.get("AFFORD_FAKE_USAGE_LIMIT"))
    account_sleep_s = float(os.environ.get("AFFORD_FAKE_ACCOUNT_SLEEP", "0"))
    account_calls = 0
    drop_sleep_s = float(os.environ.get("AFFORD_FAKE_DROP_SLEEP", "0"))
    raw_auth_error = os.environ.get("AFFORD_FAKE_AUTH_ERROR")
    auth_error = json.loads(raw_auth_error) if raw_auth_error else None
    threads: dict[str, list[str]] = {}
    pending_turns: dict[str, threading.Event] = {}
    stripped_ids: set[str] = set()
    next_thread = 1
    next_turn = 1

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        ident = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if isinstance(method, str) and rpc_log:
            with open(rpc_log, "a") as fh:
                fh.write(method + "\n")

        if method == "initialize":
            _send({"id": ident, "result": {"protocolVersion": protocol, "userAgent": "fake"}})
            continue
        if method == "initialized":
            continue
        if method in {"account/read", "account/rateLimits/read"}:
            if account_sleep_s and account_calls:
                time.sleep(account_sleep_s)
            account_calls += 1
            if auth_error is not None:
                # AFFORD_FAKE_AUTH_AFTER buys that many successes first, so
                # the daemon's boot check can pass before the error lands.
                if remaining_ok:
                    remaining_ok -= 1
                    _send({"id": ident, "result": {"account": {"email": "spark@test"}}})
                else:
                    _send({"id": ident, "error": auth_error})
                continue
            if remaining_ok is not None:
                if remaining_ok <= 0:
                    auth = "expired"
                else:
                    remaining_ok -= 1
            if auth == "expired":
                _send(
                    {
                        "id": ident,
                        "error": {"code": 401, "message": "unauthorized: token expired"},
                    }
                )
            else:
                _send({"id": ident, "result": {"account": {"email": "spark@test"}}})
            continue
        if method == "thread/start":
            thread_id = f"thread-{next_thread}"
            next_thread += 1
            threads[thread_id] = []
            _send(
                {
                    "id": ident,
                    "result": {
                        "thread": {"id": thread_id, "ephemeral": True, "status": {"type": "idle"}}
                    },
                }
            )
            _send({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
            continue
        if method == "turn/start":
            if usage_limit:
                _send(
                    {
                        "id": ident,
                        "error": {
                            "code": -32000,
                            "message": "You have reached your usage limit.",
                        },
                    }
                )
                continue
            thread_id = str(params.get("threadId") or "")
            prompts = threads.setdefault(thread_id, [])
            user_text = ""
            incoming = params.get("input")
            if isinstance(incoming, list) and incoming and isinstance(incoming[0], dict):
                user_text = str(incoming[0].get("text") or "")
            prompts.append(user_text)
            turn_id = f"turn-{next_turn}"
            next_turn += 1
            _send({"id": ident, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            cancel = threading.Event()
            pending_turns[turn_id] = cancel

            def _finish(
                tid: str = turn_id,
                th: str = thread_id,
                seen: list[str] | None = None,
                ev: threading.Event = cancel,
            ) -> None:
                snapshot = [] if seen is None else list(seen)
                interrupted = ev.wait(sleep_s) if sleep_s > 0 else ev.is_set()
                status = "interrupted" if interrupted else "completed"
                reason = json.dumps(snapshot) if echo_prompts else None
                body = json.dumps(
                    {
                        "status": "complete",
                        "matches": [],
                        "searched_paths": 1,
                        "uncertainty": [],
                        "reason": reason,
                    }
                )
                if history_path and not interrupted:
                    with open(history_path, "a") as fh:
                        fh.write(json.dumps({"thread": th, "prompts": snapshot}) + "\n")
                params: dict[str, Any] = {
                    "turn": {
                        "status": status,
                        "items": [{"type": "agentMessage", "text": body}],
                    }
                }
                if tid not in stripped_ids:
                    params["threadId"] = th
                    params["turn"]["id"] = tid
                _send({"method": "turn/completed", "params": params})

            threading.Thread(
                target=_finish,
                kwargs={"seen": list(prompts)},
                name=f"fake-turn-{turn_id}",
                daemon=True,
            ).start()
            continue
        if method == "turn/interrupt":
            turn_id = str(params.get("turnId") or "")
            ev = pending_turns.get(turn_id)
            if strip_ids_on_interrupt and turn_id:
                stripped_ids.add(turn_id)
            if interrupt_mode == "missing":
                # Method is absent: do not cancel the in-flight turn.
                _send(
                    {
                        "id": ident,
                        "error": {"code": -32601, "message": "Method not found: turn/interrupt"},
                    }
                )
                continue
            if ev is not None:
                ev.set()
            _send({"id": ident, "result": {}})
            continue
        if method in {"thread/archive", "thread/unsubscribe"}:
            if drop_sleep_s:
                time.sleep(drop_sleep_s)
            thread_id = str(params.get("threadId") or "")
            threads.pop(thread_id, None)
            if history_path:
                with open(history_path, "a") as fh:
                    fh.write(json.dumps({"dropped": thread_id}) + "\n")
            _send({"id": ident, "result": {}})
            continue
        if ident is not None:
            _send(
                {
                    "id": ident,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    # ``codex app-server`` (and any other argv) — this process IS the server.
    main()
