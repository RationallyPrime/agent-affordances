"""Shared pool/entitlement refusal classifier for oneshot and daemon.

One predicate, invoked once per error: ``classify_code`` for a structured
JSON-RPC/HTTP code, ``classify_refusal`` for text. Nothing re-classifies a
message a caller has already rendered — that reintroduces the rendered-text
haystack the codes exist to replace.

Two sub-cases, one class: auth-expiry vs pool/rate-limit. Both transports
call this so telemetry ``status`` is comparable across ``transport``.

Numeric ``401``/``403``/``429`` match only in a status context, and only
when nothing word-like follows: ``status 401`` and ``server returned 429``
are refusals; ``returned 401 rows``, ``code 403 chunks pending``,
``/403/``, ``file.py:403:`` and ``turn took 403 ms`` are not.

**Decided, not accidental:** a bare ``<context word> <code>`` with nothing
after it *is* classified as a refusal even when the number is plainly a
count — ``exit code 429`` classifies as a pool refusal. On the unstructured
path there is no way to tell the two apart from the text alone, and the two
errors are not symmetric: a refusal that falls through is retried against a
metered pool, while a false refusal is a loud, terminal message that names
the exact remedy. We take the false refusal.
"""

from __future__ import annotations

import re
from typing import Literal

type RefusalKind = Literal["auth", "unavailable"]

AUTH_WORD_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "not authenticated",
    "auth expired",
    "token expired",
    "login required",
    "re-authenticate",
    "not logged in",
)
POOL_WORD_MARKERS = (
    "usage limit",
    "rate limit",
    "too many requests",
)
# Structured JSON-RPC / HTTP codes. Never match these as substrings of a
# rendered error — a code of -32403 or a path like error403.py is not auth.
AUTH_CODES = {401, 403}
POOL_CODES = {429}

# Three shapes, in order:
#   1. a context word, the code, and nothing word-like after it. The
#      lookbehind stops ``src/http/error403.py`` matching on ``http`` +
#      ``/error`` + ``403``; the trailing lookahead stops ``returned 401
#      rows`` and ``code 403 chunks pending`` — a count, not a status.
#   2. the code followed by its HTTP reason phrase (``403 Forbidden``).
#      ``unauthorized`` is absent on purpose: it is an AUTH_WORD_MARKER, so
#      classify_refusal returns before this regex ever runs.
#   3. ``429`` followed by a throttle phrase.
_STATUS_CTX = re.compile(
    r"(?:status|http|code|returned|response|error)\W{0,4}"
    r"(?<![A-Za-z0-9_])(?:40[13]|429)(?![A-Za-z0-9_])(?!\s+[A-Za-z])"
    r"|(?<![A-Za-z0-9_])40[13]\W{0,4}forbidden"
    r"|(?<![A-Za-z0-9_])429\W{0,4}(?:too many|rate)",
    re.IGNORECASE,
)


def classify_code(code: object) -> RefusalKind | None:
    if not isinstance(code, int):
        return None
    if code in AUTH_CODES:
        return "auth"
    if code in POOL_CODES:
        return "unavailable"
    return None


def classify_refusal(text: str) -> RefusalKind | None:
    lowered = text.lower()
    if any(marker in lowered for marker in AUTH_WORD_MARKERS):
        return "auth"
    if any(marker in lowered for marker in POOL_WORD_MARKERS):
        return "unavailable"
    match = _STATUS_CTX.search(text)
    if match is None:
        return None
    return "unavailable" if "429" in match.group(0) else "auth"


def refusal_message(kind: RefusalKind) -> str:
    if kind == "auth":
        return (
            "Spark auth expired or missing — re-authenticate the "
            "Codex CLI (`codex login`). This is terminal: no retry."
        )
    return (
        "Spark pool or entitlement refused the call — usage or rate limit. "
        "This is terminal: no retry."
    )
