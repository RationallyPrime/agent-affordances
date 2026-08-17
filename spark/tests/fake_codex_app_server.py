"""Stdio JSON-RPC fake of ``codex app-server`` for wrapper tests.

Env knobs (all optional):

* ``AFFORD_FAKE_PROTOCOL`` — integer ``protocolVersion`` in initialize
* ``AFFORD_FAKE_AUTH`` — ``ok`` (default) or ``expired``
* ``AFFORD_FAKE_AUTH_AFTER`` — succeed this many ``account/read`` calls, then expire
* ``AFFORD_FAKE_TURN_SLEEP`` — seconds to wait before ``turn/completed``
* ``AFFORD_FAKE_HISTORY`` — path of a JSONL file; each turn appends
  ``{thread, prompts}`` so tests can assert isolation
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    protocol = int(os.environ.get("AFFORD_FAKE_PROTOCOL", "1"))
    auth = os.environ.get("AFFORD_FAKE_AUTH", "ok")
    auth_after = os.environ.get("AFFORD_FAKE_AUTH_AFTER")
    remaining_ok = int(auth_after) if auth_after is not None else None
    sleep_s = float(os.environ.get("AFFORD_FAKE_TURN_SLEEP", "0"))
    history_path = os.environ.get("AFFORD_FAKE_HISTORY")
    threads: dict[str, list[str]] = {}
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

        if method == "initialize":
            _send({"id": ident, "result": {"protocolVersion": protocol, "userAgent": "fake"}})
            continue
        if method == "initialized":
            continue
        if method in {"account/read", "account/rateLimits/read"}:
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
            if sleep_s > 0:
                time.sleep(sleep_s)
            # Isolation tell: the payload lists every prompt this thread has seen.
            body = json.dumps(
                {
                    "status": "complete",
                    "matches": [],
                    "searched_paths": 1,
                    "uncertainty": [],
                    "reason": None,
                }
            )
            if history_path:
                with open(history_path, "a") as fh:
                    fh.write(json.dumps({"thread": thread_id, "prompts": prompts}) + "\n")
            _send(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": turn_id,
                            "status": "completed",
                            "items": [{"type": "agentMessage", "text": body}],
                        }
                    },
                }
            )
            continue
        if method in {"thread/archive", "thread/unsubscribe"}:
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
