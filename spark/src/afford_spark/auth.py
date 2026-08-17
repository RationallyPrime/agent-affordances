"""Shared pool/entitlement refusal classifier for oneshot and daemon.

One predicate, two sub-cases: auth-expiry vs pool/rate-limit. Both
transports call this so telemetry ``status`` is comparable across
``transport``. Numeric ``401``/``403``/``429`` match only in a status
context — a path segment, a ``file.py:403:`` reference, or a count is
not a refusal.
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
)
# Structured JSON-RPC / HTTP codes. Never match these as substrings of a
# rendered error — a code of -32403 or a path like error403.py is not auth.
AUTH_CODES = {401, 403}
POOL_CODES = {429}

# Left-hand lookbehind inside the first alternation stops
# ``src/http/error403.py`` matching on ``http`` + ``/error`` + ``403``.
_STATUS_CTX = re.compile(
    r"(?:status|http|code|returned|response|error)\W{0,4}"
    r"(?<![A-Za-z0-9_])(?:40[13]|429)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])40[13]\W{0,4}(?:unauthorized|forbidden)"
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
            "Codex CLI (`codex login`). The warm daemon will not retry."
        )
    return (
        "Spark pool or entitlement refused the call — usage or rate limit. "
        "The warm daemon will not retry."
    )
