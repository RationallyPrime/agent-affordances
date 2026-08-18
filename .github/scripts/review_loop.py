#!/usr/bin/env python3
"""Portable GitHub-to-Hive review-loop router.

The cx53 self-hosted runners deliberately have a small host tool surface.  This
helper uses only Python's standard library and the credentials GitHub Actions
already injects; it does not depend on ``gh``, ``jq``, or a package manager.

Commands:

``route``
    Route one native Codex result: a submitted findings review goes to Talos;
    a bot-authored clean PR comment — in either verdict format Codex emits —
    goes to the authoring seat.  A Codex comment that looks like a verdict but
    cannot be routed is reported to Hive rather than dropped.  At the
    exhaustion gate the authoring seat gets the human gate and Theoros
    additionally gets a retrospective wake carrying the per-round history.
``nudge``
    Add one ``@codex review`` comment (with an exact-head marker) per stalled,
    unreviewed head until the bounded automatic loop is exhausted, and
    re-deliver a standing verdict once per authoritative same-head verdict
    whose seat never acted.  A wake fires exactly once from an event-driven
    path; a consumed
    wake with no new GitHub event is otherwise silence forever.  The scheduled
    exhaustion path posts the same human gate as ``route`` but does not wake
    Theoros: the retrospective requires findings still present on the exact
    current head, and this branch is entered only because that head has none.
``canary``
    Post a harmless Talos liveness wake for end-to-end verification.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

CODEX_LOGIN = "chatgpt-codex-connector[bot]"
WORKFLOW_LOGIN = "github-actions[bot]"
MAX_REVIEW_ROUNDS = 5
REVIEW_STALL_SECONDS = 20 * 60
# Deliberately separate from REVIEW_STALL_SECONDS: that window bounds how long a
# head may wait for a *verdict*, while this one bounds how long a delivered
# verdict may sit unacted.  Burns routinely run past twenty minutes, so reusing
# the nudge window would wake a working seat mid-burn every scan.  At an hour,
# the worst case is one redundant wake, which seat dedupe doctrine makes safe.
WAKE_REDELIVERY_SECONDS = 60 * 60
DIGEST_CHUNK_BUDGET = 12000
ROUND_LOCATION_LINE_BUDGET = 8000
EXHAUSTED_MARKER_PREFIX = "<!-- weave-review-loop:exhausted:"
NUDGE_MARKER_PREFIX = "<!-- weave-review-loop:nudge:"
REDELIVERY_MARKER_PREFIX = "<!-- weave-review-loop:redelivered:"
HEAD_BASE_MARKER_PREFIX = "<!-- weave-review-loop:head-base:"
# Machine markers are versioned (KRA-1122 class): writers emit the ``v2:``
# forms below, and every reader also accepts the unversioned legacy form so
# markers already standing on open PRs keep suppressing what they suppressed.
# A policy-bearing marker (exhaustion) additionally records the policy value
# in force when it was written; a reader whose current policy differs must
# not treat it as terminal — the stale-three-round-marker incident.
MARKER_SCHEMA_VERSION = "v2"
HEAD_BASE_MARKER_RE = re.compile(
    r"<!-- weave-review-loop:head-base:([^:\s]+):([^:\s]+) -->"
)
HEAD_BASE_MARKER_V2_RE = re.compile(
    r"<!-- weave-review-loop:head-base:v2:([^:\s]+):([^:\s]+):([^:\s]+) -->"
)
EXHAUSTED_MARKER_RE = re.compile(
    r"<!-- weave-review-loop:exhausted:"
    r"(?:v2:([^:\s]+):max-rounds=(\d+)|([^:\s]+)) -->"
)
PRODUCT_GATE_MARKER_PREFIX = "<!-- weave-review-loop:product-gate:"
NOISE_MARKER_PREFIX = "<!-- weave-review-loop:noise:"
SUBSTITUTE_SUMMON_MARKER_PREFIX = "<!-- weave-review-loop:substitute-summon:"
SUBSTITUTE_VERDICT_MARKER_PREFIX = "<!-- weave-review-loop:substitute-verdict:"
# head : actor : clean | findings:<p1>:<p2>:<p3>.  The marker — not the prose
# around it — is the machine contract: substitute reviewers are seats, their
# prose formats drift, and parsing prose is the defect class the belt audit
# named.  Full 40-hex head only: a short sha cannot be exact-head evidence.
# The actor token is shared with ``normalize_substitute_actor`` so a configured
# seat cannot be summoned into an unparseable marker.
SUBSTITUTE_ACTOR_TOKEN = r"[a-z0-9-]+"
SUBSTITUTE_ACTOR_RE = re.compile(rf"^{SUBSTITUTE_ACTOR_TOKEN}$")
SUBSTITUTE_VERDICT_MARKER_RE = re.compile(
    r"<!-- weave-review-loop:substitute-verdict:"
    rf"([0-9a-fA-F]{{40}}):({SUBSTITUTE_ACTOR_TOKEN}):"
    r"(clean|findings:\d+:\d+:\d+) -->"
)
# The connector's account-wide quota refusal (2026-08-16 outage shape).  It is
# not CODEX_CONNECTOR_ERROR_PREFIX ("To use Codex here"), which is the
# unconnected-repo error; this one arrives on connected repos, auto-fires on
# pushes and comments, and means the find half is down until credits or reset.
CODEX_QUOTA_REFUSAL_PREFIX = "You have reached your Codex usage limits"
AI_USAGE_DEFAULT_THRESHOLD = 0.9
DEFAULT_SUBSTITUTE_ACTOR = "theoros"
FINDING_IDENTITY_MAX = 96
_IDENTITY_NOISE = re.compile(
    r"</?sub>|!\[[^\]]*\]\([^)]*\)|\[P[123]-[A-Za-z]+\]|\*{1,2}|_{1,2}"
)
UNRESOLVED_CLEAN_PREFIX = "unresolved-clean:"
CODEX_CLEAN_COMMENT_PREFIX = "Codex Review: Didn't find any major issues."
CODEX_RESULT_HEADING = "## Review Result"
CODEX_RESULT_CLEAN_PHRASE = "no blocking findings"
# A lone period or bang ends a sentence.  Ellipsis is continuation — the
# prefix check `tail[0] in ".!"` treated the first dot of "..." as a stop.
_SENTENCE_END = re.compile(r"(?<!\.)\.(?!\.)|!")
CODEX_TASK_REPORT_HEADING = "### Summary"
CODEX_CONNECTOR_ERROR_PREFIX = "To use Codex here"
REVIEWED_COMMIT_PATTERN = re.compile(
    r"\*\*Reviewed commit:\*\*\s*`([0-9a-fA-F]{7,40})`"
)
# Codex anchors every claim in a task-channel verdict to a file permalink at the
# exact tree it read.  ``commit``/``pull`` links are deliberately excluded: they
# reference a commit under discussion, not the tree the verdict was formed on.
PERMALINK_COMMIT_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/(?:blob|blame)/([0-9a-fA-F]{40})/"
)
CODEX_COMMENT_CLEAN = "clean"
CODEX_COMMENT_TASK_VERDICT = "task-verdict"
CODEX_COMMENT_TASK_REPORT = "task-report"
CODEX_COMMENT_CONNECTOR_ERROR = "connector-error"
CODEX_COMMENT_QUOTA_REFUSAL = "quota-refusal"
CODEX_COMMENT_UNKNOWN = "unknown"
# Sentinel: ``find_half_route`` / ``_scan_open_pull`` fetch the meter themselves
# unless the scheduled scan supplies the one reading it already took.
_FETCH_METER = object()
SEAT_ACTORS = {
    "Fable": "fable",
    "Ariadne": "ariadne",
    "gnomon": "gnomon",
    "Talos": "talos",
    "Theoros": "theoros",
}


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable {name} is missing")
    return value


class ApiHttpError(RuntimeError):
    """HTTP failure with a machine-checkable status and bounded message."""

    def __init__(self, method: str, path: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"{method} {path} failed with HTTP {status_code}")


class JsonApi:
    """Small authenticated JSON HTTP client with bounded error output."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        authorization_scheme: str = "Bearer",
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.authorization_scheme = authorization_scheme
        self.extra_headers = dict(extra_headers or {})

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"{self.authorization_scheme} {self.token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "weave-doctrine-review-loop",
            **self.extra_headers,
        }
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            # Never echo response bodies: they can contain private PR text or
            # provider diagnostics.  The status and endpoint are enough to act.
            raise ApiHttpError(method, path, error.code) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"{method} {path} failed: {error.reason}") from error
        return json.loads(raw) if raw else None


class GitHubApi(JsonApi):
    def __init__(self, token: str, repository: str) -> None:
        super().__init__(
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            token,
            extra_headers={"X-GitHub-Api-Version": "2022-11-28"},
        )
        self.repository = repository

    def repo_path(self, suffix: str) -> str:
        return f"repos/{self.repository}/{suffix.lstrip('/')}"

    def authenticated_login(self) -> str:
        user = self.request("GET", "user")
        if not isinstance(user, Mapping) or not str(user.get("login") or "").strip():
            raise RuntimeError("GitHub authenticated-user lookup returned no login")
        return str(user["login"])

    def get(self, suffix: str, *, query: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", self.repo_path(suffix), query=query)

    def post(self, suffix: str, payload: Mapping[str, Any]) -> Any:
        return self.request("POST", self.repo_path(suffix), payload=payload)

    def paginate(
        self, suffix: str, *, query: Mapping[str, Any] | None = None
    ) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            page_query = {**dict(query or {}), "per_page": 100, "page": page}
            batch = self.get(suffix, query=page_query)
            if not isinstance(batch, list):
                raise TypeError(f"GET {suffix} did not return a list")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1


class SlackApi(JsonApi):
    def __init__(self, token: str, channel: str) -> None:
        super().__init__(
            "https://slack.com/api",
            token,
            extra_headers={"Accept": "application/json"},
        )
        self.channel = channel

    def post_message(self, text: str, *, thread_ts: str | None = None) -> str:
        payload: dict[str, Any] = {"channel": self.channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = self.request(
            "POST",
            "chat.postMessage",
            payload=payload,
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = (
                result.get("error", "unknown_error")
                if isinstance(result, dict)
                else "invalid_response"
            )
            raise RuntimeError(f"Slack chat.postMessage failed: {error}")
        ts = result.get("ts")
        if not isinstance(ts, str) or not ts:
            raise RuntimeError("Slack chat.postMessage returned no ts")
        return ts


INERT_WAKE_QUOTE = "> "
EVIDENCE_HEADING_SEPARATOR = "\n## "


def neutralize_wake_lines(text: str) -> str:
    """Quote every line Hive's ``parseAddressedWake`` would read as an envelope.

    Evidence published before the commit-point WAKE must be inert: the parser
    scans every line of every message — thread replies included — so an
    embedded ``WAKE: talos`` inside a quoted finding body would dispatch the
    seat against a partially published digest, the exact race the
    evidence-first protocol exists to close.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith(("WAKE:", "NEXT ")):
            lines.append(f"{INERT_WAKE_QUOTE}{line}")
        else:
            lines.append(line)
    return "\n".join(lines)


def wake_instruction(message: str) -> str:
    """The instruction half of a wake message — everything before the evidence.

    Every chunked wake builder ends its header with exactly one ``## `` evidence
    heading (a doctrine test pins this for both builders); the digest blocks
    after it are data, not instruction.  The commit-point message must carry the
    full instruction — the skill tag, the verdict counts, the coordinates, and
    the doctrine text — because the addressed message is the only one the seat
    is dispatched on; a commit point reduced to coordinates dispatches a burn
    with zero findings in its instruction body.
    """
    return message.split(EVIDENCE_HEADING_SEPARATOR, 1)[0].rstrip()


def evidence_root_notice(envelope: str, chunk_count: int) -> str:
    """The inert thread root. Must never start a line with WAKE:/NEXT."""
    return (
        f"Review-loop: publishing {chunk_count} evidence message(s) for "
        f"`{envelope}` in this thread. The addressed wake posts LAST, after "
        "every evidence message lands; until then nothing in this thread is a "
        "dispatch, and a failed publication leaves no wake at all."
    )


def publication_commit_message(chunk_count: int, header: str) -> str:
    """The final addressed message — the dispatch commit point.

    Carries the wake's full instruction (envelope, skill tag, verdict counts,
    coordinates, doctrine), stripped only of the digest blocks: those are the
    evidence messages above it in the thread, which the seat reads as data.
    """
    return (
        f"{wake_instruction(header)}\n\n"
        f"Evidence publication complete — the full digest is the {chunk_count} "
        "evidence message(s) above in this thread (envelope lines quoted "
        "inert). Read the whole thread before acting; this message is the "
        "dispatch commit point."
    )


def post_threaded_messages(slack: SlackApi, messages: Sequence[str]) -> None:
    """Publish a wake atomically: evidence first, the addressed WAKE last.

    Hive binds a seat to the thread of a WAKE delivery, and it can claim the
    first addressed message before later chunks exist — so a multi-message
    wake posted WAKE-first can dispatch a seat against a partial digest, and
    a failed continuation leaves a live but incomplete instruction.

    A single-message wake needs no protocol and posts as before.  A chunked
    wake posts an inert thread root, then every chunk with its envelope lines
    neutralized, and only after all of them succeed a final small addressed
    message carrying the envelope and the belt subject.  Any earlier post
    failing raises out (R-3) and no WAKE is ever published.
    """
    if not messages:
        return
    if len(messages) == 1:
        slack.post_message(messages[0])
        return
    envelope = wake_envelope_line(messages[0])
    thread_ts = slack.post_message(evidence_root_notice(envelope, len(messages)))
    for message in messages:
        slack.post_message(neutralize_wake_lines(message), thread_ts=thread_ts)
    slack.post_message(
        publication_commit_message(len(messages), messages[0]),
        thread_ts=thread_ts,
    )


def load_event() -> dict[str, Any]:
    path = required_env("GITHUB_EVENT_PATH")
    with open(path, encoding="utf-8") as event_file:
        event = json.load(event_file)
    if not isinstance(event, dict):
        raise TypeError("GitHub event payload is not an object")
    return event


def commit_author_name(commit: Mapping[str, Any]) -> str:
    nested = commit.get("commit")
    if not isinstance(nested, Mapping):
        return ""
    author = nested.get("author")
    return str(author.get("name") or "") if isinstance(author, Mapping) else ""


def choose_author(names: Iterable[str], fallback: str = "") -> str:
    """Choose the original non-Talos seat, then Talos, then HEAD author."""
    first_talos = ""
    for name in names:
        if name in {"Fable", "Ariadne", "gnomon", "Theoros"}:
            return name
        if name == "Talos" and not first_talos:
            first_talos = name
    return first_talos or fallback


def author_for_pr(
    github: GitHubApi, pr_number: int, head_sha: str
) -> tuple[str, str] | None:
    commits = github.paginate(f"pulls/{pr_number}/commits")
    author = choose_author(commit_author_name(item) for item in commits)
    if not author:
        # A queued review can outlive its HEAD SHA after a force-push or fork
        # deletion. Do not let a needless fallback lookup suppress a seat that
        # the surviving PR commit list already identified.
        author = commit_author_name(github.get(f"commits/{head_sha}"))
    actor = SEAT_ACTORS.get(author)
    return (author, actor) if actor else None


def is_quota_refusal_body(body: str) -> bool:
    """True when this text is the connector's account-wide quota refusal.

    One predicate for both channels (submitted review and issue comment).
    A refusal is not a verdict: it must not enter the result stream, must
    not route as CLEAN or findings, and must not count as a reviewed head.
    """
    return str(body or "").lstrip().startswith(CODEX_QUOTA_REFUSAL_PREFIX)


def review_findings(
    comments: Sequence[Mapping[str, Any]], review_id: int, codex_login: str
) -> list[dict[str, Any]]:
    """Select one review's inline Codex findings from the PR's comment list.

    Bind on ``pull_request_review_id``, never on ``commit_id == head``.
    GitHub repositions older unresolved comments onto the live head, so a
    ``commit_id`` filter mixes leftover rounds into the current verdict.

    The comment list is fetched once and filtered per review, so building a
    whole-PR round history costs no additional API calls.
    """
    findings: list[dict[str, Any]] = []
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") != codex_login:
            continue
        if comment.get("pull_request_review_id") != review_id:
            continue
        findings.append(
            {
                "path": comment.get("path") or "?",
                "line": comment.get("line") or comment.get("original_line") or "?",
                "body": str(comment.get("body") or ""),
            }
        )
    return findings


def severity_counts(findings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"p1": 0, "p2": 0, "p3": 0, "total": len(findings)}
    for finding in findings:
        body = str(finding.get("body") or "")
        if re.search(r"P1-orange", body):
            counts["p1"] += 1
        elif re.search(r"P2-yellow", body):
            counts["p2"] += 1
        else:
            # P3-tagged and unmarked inline comments both remain findings.
            counts["p3"] += 1
    return counts


def codex_review_heads(
    reviews: Sequence[Mapping[str, Any]], codex_login: str
) -> list[str]:
    """Return distinct reviewed commit ids in review order."""
    heads: list[str] = []
    for review in reviews:
        user = review.get("user")
        if not isinstance(user, Mapping) or user.get("login") != codex_login:
            continue
        if is_quota_refusal_body(str(review.get("body") or "")):
            continue
        head_sha = str(review.get("commit_id") or "").strip()
        if head_sha and head_sha not in heads:
            heads.append(head_sha)
    return heads


def _opening_sentence(statement: str) -> str:
    """First sentence of a verdict line.

    Ellipsis (``...`` / ``…``) continues the sentence; a lone ``.`` or ``!``
    ends it.  Keying on this boundary — not on a prefix of the line — is what
    keeps ``No blocking findings... yet`` from counting as a clean verdict.
    """
    normalized = statement.replace("…", "...")
    match = _SENTENCE_END.search(normalized)
    if match is None:
        return normalized
    return normalized[: match.end()]


def task_verdict_is_clean(body: str) -> bool:
    """True only when the opening *sentence* is the clean assertion.

    The verdict is the first sentence under ``## Review Result``, not a prefix
    of that sentence.  ``No blocking findings could be ruled out.`` and
    ``No blocking findings... yet`` share the clean phrase as an opening but
    are different sentences; treating either as CLEAN would hand merge
    authority to a verdict Codex withheld.

    Every real task-channel verdict observed opens exactly
    ``No blocking findings.`` before elaborating.
    """
    _, _, remainder = body.partition(CODEX_RESULT_HEADING)
    for line in remainder.splitlines():
        statement = line.strip().lstrip("*_->#").strip().lower()
        if not statement:
            continue
        opening = _opening_sentence(statement)
        # Emphasis is markup, not sentence structure.  Closing markers and a
        # real terminator may arrive in either order (``**phrase**.`` vs
        # ``**phrase.**``); peel both until stable.  Ellipsis is
        # continuation, never a stop, so it is not stripped.
        has_stop = _SENTENCE_END.search(opening) is not None
        asserted = opening
        while True:
            nxt = asserted.rstrip("*_ ").strip()
            if has_stop and not nxt.endswith("..."):
                nxt = nxt.rstrip(".! ").strip()
            if nxt == asserted:
                break
            asserted = nxt
        return asserted == CODEX_RESULT_CLEAN_PHRASE
    return False


def codex_comment_kind(body: str) -> str:
    """Classify one Codex-authored PR comment into the loop's result vocabulary.

    Codex speaks through two channels.  The review connector emits
    ``CODEX_CLEAN_COMMENT_PREFIX`` with a declared ``**Reviewed commit:**``.  A
    Codex *task* asked to review the PR emits ``## Review Result`` and anchors
    its claims in file permalinks instead.  Both are verdicts.

    A task-channel verdict that is not affirmatively clean is ``task-verdict``:
    findings delivered that way reach neither ``route_review`` nor the burn
    twin, so the loop has no route for them and must say so.

    A task that *changed* code reports under ``### Summary``; that is a work
    report, never a verdict — its permalinks point at the tree the task read
    before committing on top of it, so its head is stale by construction.

    Everything else is ``unknown`` on purpose: an unrecognised shape must be
    reported, not silently dropped, which is the defect this vocabulary closes.
    """
    stripped = body.lstrip()
    if stripped.startswith(CODEX_CONNECTOR_ERROR_PREFIX):
        return CODEX_COMMENT_CONNECTOR_ERROR
    if is_quota_refusal_body(stripped):
        return CODEX_COMMENT_QUOTA_REFUSAL
    if stripped.startswith(CODEX_CLEAN_COMMENT_PREFIX):
        return CODEX_COMMENT_CLEAN
    if stripped.startswith(CODEX_RESULT_HEADING):
        if task_verdict_is_clean(stripped):
            return CODEX_COMMENT_CLEAN
        return CODEX_COMMENT_TASK_VERDICT
    if stripped.startswith(CODEX_TASK_REPORT_HEADING):
        return CODEX_COMMENT_TASK_REPORT
    return CODEX_COMMENT_UNKNOWN


def permalink_commit(body: str, repository: str) -> str | None:
    """Return the one commit this body's own-repository permalinks all pin.

    Two distinct SHAs mean the comment spans trees, so no single reviewed head
    can be claimed.  Links into other repositories are ignored outright — a SHA
    from elsewhere is not evidence about this pull request.
    """
    shas = {
        match.group(2).lower()
        for match in PERMALINK_COMMIT_PATTERN.finditer(body)
        if match.group(1).lower() == repository.lower()
    }
    if len(shas) != 1:
        return None
    return shas.pop()


def comment_commit_reference(body: str, repository: str) -> str | None:
    """Any commit this comment points at, whatever the comment's shape."""
    match = REVIEWED_COMMIT_PATTERN.search(body)
    if match:
        return match.group(1).lower()
    for permalink in PERMALINK_COMMIT_PATTERN.finditer(body):
        if permalink.group(1).lower() == repository.lower():
            return permalink.group(2).lower()
    return None


def clean_comment_commitish(
    comment: Mapping[str, Any], codex_login: str, repository: str
) -> str | None:
    """Extract the reviewed commit from a Codex clean verdict in either format."""
    user = comment.get("user")
    if not isinstance(user, Mapping) or user.get("login") != codex_login:
        return None
    body = str(comment.get("body") or "")
    if codex_comment_kind(body) != CODEX_COMMENT_CLEAN:
        return None
    match = REVIEWED_COMMIT_PATTERN.search(body)
    if match:
        return match.group(1).lower()
    return permalink_commit(body, repository)


def clean_comment_head(
    github: GitHubApi, comment: Mapping[str, Any], codex_login: str
) -> str | None:
    """Resolve Codex's displayed short commit to one exact repository SHA."""
    commitish = clean_comment_commitish(comment, codex_login, github.repository)
    if commitish is None:
        return None
    if len(commitish) == 40:
        return commitish
    try:
        commit = github.get(f"commits/{commitish}")
    except ApiHttpError as error:
        # A stale force-pushed commit may no longer resolve. It cannot establish
        # current-head closure, but every transient or authority failure must
        # fail visibly so the workflow retries instead of consuming a round.
        if error.status_code == 404:
            return None
        raise
    if not isinstance(commit, Mapping):
        raise TypeError("GitHub commit lookup returned an invalid response")
    resolved = str(commit.get("sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise RuntimeError("GitHub commit lookup returned an invalid commit id")
    return resolved


def codex_result_events(
    github: GitHubApi,
    reviews: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    codex_login: str,
) -> list[tuple[datetime, int, str, str]]:
    """Return time-ordered Codex results as ``(time, order, head, kind)``.

    ``kind`` is ``findings`` for a submitted review and ``clean`` for a clean
    comment.  Both channels are retained — including a later CLEAN on a head
    that already had findings — so a retrospective can pick the later verdict
    without treating the earlier review as current.

    A stale short commit can become unresolvable after history changes. It still
    consumed an automatic review round, so retain a namespaced short key for the
    bounded-loop count without ever treating it as exact-head closure.

    Finding-reviews and clean comments are independent channels. Listing one
    channel then the other is not chronological, so a retrospective would number
    a later findings head before an earlier CLEAN. Merge by timestamp before
    treating the list as round order.
    """
    events: list[tuple[datetime, int, str, str]] = []
    for review in reviews:
        user = review.get("user")
        if not isinstance(user, Mapping) or user.get("login") != codex_login:
            continue
        if is_quota_refusal_body(str(review.get("body") or "")):
            continue
        head_sha = str(review.get("commit_id") or "").strip()
        if not head_sha:
            continue
        events.append(
            (
                result_event_time(review.get("submitted_at")),
                len(events),
                head_sha,
                "findings",
            )
        )
    for comment in comments:
        commitish = clean_comment_commitish(comment, codex_login, github.repository)
        if commitish is None:
            continue
        resolved = clean_comment_head(github, comment, codex_login)
        result_head = resolved or f"{UNRESOLVED_CLEAN_PREFIX}{commitish}"
        events.append(
            (
                result_event_time(comment.get("created_at")),
                len(events),
                result_head,
                "clean",
            )
        )
    for _, verdict in substitute_verdicts(comments):
        events.append(
            (
                verdict["at"],
                len(events),
                verdict["head"],
                verdict["kind"],
            )
        )
    events.sort()
    return events


def substitute_verdicts(
    comments: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], dict[str, Any]]]:
    """Trusted substitute exact-head verdicts, as ``(comment, parsed)`` pairs.

    A substitute verdict is a PR comment by a trusted control identity carrying
    the ``substitute-verdict`` marker.  The marker is the whole contract — full
    head SHA, the substitute actor, and ``clean`` or ``findings:p1:p2:p3`` —
    because seats' verdict prose drifts and the loop must never parse it.  An
    untrusted author's marker is a forgery and never counts, same trust rule as
    every other control marker.
    """
    trusted = trusted_control_logins()
    verdicts: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") not in trusted:
            continue
        match = SUBSTITUTE_VERDICT_MARKER_RE.search(str(comment.get("body") or ""))
        if match is None:
            continue
        counts = {"p1": 0, "p2": 0, "p3": 0}
        if match.group(3) != "clean":
            # The marker, not the prose, is the machine contract — severity
            # counts parse from it deterministically.
            _, p1, p2, p3 = match.group(3).split(":")
            counts = {"p1": int(p1), "p2": int(p2), "p3": int(p3)}
        verdicts.append(
            (
                comment,
                {
                    "at": result_event_time(comment.get("created_at")),
                    "head": match.group(1).lower(),
                    "actor": match.group(2),
                    "kind": "clean" if match.group(3) == "clean" else "findings",
                    "counts": counts,
                },
            )
        )
    return verdicts


def standing_substitute_findings(
    comments: Sequence[Mapping[str, Any]], head_sha: str
) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """The latest substitute FINDINGS verdict for this head, with its comment.

    The comment body IS the digest — the marker contract requires the
    substitute to carry the findings in the verdict comment itself, and the
    loop never re-parses seat prose into structure.
    """
    latest: tuple[Mapping[str, Any], dict[str, Any]] | None = None
    for comment, verdict in substitute_verdicts(comments):
        if verdict["head"] != head_sha.lower() or verdict["kind"] != "findings":
            continue
        if latest is None or verdict["at"] > latest[1]["at"]:
            latest = (comment, verdict)
    return latest


def result_heads_from_events(
    events: Sequence[tuple[datetime, int, str, str]],
) -> list[str]:
    """Distinct heads in first-seen time order; a later event does not add a round."""
    heads: list[str] = []
    for _, _, head, _ in events:
        if head not in heads:
            heads.append(head)
    return heads


def latest_result_kind_by_head(
    events: Sequence[tuple[datetime, int, str, str]],
) -> dict[str, str]:
    """Later of findings-review vs clean-comment for each head."""
    latest: dict[str, tuple[datetime, int, str]] = {}
    for at, order, head, kind in events:
        previous = latest.get(head)
        if previous is None or (at, order) >= (previous[0], previous[1]):
            latest[head] = (at, order, kind)
    return {head: kind for head, (_, _, kind) in latest.items()}


def codex_result_heads(
    github: GitHubApi,
    reviews: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    codex_login: str,
) -> list[str]:
    """Return distinct finding-review and clean-comment heads in time order."""
    return result_heads_from_events(
        codex_result_events(github, reviews, comments, codex_login)
    )


def result_round_for_head(reviewed_heads: Sequence[str], head_sha: str) -> int:
    """Return this head's one-based round within distinct Codex results."""
    heads = list(reviewed_heads)
    if head_sha not in heads:
        heads.append(head_sha)
    return len(heads)


def review_round_for_head(
    reviews: Sequence[Mapping[str, Any]], head_sha: str, codex_login: str
) -> int:
    return result_round_for_head(codex_review_heads(reviews, codex_login), head_sha)


def _substitute_history_by_head(
    issue_comments: Sequence[Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Latest trusted substitute verdict per head, with its comment body.

    The marker is the machine contract (kind + counts). The comment body is
    retained as the digest so a retrospective can see earlier substitute
    rounds; seat prose is never re-parsed into structured findings.
    """
    latest: dict[str, dict[str, Any]] = {}
    if not issue_comments:
        return latest
    for comment, verdict in substitute_verdicts(issue_comments):
        head = str(verdict["head"])
        previous = latest.get(head)
        if previous is None or verdict["at"] > previous["at"]:
            counts = dict(verdict["counts"])
            latest[head] = {
                "at": verdict["at"],
                "actor": verdict["actor"],
                "kind": verdict["kind"],
                "counts": {
                    "p1": int(counts.get("p1", 0)),
                    "p2": int(counts.get("p2", 0)),
                    "p3": int(counts.get("p3", 0)),
                },
                "body": str(comment.get("body") or ""),
            }
            latest[head]["counts"]["total"] = (
                latest[head]["counts"]["p1"]
                + latest[head]["counts"]["p2"]
                + latest[head]["counts"]["p3"]
            )
    return latest


def round_history(
    reviewed_heads: Sequence[str],
    reviews: Sequence[Mapping[str, Any]],
    review_comments: Sequence[Mapping[str, Any]],
    codex_login: str,
    *,
    latest_kind_by_head: Mapping[str, str] | None = None,
    head_bases: Mapping[str, str] | None = None,
    issue_comments: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Describe every consumed round in round order: head, verdict, findings.

    A retrospective needs the shape of the whole sequence, so this covers the
    same head set the bounded round count is drawn from — clean-comment rounds
    included.  A round whose inline comments are not retrievable is never
    reported as CLEAN: only a Codex clean comment establishes that, and an
    absence establishes nothing.  When a later clean comment supersedes an
    earlier findings review on the same unchanged head, the later CLEAN is
    the verdict.  Reviews are deduplicated by id because the submitted review
    under routing is also present in the paginated list.  Only the latest
    findings review per head supplies the digest: an earlier review's comments
    on the same unchanged head are superseded by the later review's own
    complete assessment (an intervening CLEAN may have resolved them), so
    merging rounds would hand Theoros findings that are no longer live.
    """
    latest_review_by_head: dict[str, tuple[datetime, int, int]] = {}
    seen_reviews: set[int] = set()
    for review in reviews:
        user = review.get("user")
        if not isinstance(user, Mapping) or user.get("login") != codex_login:
            continue
        if is_quota_refusal_body(str(review.get("body") or "")):
            continue
        head = str(review.get("commit_id") or "").strip()
        review_id = review.get("id")
        if not head or review_id is None or int(review_id) in seen_reviews:
            continue
        seen_reviews.add(int(review_id))
        candidate = (
            result_event_time(review.get("submitted_at")),
            len(seen_reviews),
            int(review_id),
        )
        previous = latest_review_by_head.get(head)
        if previous is None or candidate[:2] >= previous[:2]:
            latest_review_by_head[head] = candidate
    findings_by_head: dict[str, list[dict[str, Any]]] = {
        head: review_findings(review_comments, review_id, codex_login)
        for head, (_, _, review_id) in latest_review_by_head.items()
    }
    latest_kind = dict(latest_kind_by_head or {})
    bases = dict(head_bases or {})
    substitute_by_head = _substitute_history_by_head(issue_comments)
    history: list[dict[str, Any]] = []
    for index, head in enumerate(reviewed_heads, start=1):
        findings = list(findings_by_head.get(head, []))
        kind = latest_kind.get(head)
        digest = ""
        if head.startswith(UNRESOLVED_CLEAN_PREFIX) or kind == "clean":
            substitute_round = substitute_by_head.get(str(head).lower())
            if head.startswith(UNRESOLVED_CLEAN_PREFIX):
                verdict = "CLEAN (clean comment whose commit no longer resolves)"
            elif substitute_round is not None and substitute_round["kind"] == "clean":
                # A substitute CLEAN is not a Codex clean comment: the
                # retrospective compares reviewer/repair sequences, so false
                # provenance here is exactly where it misleads.
                verdict = f"CLEAN (substitute {substitute_round['actor']} verdict)"
            else:
                verdict = "CLEAN (Codex clean comment)"
            findings = []
            counts = severity_counts(findings)
            locations = []
        elif kind == "findings" and head not in findings_by_head:
            # A substitute findings verdict: no Codex review exists for this
            # head, so absence from the review map is NOT clean-comment
            # evidence. The digest lives in the substitute's verdict comment;
            # the counts are in its marker and its prose is never re-parsed.
            substitute = substitute_by_head.get(str(head).lower())
            if substitute is not None:
                actor = substitute["actor"]
                verdict = (
                    f"findings verdict (substitute {actor}; "
                    "digest in the verdict comment)"
                )
                counts = dict(substitute["counts"])
                digest = str(substitute["body"])
            else:
                verdict = (
                    "findings verdict (substitute marker; "
                    "digest in the verdict comment)"
                )
                counts = {"p1": 0, "p2": 0, "p3": 0, "total": 0}
            locations = []
        elif head not in findings_by_head:
            verdict = "CLEAN (Codex clean comment)"
            counts = severity_counts([])
            locations = []
        elif findings:
            verdict = "findings review"
            counts = severity_counts(findings)
            locations = [history_location(finding) for finding in findings]
        else:
            verdict = (
                "findings review submitted, 0 inline comments retrievable "
                "(not evidence of CLEAN)"
            )
            counts = severity_counts(findings)
            locations = []
        history.append(
            {
                "round": index,
                "rounds": len(reviewed_heads),
                "head": head,
                "verdict": verdict,
                "counts": counts,
                "locations": locations,
                "base": bases.get(head),
                "digest": digest,
            }
        )
    return history


def finding_identity(body: str, *, max_len: int = FINDING_IDENTITY_MAX) -> str:
    """First significant line of a finding: badges and severity tags stripped.

    Path equality is not identity. The burn wake must carry this so three
    unrelated findings in one file are not treated as the same gate.
    """
    if max_len < 1:
        raise ValueError("max_len must be at least 1")
    for raw in str(body).splitlines():
        line = _IDENTITY_NOISE.sub("", raw)
        line = re.sub(r"\s+", " ", line).strip(" \t-*#_")
        if not line:
            continue
        if len(line) > max_len:
            return f"{line[: max_len - 1]}…"
        return line
    return ""


def history_location(finding: Mapping[str, Any]) -> str:
    """A wake history item: path:line, plus identity when one can be read."""
    site = f"{finding.get('path', '?')}:{finding.get('line', '?')}"
    identity = finding_identity(str(finding.get("body") or ""))
    if not identity:
        return site
    return f"{site} — {identity}"


def exhaustion_marker(head_sha: str) -> str:
    """The versioned writer form; policy-bearing (records the round bound)."""
    return (
        f"{EXHAUSTED_MARKER_PREFIX}{MARKER_SCHEMA_VERSION}:{head_sha}"
        f":max-rounds={MAX_REVIEW_ROUNDS} -->"
    )


def legacy_exhaustion_marker(head_sha: str) -> str:
    return f"{EXHAUSTED_MARKER_PREFIX}{head_sha} -->"


def exhaustion_marker_state(
    comments: Sequence[Mapping[str, Any]], head_sha: str
) -> str:
    """``terminal``, ``stale-policy``, or ``absent`` for this head's gate.

    A v2 marker whose recorded ``max-rounds`` equals the current policy is
    terminal.  A v2 marker under a *different* policy is not: the situation it
    gated no longer exists, so automation may re-evaluate (and re-post a gate
    under the current policy).  A legacy unversioned marker is honored as
    terminal — its head was deliberately stopped, and "silence means stop
    safely" is load-bearing — but honoring it is logged so the estate can see
    how many pre-versioning gates still govern.
    """
    trusted = trusted_control_logins()
    state = "absent"
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") not in trusted:
            continue
        for match in EXHAUSTED_MARKER_RE.finditer(str(comment.get("body") or "")):
            v2_head, v2_rounds, legacy_head = match.groups()
            if v2_head == head_sha and v2_rounds is not None:
                if int(v2_rounds) == MAX_REVIEW_ROUNDS:
                    return "terminal"
                state = "stale-policy"
            elif legacy_head == head_sha:
                print(
                    "legacy unversioned exhaustion marker honored for head "
                    f"{head_sha} (no recorded policy; treating as terminal)"
                )
                return "terminal"
    return state


def product_gate_marker(head_sha: str) -> str:
    return f"{PRODUCT_GATE_MARKER_PREFIX}{MARKER_SCHEMA_VERSION}:{head_sha} -->"


def legacy_product_gate_marker(head_sha: str) -> str:
    """Burn seats persist this form per the talos-burn skill; read it forever."""
    return f"{PRODUCT_GATE_MARKER_PREFIX}{head_sha} -->"


def product_gate_comment_body(head_sha: str) -> str:
    """The once-per-head record that a burn completed as product-gate.

    The head is unchanged on purpose; without this marker the scheduled
    redelivery path treats that as a stall and re-wakes Talos.
    """
    return (
        "Review-loop: product-gate recorded for this head; the head is "
        "unchanged on purpose. Standing-wake redelivery must not treat this "
        "as an ignored wake.\n"
        f"{product_gate_marker(head_sha)}"
    )


def noise_marker(head_sha: str) -> str:
    return f"{NOISE_MARKER_PREFIX}{MARKER_SCHEMA_VERSION}:{head_sha} -->"


def legacy_noise_marker(head_sha: str) -> str:
    """Burn seats persist this form per the talos-burn skill; read it forever."""
    return f"{NOISE_MARKER_PREFIX}{head_sha} -->"


def noise_comment_body(head_sha: str) -> str:
    """The once-per-head record that a burn completed as all-noise.

    The head is unchanged on purpose; without this marker the scheduled
    redelivery path treats that as a stall and re-wakes Talos.
    """
    return (
        "Review-loop: noise recorded for this head; the head is "
        "unchanged on purpose. Standing-wake redelivery must not treat this "
        "as an ignored wake.\n"
        f"{noise_marker(head_sha)}"
    )


def nudge_marker(head_sha: str) -> str:
    return f"{NUDGE_MARKER_PREFIX}{MARKER_SCHEMA_VERSION}:{head_sha} -->"


def legacy_nudge_marker(head_sha: str) -> str:
    return f"{NUDGE_MARKER_PREFIX}{head_sha} -->"


def nudge_comment_body(head_sha: str) -> str:
    """Codex trigger plus an exact-head marker so force-pushes cannot reuse it."""
    return f"@codex review\n{nudge_marker(head_sha)}"


def redelivery_marker(head_sha: str, verdict_at: str) -> str:
    """Once-per-verdict marker: a later verdict on the same SHA is a new event."""
    return (
        f"{REDELIVERY_MARKER_PREFIX}{MARKER_SCHEMA_VERSION}:{head_sha}:{verdict_at} -->"
    )


def legacy_redelivery_marker(head_sha: str, verdict_at: str) -> str:
    return f"{REDELIVERY_MARKER_PREFIX}{head_sha}:{verdict_at} -->"


def head_base_marker(head_sha: str, base_ref: str, base_sha: str) -> str:
    """v2 pins carry the base *ref* too: the closure predicate reads it."""
    return (
        f"{HEAD_BASE_MARKER_PREFIX}{MARKER_SCHEMA_VERSION}:"
        f"{head_sha}:{base_ref}:{base_sha} -->"
    )


def head_base_marker_for(head_sha: str) -> tuple[str, str]:
    """Prefixes matching any pin for this head, versioned or legacy."""
    return (
        f"{HEAD_BASE_MARKER_PREFIX}{MARKER_SCHEMA_VERSION}:{head_sha}:",
        f"{HEAD_BASE_MARKER_PREFIX}{head_sha}:",
    )


def head_base_comment_body(head_sha: str, base_ref: str, base_sha: str) -> str:
    return (
        "Review-loop: recorded contemporaneous base "
        f"`{base_ref}` @ `{base_sha}` for reviewed head `{head_sha}`.\n"
        f"{head_base_marker(head_sha, base_ref, base_sha)}"
    )


def recorded_head_bases(comments: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Trusted pins of each reviewed head's contemporaneous base SHA.

    First pin for a head wins: that is the base that was live when the
    verdict was first accepted. A later force-push of the target does not
    rewrite the series bound. Both marker generations are read; a v2 pin and
    a legacy pin for the same head keep whichever came first in comment order.
    """
    trusted = trusted_control_logins()
    bases: dict[str, str] = {}
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") not in trusted:
            continue
        body = str(comment.get("body") or "")
        for v2 in HEAD_BASE_MARKER_V2_RE.finditer(body):
            head, _ref, base = v2.groups()
            if head not in bases:
                bases[head] = base
        for match in HEAD_BASE_MARKER_RE.finditer(body):
            head, base = match.group(1), match.group(2)
            if head == MARKER_SCHEMA_VERSION:
                continue  # the first two segments of a v2 pin, not a legacy pin
            if head not in bases:
                bases[head] = base
    return bases


def recorded_head_base_refs(comments: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Trusted pins of each reviewed head's contemporaneous base *ref*.

    Only v2 pins carry a ref; a legacy pin contributes nothing here, so the
    closure predicate treats its head as having no recorded ref to compare.
    """
    trusted = trusted_control_logins()
    refs: dict[str, str] = {}
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") not in trusted:
            continue
        for v2 in HEAD_BASE_MARKER_V2_RE.finditer(str(comment.get("body") or "")):
            head, ref, _base = v2.groups()
            if head not in refs:
                refs[head] = ref
    return refs


def stale_base_ref_for_closure(
    comments: Sequence[Mapping[str, Any]], head_sha: str, live_base_ref: str
) -> str | None:
    """The recorded verdict-time base ref when it disagrees with the live one.

    The minimal review-subject predicate: an exact-head CLEAN was formed
    against the PR as targeted at verdict time. If the PR has since been
    retargeted (base *ref* changed — tip movement alone does not trip this),
    the head SHA alone no longer names what was reviewed, and closure must
    refuse rather than wake a seat with a stale claim. Refusing wakes nobody;
    the refusal is logged and a human resolves the retarget.
    """
    recorded = recorded_head_base_refs(comments).get(head_sha)
    if recorded and recorded != live_base_ref:
        return recorded
    return None


def base_retarget_after(
    github: GitHubApi, pr_number: int, verdict_time: str
) -> str | None:
    """The timestamp of a base-branch retarget newer than the verdict, if any.

    The pin comment cannot witness the FIRST closure after a retarget: the pin
    is written at hook time, so a verdict formed against the old target and a
    pin recorded after the retarget agree with each other and with the live
    base.  GitHub's issue events are the independent witness — a
    ``base_ref_changed`` event created after the verdict proves the review's
    subject is not the PR as now targeted.  Fetched only on the closure path,
    so the ordinary scan pays nothing.
    """
    verdict_at = result_event_time(verdict_time)
    for event in github.paginate(f"issues/{pr_number}/events"):
        if not isinstance(event, Mapping):
            continue
        if event.get("event") != "base_ref_changed":
            continue
        created = str(event.get("created_at") or "")
        # An undated verdict sorts to datetime.min, so any retarget event
        # refuses closure — the safe direction for an unwitnessable subject.
        if created and result_event_time(created) > verdict_at:
            return created
    return None


def record_reviewed_head_base(
    github: GitHubApi,
    pr_number: int,
    head_sha: str,
    base_ref: str,
    base_sha: str,
    *,
    repository: str,
    comments: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Persist the base that was live when this head was reviewed.

    Deduplicates by head, not by ``(head, base)``: the first accepted pin
    is the contemporaneous one. A later pin after the target moved would
    record the wrong series bound.
    """
    existing = comments
    if existing is None:
        existing = github.paginate(f"issues/{pr_number}/comments")
    if marker_comment_exists(existing, head_base_marker_for(head_sha)):
        return "existing"
    return ensure_comment_at_head(
        github,
        pr_number,
        head_base_marker_for(head_sha),
        head_base_comment_body(head_sha, base_ref, base_sha),
        expected_head=head_sha,
        repository=repository,
        action="head-base pin",
    )


def redelivery_comment_body(head_sha: str, verdict_at: str) -> str:
    """The once-per-verdict record that a standing verdict was re-delivered.

    Deliberately carries no ``@codex`` trigger: redelivery re-delivers attention
    to a seat, it never summons another review.  The nudge leg owns summoning.
    """
    return (
        "Review-loop: the standing verdict for this head was re-delivered to "
        f"#hive (original verdict `{verdict_at}`; the head has not moved "
        "since). One redelivery per standing verdict — a later verdict on this "
        "head, or a push, starts a fresh cycle.\n"
        f"{redelivery_marker(head_sha, verdict_at)}"
    )


def redelivery_notice(verdict_at: str) -> str:
    """The line that tells the receiving seat this wake is not a new dispatch."""
    return (
        f"REDELIVERY — restating a standing review-loop verdict first delivered "
        f"at `{verdict_at}`; the head has not moved since, so the action below "
        "is still outstanding. Per `universe/30-hive.md`, check whether the work "
        "already happened before redoing it: a repeated wake is never a second "
        "dispatch, and it adds no review round and no authority."
    )


def exhaustion_gate(
    *,
    pr_url: str,
    head_sha: str,
    reviewed_heads: int,
    reason: str,
) -> str:
    """A once-per-head, cold-answerable stop after bounded automation."""
    return (
        f"{exhaustion_marker(head_sha)}\n"
        "## Review loop exhausted — human decision required\n\n"
        f"PR: {pr_url}\n\n"
        f"Current head: `{head_sha}`\n\n"
        f"Automatic review rounds consumed: {reviewed_heads}/{MAX_REVIEW_ROUNDS}.\n\n"
        f"Why the loop stopped: {reason}\n\n"
        "This head is **not merge-ready**. The safe default is to leave it "
        "unmerged and stop automatic repair/re-review. Hákon: choose one of "
        "these explicit continuations:\n\n"
        "1. authorize one additional manual repair/re-review round outside the "
        "automatic loop;\n"
        "2. re-scope genuinely out-of-scope findings into named follow-up "
        "tickets, then request a fresh exact-head review; or\n"
        "3. close or defer the PR.\n\n"
        "A later merge still requires an exact-head clean review, green required "
        "checks, no conflicts, and separate merge authority."
    )


def ensure_exhaustion_gate_at_head(
    github: GitHubApi,
    pr_number: int,
    gate: str,
    *,
    head_sha: str,
    repository: str,
    action: str,
) -> str:
    """Post an exhaustion gate once per ``(head, policy)``.

    Terminal (same policy, or an honored legacy marker) deduplicates; a
    stale-policy marker does not — the situation it gated was measured against
    a bound that no longer exists, so the gate is re-stated under the current
    policy rather than silently inherited.
    """
    comments = github.paginate(f"issues/{pr_number}/comments")
    state = exhaustion_marker_state(comments, head_sha)
    if state == "terminal":
        return "existing"
    if state == "stale-policy":
        print(
            f"re-posting exhaustion gate for {repository}#{pr_number} "
            f"(head={head_sha}): the prior gate recorded a different policy"
        )
    if (
        refresh_pr_at_head(
            github,
            pr_number,
            head_sha,
            repository,
            action=action,
        )
        is None
    ):
        return "stale"
    github.post(f"issues/{pr_number}/comments", {"body": gate})
    return "posted"


def ensure_comment_at_head(
    github: GitHubApi,
    pr_number: int,
    marker: str | Sequence[str],
    body: str,
    *,
    expected_head: str,
    repository: str,
    action: str,
) -> str:
    """Deduplicate, then revalidate the live head immediately before posting."""
    comments = github.paginate(f"issues/{pr_number}/comments")
    if marker_comment_exists(comments, marker):
        return "existing"
    if (
        refresh_pr_at_head(
            github,
            pr_number,
            expected_head,
            repository,
            action=action,
        )
        is None
    ):
        return "stale"
    github.post(f"issues/{pr_number}/comments", {"body": body})
    return "posted"


def trusted_control_logins() -> frozenset[str]:
    """Identities whose control markers are authoritative for deduplication.

    Two identities write control markers. The workflow itself posts exhaustion
    gates on the event path; the scheduled path authenticates as the
    Codex-connected account (KRA-1032) and posts both nudges and gates. They are
    one trust domain, so a marker written by either must suppress the other.

    Splitting them is the defect: whichever path does not recognise the other's
    marker re-posts it, giving one head two nudges or two exhaustion gates. Both
    halves therefore read this one set rather than each naming its own author.
    """
    logins = [os.environ.get("WORKFLOW_LOGIN", WORKFLOW_LOGIN)]
    author = os.environ.get("CODEX_REVIEW_AUTHOR")
    if author:
        logins.append(author)
    return frozenset(login for login in logins if login)


def marker_comment_exists(
    comments: Sequence[Mapping[str, Any]],
    marker: str | Sequence[str],
) -> bool:
    """Trust a control marker only when a trusted control identity authored it.

    ``marker`` may be several equivalent forms (versioned plus legacy); any
    one of them counts.
    """
    markers = [marker] if isinstance(marker, str) else list(marker)
    trusted = trusted_control_logins()
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") not in trusted:
            continue
        body = str(comment.get("body") or "")
        if any(form in body for form in markers):
            return True
    return False


def trusted_marker_time(
    comments: Sequence[Mapping[str, Any]], marker: str | Sequence[str]
) -> datetime | None:
    """Latest created_at of a trusted comment carrying this marker, if dated."""
    markers = [marker] if isinstance(marker, str) else list(marker)
    trusted = trusted_control_logins()
    latest: datetime | None = None
    unstamped = datetime.min.replace(tzinfo=timezone.utc)
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") not in trusted:
            continue
        if not any(form in str(comment.get("body") or "") for form in markers):
            continue
        raw = comment.get("created_at")
        if not isinstance(raw, str) or not raw.strip():
            continue
        stamped = result_event_time(raw)
        if stamped == unstamped:
            continue
        if latest is None or stamped > latest:
            latest = stamped
    return latest


def refresh_pr_at_head(
    github: GitHubApi,
    pr_number: int,
    expected_head: str,
    repository: str,
    *,
    action: str = "Codex result",
) -> Mapping[str, Any] | None:
    """Re-read the live PR immediately before publishing a verdict."""
    pr_state = github.get(f"pulls/{pr_number}")
    if not isinstance(pr_state, Mapping):
        raise TypeError("pull request lookup did not return an object")
    head = pr_state.get("head")
    if not isinstance(head, Mapping):
        raise TypeError("live pull_request.head is missing")
    current_head = str(head["sha"])
    if current_head != expected_head:
        print(
            f"ignored {action} after head moved for {repository}#{pr_number} "
            f"(review={expected_head}, current={current_head})"
        )
        return None
    return pr_state


def pr_base(pr_state: Mapping[str, Any]) -> tuple[str, str]:
    """Return the live PR's ``(base.ref, base.sha)``.

    Burn and retrospective wakes embed both so a seat that can fetch the
    repo but cannot call the GitHub API — Talos v1 has no ``gh`` — can
    still name the declared base.  ``origin/HEAD`` is the wrong tree on a
    stacked or release-branch PR.
    """
    base = pr_state.get("base")
    if not isinstance(base, Mapping):
        raise TypeError("live pull_request.base is missing")
    ref = str(base.get("ref") or "").strip()
    sha = str(base.get("sha") or "").strip()
    if not ref or not sha:
        raise TypeError("live pull_request.base is missing ref or sha")
    return ref, sha


def merge_word(has_merge_on_green: bool) -> str:
    if has_merge_on_green:
        return (
            "The merge-on-green label is present: Hákon's merge word is "
            "pre-granted — merge only when this exact head is review-closed, "
            "required checks are green, and the PR is conflict-free."
        )
    return (
        "No merge-on-green label: the ceiling is ready-for-review; the merge "
        "waits on Hákon's word."
    )


def finding_blocks(
    findings: Sequence[Mapping[str, Any]], *, max_body: int = 3500
) -> list[str]:
    blocks: list[str] = []
    for index, finding in enumerate(findings, start=1):
        body = str(finding.get("body") or "").strip()
        if len(body) > max_body:
            body = f"{body[: max_body - 1]}…"
        blocks.append(
            f"### Finding {index} — {finding.get('path', '?')}:{finding.get('line', '?')}\n{body}\n"
        )
    return blocks


def wake_envelope_line(text: str) -> str:
    """First WAKE:/NEXT line — the envelope Hive's ``parseAddressedWake`` binds.

    That parser scans every line (``src/addressing.ts``), so a continuation
    that starts with only a label lets an embedded finding line such as
    ``WAKE: talos`` or ``NEXT fable`` become the routing envelope and steal
    the chunk from the intended seat.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("WAKE:") or stripped.upper().startswith("NEXT "):
            return stripped
    return ""


def chunk_digest(
    header: str,
    blocks: Sequence[str],
    *,
    label: str,
    budget: int = DIGEST_CHUNK_BUDGET,
) -> list[str]:
    """Split a digest across Slack messages; ``label`` names it in continuations."""
    if not blocks:
        return [f"{header}(no {label} entries)"]
    envelope = wake_envelope_line(header)
    messages: list[str] = []
    prefix = header
    current: list[str] = []
    used = len(prefix)
    for block in blocks:
        separator = 1 if current else 0
        if current and used + separator + len(block) > budget:
            messages.append(prefix + "\n".join(current))
            continued = f"({label} continued — part {len(messages) + 1})\n\n"
            prefix = f"{envelope}\n\n{continued}" if envelope else continued
            current = [block]
            used = len(prefix) + len(block)
        else:
            current.append(block)
            used += separator + len(block)
    if current:
        messages.append(prefix + "\n".join(current))
    return messages


def build_burn_messages(
    *,
    findings: Sequence[Mapping[str, Any]],
    review_state: str,
    word: str,
    pr_url: str,
    branch: str,
    head_sha: str,
    base_ref: str,
    base_sha: str,
    repository: str,
    author: str,
    author_actor: str,
    has_merge_on_green: bool,
    review_round: int,
    history: Sequence[Mapping[str, Any]] | None = None,
    substitute: Mapping[str, Any] | None = None,
) -> list[str]:
    if substitute is None:
        counts = severity_counts(findings)
        verdict = (
            f"Codex findings: {counts['p1']} P1 / {counts['p2']} P2 / "
            f"{counts['p3']} P3 (review state: {review_state})."
        )
    else:
        # A substitute verdict: counts come from the marker (deterministic),
        # and the digest is the substitute's verdict comment verbatim — the
        # loop never re-parses seat prose into structured findings.
        counts = dict(substitute["counts"])
        verdict = (
            f"Substitute ({substitute['actor']}) findings: {counts['p1']} P1 "
            f"/ {counts['p2']} P2 / {counts['p3']} P3 (marker verdict; the "
            "digest below is the substitute's verdict comment, verbatim)."
        )
        if findings:
            verdict += (
                f" This head also carries {len(findings)} Codex inline "
                "finding(s) from an earlier review — included below; burn "
                "the union."
            )
    evidence_heading = (
        "## Per-round history, then the current head's findings\n"
        if history
        else "## Finding digest\n"
    )
    header = (
        "WAKE: talos\n\n"
        "@Talos-burn — load skill `talos-burn` and burn these findings.\n\n"
        f"Review-loop hook: {verdict} {word}\n\n"
        f"PR: {pr_url}\n"
        f"Branch: `{branch}`\n"
        f"Head: `{head_sha}`\n"
        f"Base: `{base_ref}` @ `{base_sha}`\n"
        f"Repo: `{repository}`\n"
        f"Author: `{author}` (seat `{author_actor}`)\n"
        f"merge-on-green: `{'yes' if has_merge_on_green else 'no'}`\n\n"
        f"Exact-head review: `yes` — round {review_round}/{MAX_REVIEW_ROUNDS}.\n"
        "Any repair changes the head and invalidates this review closure. Push "
        "one coherent burn, then wait for a fresh exact-head Codex review.\n\n"
        "Doctrine: in-scope findings are fixed unless tagged `product-gate` or `noise`; "
        "out-of-scope findings become follow-up tickets, then seek fresh "
        "exact-head closure. Never interleave fixing with merging. "
        "If this seat cannot access the repo, re-WAKE the authoring seat with "
        "the digest intact (R-3: failures stay visible).\n\n"
        f"{evidence_heading}"
    )
    blocks = [
        *(round_history_blocks(history, head_sha) if history else ()),
        *(
            finding_blocks(findings)
            if substitute is None
            else [
                *finding_blocks(findings),
                *prior_substitute_digest_blocks(history or (), head_sha),
                *bounded_digest_blocks(str(substitute["body"])),
            ]
        ),
    ]
    return chunk_digest(header, blocks, label="finding digest")


def bounded_digest_blocks(body: str, *, max_body: int = 3500) -> list[str]:
    """Split a raw substitute digest so ``chunk_digest`` can place it.

    ``chunk_digest`` never splits inside a block. Codex findings are already
    bounded by ``finding_blocks``; a substitute verdict is one comment, so an
    oversized body has to be pre-split or the Slack post fails after the
    head already counts as reviewed.
    """
    text = str(body or "")
    if not text:
        return [""]
    if len(text) <= max_body:
        return [text]
    blocks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_body:
            blocks.append(remaining)
            break
        window = remaining[:max_body]
        cut = window.rfind("\n\n")
        if cut < max_body // 2:
            cut = window.rfind("\n")
        if cut < max_body // 2:
            cut = max_body
        blocks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return blocks


def prior_substitute_digest_blocks(
    history: Sequence[Mapping[str, Any]], current_head: str
) -> list[str]:
    """Earlier substitute-round comment bodies, already bounded for chunking."""
    blocks: list[str] = []
    wanted = current_head.lower()
    for entry in history:
        if str(entry.get("head") or "").lower() == wanted:
            continue
        digest = str(entry.get("digest") or "")
        if not digest:
            continue
        heading = (
            f"### Round {entry['round']}/{entry['rounds']} substitute digest "
            f"— `{entry['head']}`\n"
        )
        continued = (
            f"### Round {entry['round']}/{entry['rounds']} substitute digest "
            f"— `{entry['head']}` (continued)\n"
        )
        room = max(1, 3500 - max(len(heading), len(continued)))
        parts = bounded_digest_blocks(digest, max_body=room)
        blocks.append(heading + parts[0] + "\n")
        for part in parts[1:]:
            blocks.append(continued + part + "\n")
    return blocks


def location_lines(
    locations: Sequence[str], *, max_len: int = ROUND_LOCATION_LINE_BUDGET
) -> list[str]:
    """Split a Locations line so one round cannot overrun the digest budget."""
    prefix = "Locations: "
    room = max_len - len(prefix)
    if room < 1:
        raise ValueError("max_len is too small for a Locations line")
    if not locations:
        return []
    lines: list[str] = []
    current: list[str] = []
    used = 0

    def fit(location: str) -> str:
        if len(location) <= room:
            return location
        return f"{location[: room - 1]}…"

    for location in locations:
        item = fit(str(location))
        extra = len(item) if not current else len(item) + 2
        if current and used + extra > room:
            lines.append(prefix + ", ".join(current))
            current = [item]
            used = len(item)
        else:
            current.append(item)
            used += extra
    if current:
        lines.append(prefix + ", ".join(current))
    return lines


def contemporaneous_base_line(entry: Mapping[str, Any]) -> str:
    """Name this head's recorded series bound, or refuse to invent one."""
    base = str(entry.get("base") or "").strip()
    if base:
        return f"Base at review: `{base}`"
    return (
        "Base at review: unavailable — do not derive from the live Base; "
        "stop classification"
    )


def round_history_blocks(
    history: Sequence[Mapping[str, Any]], current_head: str
) -> list[str]:
    """Render self-labelling blocks per round so any chunk split stays legible.

    A single review can name enough locations to exceed ``chunk_digest``'s
    budget. Locations are split into continued blocks, each still labelled
    with the round and head.
    """
    blocks: list[str] = []
    for entry in history:
        counts = entry["counts"]
        head = str(entry["head"])
        marker = " **(current head)**" if head == current_head else ""
        title = f"### Round {entry['round']}/{entry['rounds']} — `{head}`{marker}"
        base_line = contemporaneous_base_line(entry)
        if not counts["total"]:
            blocks.append(f"{title}\n{base_line}\nVerdict: {entry['verdict']}.\n")
            continue
        verdict = (
            f"Verdict: {entry['verdict']} — {counts['p1']} P1 / "
            f"{counts['p2']} P2 / {counts['p3']} P3 ({counts['total']} total)."
        )
        loc_lines = location_lines([str(item) for item in entry["locations"]])
        if not loc_lines:
            blocks.append(f"{title}\n{base_line}\n{verdict}\n")
            continue
        blocks.append(f"{title}\n{base_line}\n{verdict}\n{loc_lines[0]}\n")
        continued = f"{title} (locations continued)"
        for loc_line in loc_lines[1:]:
            blocks.append(f"{continued}\n{loc_line}\n")
    return blocks


def build_retrospective_messages(
    *,
    findings: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    pr_url: str,
    branch: str,
    head_sha: str,
    base_ref: str,
    base_sha: str,
    repository: str,
    author: str,
    author_actor: str,
    review_round: int,
    substitute: Mapping[str, Any] | None = None,
) -> list[str]:
    """Ask Theoros why burning could not close this PR, and what generalises."""
    header = (
        "WAKE: theoros\n\n"
        "Review-loop hook: the bounded automatic loop is exhausted — "
        f"{review_round} reviewed head(s) consumed against an automatic bound "
        f"of {MAX_REVIEW_ROUNDS}, and burning did not close this PR. You are "
        "asked for a retrospective verdict. This wake carries no repair, "
        "re-review, or merge authority.\n\n"
        f"PR: {pr_url}\n"
        f"Branch: `{branch}`\n"
        f"Head: `{head_sha}`\n"
        f"Base: `{base_ref}` @ `{base_sha}`\n"
        f"Repo: `{repository}`\n"
        f"Author: `{author}` (seat `{author_actor}`)\n"
        f"Rounds consumed: {review_round} (automatic bound: "
        f"{MAX_REVIEW_ROUNDS}) — a PR whose rounds predate the bound reads "
        "above it.\n\n"
        "The human gate is already on the PR and the authoring seat is already "
        "woken; nothing waits on this verdict.\n\n"
        "What is asked:\n\n"
        "1. **Classify each reviewed head** as *original*, *repair*, or "
        "*no-op* before naming a cause. Give repairs-per-round, never "
        "rounds alone. The two-range form is `git range-diff "
        "<old-base>..<old-head> <new-base>..<new-head>` over each head's "
        "full series — not `git diff <head>^ <head>`, which sees only the "
        "tip and can hide an earlier repair in a multi-commit burn. Use "
        "each round's recorded contemporaneous base as that head's series "
        "bound. The live `Base: <base-ref> @ <base-sha>` line is only the "
        "current declared base (never `origin/HEAD`); do not derive an "
        "earlier series from it. After the target is force-pushed or "
        "rebased, `git merge-base <old-head> <live-base>` can fall back "
        "before commits that belonged to the old base and corrupt the "
        "classification. If a compared head has no recorded "
        "contemporaneous base, report the gap and stop; do not invent a "
        "range:\n\n"
        "    git fetch origin <old-base> <old-head> <new-base> <new-head>\n"
        "    git range-diff <old-base>..<old-head> "
        "<new-base>..<new-head>\n\n"
        "Do not pass the two tips to `git merge-base` with each other — "
        "that common ancestor is the wrong range on a stacked or release "
        "branch. If a fetch fails, report the gap and stop; "
        "do not invent a range. `=` means replayed unchanged; `!` means "
        "read the diff before counting a repair. Head-to-head `git diff` "
        "is worthless across a rebase.\n"
        "2. **Name the structural cause** — which property was being "
        "established, and why repeated repair could not establish it. "
        '"No structural cause; the finding classes were independent" is a '
        "valid and valuable verdict — some PRs are simply large. Never "
        "conclude from absence: report the gap and stop.\n"
        "3. **Codify what generalises** — a new scar-assert on an existing "
        "skill (preferred; `skills/writing-skills/SKILL.md` §4 makes "
        "refinement the default) or, rarely, a new skill. Provenance is "
        "mandatory: PR, findings, round count, claimed scope. Silence is a "
        "legitimate outcome — a one-off does not earn a corpus entry, and a "
        "corpus of noise is worse than a small one.\n"
        "4. **Post the verdict on the PR** so it is attached to the evidence, "
        "not only to this thread. Skill changes ride a review-ready PR to "
        "weave-doctrine; merge stays Hákon's word.\n\n"
        "The evidence below is the shape of the sequence: did severity fall, "
        "did the same file keep reappearing, did each burn draw new findings.\n\n"
        "## Per-round history, then the current head's findings\n"
    )
    blocks = [
        *round_history_blocks(history, head_sha),
        *(
            finding_blocks(findings)
            if substitute is None
            else [
                *finding_blocks(findings),
                *prior_substitute_digest_blocks(history, head_sha),
                *bounded_digest_blocks(str(substitute["body"])),
            ]
        ),
    ]
    return chunk_digest(header, blocks, label="retrospective evidence")


def publish_exhaustion_retrospective(
    slack: SlackApi,
    *,
    findings: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    pr_url: str,
    branch: str,
    head_sha: str,
    repository: str,
    author: str,
    author_actor: str,
    review_round: int,
    pr_number: int,
    base_ref: str,
    base_sha: str,
    substitute: Mapping[str, Any] | None = None,
) -> None:
    """Wake Theoros after an exhaustion gate. Shared by ``route`` and ``nudge``."""
    post_threaded_messages(
        slack,
        build_retrospective_messages(
            findings=findings,
            history=history,
            pr_url=pr_url,
            branch=branch,
            head_sha=head_sha,
            base_ref=base_ref,
            base_sha=base_sha,
            repository=repository,
            author=author,
            author_actor=author_actor,
            review_round=review_round,
            substitute=substitute,
        ),
    )
    print(
        f"woke theoros for {repository}#{pr_number} retrospective "
        f"(rounds={len(history)})"
    )


def clean_wake_message(
    *,
    author_actor: str,
    author: str,
    head_sha: str,
    review_round: int,
    review_state: str,
    word: str,
    pr_url: str,
    branch: str,
    verdict_source: str = "Codex review",
) -> str:
    return (
        f"WAKE: {author_actor}\n\n"
        f"Review-loop hook: {verdict_source} is CLEAN on the exact current head "
        f"`{head_sha}` (round {review_round}/{MAX_REVIEW_ROUNDS}; review "
        f"state: {review_state}). {word}\n\n"
        f"PR: {pr_url} (branch `{branch}`, author `{author}` / seat "
        f"`{author_actor}`).\n"
        "Boundary: exact-head CLEAN closes review only; it is not by itself "
        "a merge-ready claim. The same SHA must have green required checks, "
        "no conflicts, and merge authority. Without merge-on-green, report "
        "ready-for-review."
    )


def route_review(event: Mapping[str, Any] | None = None) -> None:
    event = event or load_event()
    review = event.get("review")
    pull_request = event.get("pull_request")
    if not isinstance(review, Mapping) or not isinstance(pull_request, Mapping):
        raise TypeError("event does not contain a review and pull_request")
    review_user = review.get("user")
    codex_login = os.environ.get("CODEX_LOGIN", CODEX_LOGIN)
    if not isinstance(review_user, Mapping) or review_user.get("login") != codex_login:
        print("ignored non-Codex review")
        return
    if is_quota_refusal_body(str(review.get("body") or "")):
        print("ignored Codex quota-refusal review")
        return

    repository = required_env("GITHUB_REPOSITORY")
    github = GitHubApi(required_env("GITHUB_TOKEN"), repository)
    pr_number = int(pull_request["number"])
    pr_state = github.get(f"pulls/{pr_number}")
    if not isinstance(pr_state, Mapping):
        raise TypeError("pull request lookup did not return an object")
    head = pr_state.get("head")
    if not isinstance(head, Mapping):
        raise TypeError("live pull_request.head is missing")
    head_sha = str(head["sha"])
    review_head_sha = str(review.get("commit_id") or "").strip()
    if not review_head_sha:
        raise TypeError("Codex review commit_id is missing")
    if review_head_sha != head_sha:
        print(
            f"ignored stale Codex review for {repository}#{pr_number} "
            f"(review={review_head_sha}, current={head_sha})"
        )
        return

    resolved = author_for_pr(github, pr_number, head_sha)
    if resolved is None:
        print("non-seat author - no wake")
        return
    author, author_actor = resolved
    review_comments = github.paginate(f"pulls/{pr_number}/comments")
    findings = review_findings(review_comments, int(review["id"]), codex_login)
    reviews = github.paginate(f"pulls/{pr_number}/reviews")
    conversation_comments = github.paginate(f"issues/{pr_number}/comments")
    result_events = codex_result_events(
        github, [*reviews, review], conversation_comments, codex_login
    )
    reviewed_heads = result_heads_from_events(result_events)
    review_round = result_round_for_head(reviewed_heads, review_head_sha)
    refreshed_pr = refresh_pr_at_head(github, pr_number, review_head_sha, repository)
    if refreshed_pr is None:
        return
    pr_state = refreshed_pr
    refreshed_head = pr_state.get("head")
    assert isinstance(refreshed_head, Mapping)
    branch = str(refreshed_head["ref"])
    labels = {
        str(label.get("name"))
        for label in pr_state.get("labels", [])
        if isinstance(label, Mapping)
    }
    has_merge_on_green = "merge-on-green" in labels
    word = merge_word(has_merge_on_green)
    pr_url = str(pr_state.get("html_url") or pull_request["html_url"])
    review_state = str(review.get("state") or "unknown")
    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    base_ref, base_sha = pr_base(pr_state)
    if (
        record_reviewed_head_base(
            github,
            pr_number,
            head_sha,
            base_ref,
            base_sha,
            repository=repository,
            comments=conversation_comments,
        )
        == "stale"
    ):
        return

    if not findings:
        stale_ref = stale_base_ref_for_closure(
            conversation_comments, head_sha, base_ref
        )
        if stale_ref is not None:
            print(
                f"refused clean closure for {repository}#{pr_number}: head "
                f"{head_sha} was reviewed against base ref `{stale_ref}` but "
                f"the PR now targets `{base_ref}` — the head SHA alone no "
                "longer names the reviewed subject; resolve the retarget and "
                "request a fresh exact-head review"
            )
            return
        retargeted_at = base_retarget_after(
            github, pr_number, str(review.get("submitted_at") or "")
        )
        if retargeted_at is not None:
            print(
                f"refused clean closure for {repository}#{pr_number}: the PR "
                f"base was retargeted at `{retargeted_at}`, after this "
                "verdict was formed — the review's subject is not the PR as "
                "now targeted; request a fresh exact-head review"
            )
            return
        slack.post_message(
            clean_wake_message(
                author_actor=author_actor,
                author=author,
                head_sha=head_sha,
                review_round=review_round,
                review_state=review_state,
                word=word,
                pr_url=pr_url,
                branch=branch,
            )
        )
        print(f"woke {author_actor} for {repository}#{pr_number} (findings=0)")
        return

    if review_round >= MAX_REVIEW_ROUNDS:
        counts = severity_counts(findings)
        locations = ", ".join(
            f"{finding.get('path', '?')}:{finding.get('line', '?')}"
            for finding in findings[:10]
        )
        if len(findings) > 10:
            locations += f", and {len(findings) - 10} more"
        reason = (
            f"Codex still reports {counts['total']} finding(s) on the exact "
            f"current head ({counts['p1']} P1 / {counts['p2']} P2 / "
            f"{counts['p3']} P3). Locations: {locations}."
        )
        gate = exhaustion_gate(
            pr_url=pr_url,
            head_sha=head_sha,
            reviewed_heads=review_round,
            reason=reason,
        )
        comment_state = ensure_exhaustion_gate_at_head(
            github,
            pr_number,
            gate,
            head_sha=head_sha,
            repository=repository,
            action="exhaustion gate",
        )
        if comment_state == "stale":
            print(
                f"ignored stale exhaustion gate for "
                f"{repository}#{pr_number} (head={head_sha})"
            )
            return
        if comment_state == "existing":
            print(
                f"exhaustion gate already present for "
                f"{repository}#{pr_number} (head={head_sha}); retrying wake"
            )
        if (
            refresh_pr_at_head(
                github,
                pr_number,
                head_sha,
                repository,
                action="exhaustion wake",
            )
            is None
        ):
            return
        slack.post_message(
            f"WAKE: {author_actor}\n\n"
            f"Review-loop hook: automatic repair/re-review exhausted at round "
            f"{review_round}/{MAX_REVIEW_ROUNDS}. Do not burn or merge "
            "automatically.\n\n"
            f"{gate}"
        )
        print(
            f"review loop exhausted for {repository}#{pr_number} "
            f"(head={head_sha}, findings={len(findings)})"
        )
        # The gate comment and the authoring-seat wake are already published:
        # the retrospective is a third message that never gates or delays them,
        # and a failure here terminalizes visibly instead of being swallowed.
        head_bases = recorded_head_bases(conversation_comments)
        head_bases.setdefault(head_sha, base_sha)
        history = round_history(
            reviewed_heads,
            [*reviews, review],
            review_comments,
            codex_login,
            latest_kind_by_head=latest_result_kind_by_head(result_events),
            head_bases=head_bases,
            issue_comments=conversation_comments,
        )
        publish_exhaustion_retrospective(
            slack,
            findings=findings,
            history=history,
            pr_url=pr_url,
            branch=branch,
            head_sha=head_sha,
            base_ref=base_ref,
            base_sha=base_sha,
            repository=repository,
            author=author,
            author_actor=author_actor,
            review_round=review_round,
            pr_number=pr_number,
        )
        return

    history = round_history(
        reviewed_heads,
        [*reviews, review],
        review_comments,
        codex_login,
        latest_kind_by_head=latest_result_kind_by_head(result_events),
        issue_comments=conversation_comments,
    )
    messages = build_burn_messages(
        findings=findings,
        review_state=review_state,
        word=word,
        pr_url=pr_url,
        branch=branch,
        head_sha=head_sha,
        base_ref=base_ref,
        base_sha=base_sha,
        repository=repository,
        author=author,
        author_actor=author_actor,
        has_merge_on_green=has_merge_on_green,
        review_round=review_round,
        history=history,
    )
    post_threaded_messages(slack, messages)
    print(
        f"woke talos for {repository}#{pr_number} "
        f"(findings={len(findings)}, author={author_actor}, messages={len(messages)})"
    )


def unroutable_comment_message(
    *,
    kind: str,
    pr_url: str,
    comment_url: str,
    reference: str | None,
    first_line: str,
) -> str:
    anchor = f"`{reference}`" if reference else "none it could pin"
    return (
        "FYI: review-loop saw a Codex comment it cannot route.\n"
        f"PR: {pr_url}\n"
        f"Comment: {comment_url}\n"
        f"Shape: `{kind}`  |  commit reference: {anchor}\n"
        f"First line: `{first_line}`\n"
        "No verdict was recorded for that head, so the bounded loop still "
        "treats it as unreviewed. If this is a verdict format, teach "
        "`codex_comment_kind` to recognise it."
    )


def report_unroutable_codex_comment(
    comment: Mapping[str, Any],
    codex_login: str,
    repository: str,
    issue: Mapping[str, Any],
) -> None:
    """Say out loud that a Codex comment carried a verdict the loop cannot use.

    Two tickets were paid for the opposite behaviour: an unrecognised verdict
    format was dropped in silence, and the only symptom was a head that never
    closed.  Known non-verdict shapes stay quiet; anything that looks like a
    verdict reaches #hive — a clean assertion with no pinnable head, a
    task-channel verdict that is not clean (whether or not it pins a commit:
    its findings have no route at all), or an unrecognised shape that still
    points at a commit.
    """
    user = comment.get("user")
    if not isinstance(user, Mapping) or user.get("login") != codex_login:
        print("ignored comment from a non-Codex author")
        return
    body = str(comment.get("body") or "")
    kind = codex_comment_kind(body)
    if kind in (
        CODEX_COMMENT_TASK_REPORT,
        CODEX_COMMENT_CONNECTOR_ERROR,
        CODEX_COMMENT_QUOTA_REFUSAL,
    ):
        print(f"ignored Codex {kind} comment")
        return
    reference = comment_commit_reference(body, repository)
    if kind == CODEX_COMMENT_UNKNOWN and reference is None:
        print("ignored unrecognised Codex comment with no commit reference")
        return

    pr_number = issue.get("number")
    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    slack.post_message(
        unroutable_comment_message(
            kind=kind,
            pr_url=str(issue.get("html_url") or f"{repository}#{pr_number}"),
            comment_url=str(comment.get("html_url") or "unknown"),
            reference=reference,
            first_line=body.strip().splitlines()[0][:120] if body.strip() else "",
        )
    )
    print(f"reported unroutable Codex {kind} comment on {repository}#{pr_number}")


def route_clean_comment(event: Mapping[str, Any] | None = None) -> None:
    """Route Codex's clean issue comment after resolving its exact reviewed SHA."""
    event = event or load_event()
    comment = event.get("comment")
    issue = event.get("issue")
    if not isinstance(comment, Mapping) or not isinstance(issue, Mapping):
        raise TypeError("event does not contain a comment and issue")
    if not isinstance(issue.get("pull_request"), Mapping):
        print("ignored non-PR issue comment")
        return

    codex_login = os.environ.get("CODEX_LOGIN", CODEX_LOGIN)
    repository = required_env("GITHUB_REPOSITORY")
    if clean_comment_commitish(comment, codex_login, repository) is None:
        report_unroutable_codex_comment(comment, codex_login, repository, issue)
        return

    github = GitHubApi(required_env("GITHUB_TOKEN"), repository)
    reviewed_head = clean_comment_head(github, comment, codex_login)
    if reviewed_head is None:
        print("ignored unresolvable Codex clean comment")
        return

    pr_number = int(issue["number"])
    pr_state = github.get(f"pulls/{pr_number}")
    if not isinstance(pr_state, Mapping):
        raise TypeError("pull request lookup did not return an object")
    head = pr_state.get("head")
    if not isinstance(head, Mapping):
        raise TypeError("live pull_request.head is missing")
    head_sha = str(head["sha"])
    if reviewed_head != head_sha.lower():
        print(
            f"ignored stale Codex clean comment for {repository}#{pr_number} "
            f"(review={reviewed_head}, current={head_sha})"
        )
        return

    resolved = author_for_pr(github, pr_number, head_sha)
    if resolved is None:
        print("non-seat author - no wake")
        return
    author, author_actor = resolved
    reviews = github.paginate(f"pulls/{pr_number}/reviews")
    conversation_comments = github.paginate(f"issues/{pr_number}/comments")
    reviewed_heads = codex_result_heads(
        github, reviews, [*conversation_comments, comment], codex_login
    )
    review_round = result_round_for_head(reviewed_heads, head_sha)
    refreshed_pr = refresh_pr_at_head(github, pr_number, head_sha, repository)
    if refreshed_pr is None:
        return
    pr_state = refreshed_pr
    refreshed_head = pr_state.get("head")
    assert isinstance(refreshed_head, Mapping)
    labels = {
        str(label.get("name"))
        for label in pr_state.get("labels", [])
        if isinstance(label, Mapping)
    }
    word = merge_word("merge-on-green" in labels)
    branch = str(refreshed_head["ref"])
    pr_url = str(pr_state.get("html_url") or issue.get("html_url") or "")
    base_ref, base_sha = pr_base(pr_state)
    if (
        record_reviewed_head_base(
            github,
            pr_number,
            head_sha,
            base_ref,
            base_sha,
            repository=repository,
            comments=conversation_comments,
        )
        == "stale"
    ):
        return
    stale_ref = stale_base_ref_for_closure(conversation_comments, head_sha, base_ref)
    if stale_ref is not None:
        print(
            f"refused clean closure for {repository}#{pr_number}: head "
            f"{head_sha} was reviewed against base ref `{stale_ref}` but the "
            f"PR now targets `{base_ref}` — the head SHA alone no longer "
            "names the reviewed subject; resolve the retarget and request a "
            "fresh exact-head review"
        )
        return
    retargeted_at = base_retarget_after(
        github, pr_number, str(comment.get("created_at") or "")
    )
    if retargeted_at is not None:
        print(
            f"refused clean closure for {repository}#{pr_number}: the PR base "
            f"was retargeted at `{retargeted_at}`, after this verdict was "
            "formed — the review's subject is not the PR as now targeted; "
            "request a fresh exact-head review"
        )
        return
    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    slack.post_message(
        clean_wake_message(
            author_actor=author_actor,
            author=author,
            head_sha=head_sha,
            review_round=review_round,
            review_state="clean-comment",
            word=word,
            pr_url=pr_url,
            branch=branch,
        )
    )
    print(f"woke {author_actor} for {repository}#{pr_number} (clean comment)")


def route_substitute_verdict(event: Mapping[str, Any]) -> None:
    """Route a trusted substitute reviewer's exact-head verdict, both kinds.

    CLEAN wakes the authoring seat exactly like a Codex CLEAN.  FINDINGS
    dispatches the burn twin exactly like a Codex findings review — counts
    from the marker, the digest being the substitute's verdict comment
    verbatim (seat prose is never re-parsed into structure).  A findings
    verdict that neither burns nor gates is a black hole: the head counts as
    reviewed while the burn twin never learns the findings exist.  A marker
    from an untrusted author is a forgery and routes nothing.
    """
    comment = event.get("comment")
    issue = event.get("issue")
    if not isinstance(comment, Mapping) or not isinstance(issue, Mapping):
        raise TypeError("event does not contain a comment and issue")
    if not isinstance(issue.get("pull_request"), Mapping):
        print("ignored non-PR substitute verdict comment")
        return
    parsed = substitute_verdicts([comment])
    if not parsed:
        author = comment.get("user")
        login = author.get("login") if isinstance(author, Mapping) else "unknown"
        print(f"ignored substitute-verdict marker from untrusted author {login}")
        return
    _, verdict = parsed[0]
    repository = required_env("GITHUB_REPOSITORY")
    pr_number = int(issue["number"])
    github = GitHubApi(required_env("GITHUB_TOKEN"), repository)
    pr_state = github.get(f"pulls/{pr_number}")
    if not isinstance(pr_state, Mapping):
        raise TypeError("pull request lookup did not return an object")
    head = pr_state.get("head")
    if not isinstance(head, Mapping):
        raise TypeError("live pull_request.head is missing")
    head_sha = str(head["sha"])
    if verdict["head"] != head_sha.lower():
        print(
            f"ignored stale substitute {verdict['kind']} verdict for "
            f"{repository}#{pr_number} "
            f"(verdict={verdict['head']}, current={head_sha})"
        )
        return

    resolved = author_for_pr(github, pr_number, head_sha)
    if resolved is None:
        print("non-seat author - no wake")
        return
    author, author_actor = resolved
    codex_login = os.environ.get("CODEX_LOGIN", CODEX_LOGIN)
    reviews = github.paginate(f"pulls/{pr_number}/reviews")
    conversation_comments = github.paginate(f"issues/{pr_number}/comments")
    reviewed_heads = codex_result_heads(
        github, reviews, [*conversation_comments, comment], codex_login
    )
    review_round = result_round_for_head(reviewed_heads, head_sha.lower())
    refreshed_pr = refresh_pr_at_head(github, pr_number, head_sha, repository)
    if refreshed_pr is None:
        return
    pr_state = refreshed_pr
    refreshed_head = pr_state.get("head")
    assert isinstance(refreshed_head, Mapping)
    labels = {
        str(label.get("name"))
        for label in pr_state.get("labels", [])
        if isinstance(label, Mapping)
    }
    word = merge_word("merge-on-green" in labels)
    branch = str(refreshed_head["ref"])
    pr_url = str(pr_state.get("html_url") or issue.get("html_url") or "")
    base_ref, base_sha = pr_base(pr_state)
    if (
        record_reviewed_head_base(
            github,
            pr_number,
            head_sha,
            base_ref,
            base_sha,
            repository=repository,
            comments=conversation_comments,
        )
        == "stale"
    ):
        return
    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    if verdict["kind"] != "clean":
        if review_round >= MAX_REVIEW_ROUNDS:
            # The automatic loop is exhausted. The scan CANNOT own this gate:
            # this verdict already entered the result stream, so the head is
            # in reviewed_heads and the scan's exhaustion post is behind
            # `head_sha not in reviewed_heads` — deferring here was the same
            # black hole one bound later. Mirror route_review: gate comment,
            # authoring-seat wake, Theoros retrospective. Counts come from
            # the marker (the inline list is empty by construction).
            counts = verdict["counts"]
            total = counts["p1"] + counts["p2"] + counts["p3"]
            reason = (
                f"Substitute ({verdict['actor']}) still reports {total} "
                f"finding(s) on the exact current head ({counts['p1']} P1 / "
                f"{counts['p2']} P2 / {counts['p3']} P3, marker counts). "
                "The digest is the substitute's verdict comment on the PR."
            )
            gate = exhaustion_gate(
                pr_url=pr_url,
                head_sha=head_sha,
                reviewed_heads=review_round,
                reason=reason,
            )
            comment_state = ensure_comment_at_head(
                github,
                pr_number,
                exhaustion_marker(head_sha),
                gate,
                expected_head=head_sha,
                repository=repository,
                action="exhaustion gate",
            )
            if comment_state == "stale":
                print(
                    f"ignored stale substitute exhaustion gate for "
                    f"{repository}#{pr_number} (head={head_sha})"
                )
                return
            if comment_state == "existing":
                print(
                    f"exhaustion gate already present for "
                    f"{repository}#{pr_number} (head={head_sha}); retrying wake"
                )
            slack.post_message(
                f"WAKE: {author_actor}\n\n"
                f"Review-loop hook: automatic repair/re-review exhausted at "
                f"round {review_round}/{MAX_REVIEW_ROUNDS} (substitute "
                f"verdict by `{verdict['actor']}`). Do not burn or merge "
                "automatically.\n\n"
                f"{gate}"
            )
            print(
                f"review loop exhausted for {repository}#{pr_number} "
                f"(head={head_sha}, substitute findings={total})"
            )
            review_comments = github.paginate(f"pulls/{pr_number}/comments")
            head_bases = recorded_head_bases(conversation_comments)
            head_bases.setdefault(head_sha, base_sha)
            history = round_history(
                reviewed_heads,
                reviews,
                review_comments,
                codex_login,
                latest_kind_by_head=latest_result_kind_by_head(
                    codex_result_events(
                        github,
                        reviews,
                        [*conversation_comments, comment],
                        codex_login,
                    )
                ),
                head_bases=head_bases,
                issue_comments=[*conversation_comments, comment],
            )
            publish_exhaustion_retrospective(
                slack,
                findings=[],
                history=history,
                pr_url=pr_url,
                branch=branch,
                head_sha=head_sha,
                base_ref=base_ref,
                base_sha=base_sha,
                repository=repository,
                author=author,
                author_actor=author_actor,
                review_round=review_round,
                pr_number=pr_number,
                substitute={
                    "actor": verdict["actor"],
                    "counts": verdict["counts"],
                    "body": str(comment.get("body") or ""),
                },
            )
            return
        review_comments = github.paginate(f"pulls/{pr_number}/comments")
        history = round_history(
            reviewed_heads,
            reviews,
            review_comments,
            codex_login,
            latest_kind_by_head=latest_result_kind_by_head(
                codex_result_events(
                    github, reviews, [*conversation_comments, comment], codex_login
                )
            ),
            issue_comments=[*conversation_comments, comment],
        )
        messages = build_burn_messages(
            findings=[],
            review_state=f"substitute-findings:{verdict['actor']}",
            word=word,
            pr_url=pr_url,
            branch=branch,
            head_sha=head_sha,
            base_ref=base_ref,
            base_sha=base_sha,
            repository=repository,
            author=author,
            author_actor=author_actor,
            has_merge_on_green="merge-on-green" in labels,
            review_round=review_round,
            history=history,
            substitute={
                "actor": verdict["actor"],
                "counts": verdict["counts"],
                "body": str(comment.get("body") or ""),
            },
        )
        post_threaded_messages(slack, messages)
        print(
            f"woke talos for {repository}#{pr_number} "
            f"(substitute findings by {verdict['actor']}, "
            f"counts={verdict['counts']}, messages={len(messages)})"
        )
        return
    stale_ref = stale_base_ref_for_closure(conversation_comments, head_sha, base_ref)
    if stale_ref is not None:
        print(
            f"refused substitute clean closure for {repository}#{pr_number}: "
            f"head {head_sha} was reviewed against base ref `{stale_ref}` but "
            f"the PR now targets `{base_ref}` — the head SHA alone no longer "
            "names the reviewed subject; resolve the retarget and request a "
            "fresh exact-head review"
        )
        return
    retargeted_at = base_retarget_after(
        github, pr_number, str(comment.get("created_at") or "")
    )
    if retargeted_at is not None:
        print(
            f"refused substitute clean closure for {repository}#{pr_number}: "
            f"the PR base was retargeted at `{retargeted_at}`, after this "
            "verdict was formed — the review's subject is not the PR as now "
            "targeted; request a fresh exact-head review"
        )
        return
    slack.post_message(
        clean_wake_message(
            author_actor=author_actor,
            author=author,
            head_sha=head_sha,
            review_round=review_round,
            review_state=f"substitute-clean:{verdict['actor']}",
            word=word,
            pr_url=pr_url,
            branch=branch,
            verdict_source=f"substitute review (`{verdict['actor']}`)",
        )
    )
    print(
        f"woke {author_actor} for {repository}#{pr_number} "
        f"(substitute clean by {verdict['actor']})"
    )


def route_comment_event(event: Mapping[str, Any]) -> None:
    """Route one issue-comment event: substitute verdict, malformed marker,
    or the clean-comment path."""
    comment = event.get("comment")
    if not isinstance(comment, Mapping):
        raise TypeError("event does not contain a comment")
    codex_login = os.environ.get("CODEX_LOGIN", CODEX_LOGIN)
    user = comment.get("user")
    author = user.get("login") if isinstance(user, Mapping) else None
    body = str(comment.get("body") or "")
    if author != codex_login and SUBSTITUTE_VERDICT_MARKER_RE.search(body):
        route_substitute_verdict(event)
        return
    if (
        author != codex_login
        and author in trusted_control_logins()
        and "weave-review-loop:substitute-verdict:" in body
        and SUBSTITUTE_SUMMON_MARKER_PREFIX not in body
    ):
        # A trusted substitute tried to post a verdict and the marker
        # does not parse. Falling through to the clean-comment path
        # ignores it silently while the standing summon marker suppresses
        # every scheduled retry — a permanently unreviewed head. R-3:
        # terminalize loudly so the malformed verdict is visible and the
        # substitute can repost the marker verbatim. (Summon comments
        # quote the marker grammar and are excluded by their own marker.)
        raise RuntimeError(
            "trusted substitute verdict marker does not parse "
            f"(author={author}); repost the verdict with the marker "
            "grammar quoted in the summon, verbatim"
        )
    route_clean_comment(event)


def route_codex_result() -> None:
    """Route Codex's findings review, its clean comment, or a substitute verdict."""
    event = load_event()
    if isinstance(event.get("review"), Mapping):
        route_review(event)
        return
    if isinstance(event.get("comment"), Mapping):
        route_comment_event(event)
        return
    raise TypeError("event contains neither a review nor an issue comment")


def parse_github_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def result_event_time(raw: Any) -> datetime:
    """Parse a GitHub event timestamp; undated events sort first, then by input order."""
    if not isinstance(raw, str) or not raw.strip():
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = parse_github_time(raw.strip())
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def commit_time(commit: Mapping[str, Any], fallback: str) -> datetime:
    nested = commit.get("commit")
    if isinstance(nested, Mapping):
        for role in ("committer", "author"):
            identity = nested.get(role)
            if isinstance(identity, Mapping) and identity.get("date"):
                return parse_github_time(str(identity["date"]))
    return parse_github_time(fallback)


def review_stall_anchor(
    commit: Mapping[str, Any], pull_request: Mapping[str, Any], now: datetime
) -> datetime:
    """Use a stable server timestamp when the client-authored commit date is future.

    GitHub preserves author-controlled commit timestamps. A bad agent clock must
    not suppress nudges indefinitely, but it also must not bypass the ordinary
    20-minute review window. Fall back to the PR's server-observed update (or
    creation) time for an implausible future commit date.
    """
    fallback = str(pull_request["created_at"])
    committed_at = commit_time(commit, fallback)
    if committed_at <= now:
        return committed_at
    server_observed = str(pull_request.get("updated_at") or fallback)
    return parse_github_time(server_observed)


def bare_nudge_covers_head(
    comments: Sequence[Mapping[str, Any]],
    head_sha: str,
) -> bool:
    """True when a prior control-identity nudge already targeted this exact head.

    Timestamps are not used: a force-push or reset onto an older commit can make
    an earlier head's nudge ``created_at`` fall after the restored head's
    committer date, which would falsely suppress re-nudges for the current SHA.
    Marker text alone is not enough — only a trusted control identity counts.
    """
    return marker_comment_exists(
        comments, (nudge_marker(head_sha), legacy_nudge_marker(head_sha))
    )


def exact_head_codex_reviews(
    reviews: Sequence[Mapping[str, Any]], head_sha: str, codex_login: str
) -> list[Mapping[str, Any]]:
    """Codex reviews submitted against this exact head, in review order."""
    wanted = head_sha.lower()
    matched: list[Mapping[str, Any]] = []
    for review in reviews:
        user = review.get("user")
        if not isinstance(user, Mapping) or user.get("login") != codex_login:
            continue
        if is_quota_refusal_body(str(review.get("body") or "")):
            continue
        if str(review.get("commit_id") or "").strip().lower() == wanted:
            matched.append(review)
    return matched


def standing_verdict_time(
    github: GitHubApi,
    reviews: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    head_sha: str,
    codex_login: str,
) -> datetime | None:
    """When Codex last published a verdict for this exact head.

    Both verdict channels count: a submitted review's ``submitted_at`` and a
    clean comment's ``created_at``.  The *latest* is the anchor — the window
    measures how long the current verdict has stood unacted, so a re-review
    restarts it rather than inheriting the first delivery's age.
    """
    stamps: list[datetime] = []
    for review in exact_head_codex_reviews(reviews, head_sha, codex_login):
        submitted = str(review.get("submitted_at") or "").strip()
        if submitted:
            stamps.append(parse_github_time(submitted))
    for comment in comments:
        created = str(comment.get("created_at") or "").strip()
        if not created:
            continue
        if clean_comment_head(github, comment, codex_login) == head_sha.lower():
            stamps.append(parse_github_time(created))
    for _, verdict in substitute_verdicts(comments):
        if verdict["head"] == head_sha.lower():
            stamps.append(verdict["at"])
    return max(stamps) if stamps else None


def standing_findings(
    github: GitHubApi,
    pr_number: int,
    reviews: Sequence[Mapping[str, Any]],
    codex_login: str,
) -> list[dict[str, Any]]:
    """Inline findings belonging to the supplied reviews.

    The caller chooses the review set.  Redelivery passes every same-head
    findings review since the most recent CLEAN: a later findings review
    does not resolve earlier comments, and only CLEAN (or a new head) does.
    Each inline comment belongs to exactly one review, so the result is a
    concatenation with no deduplication.  The comment list is fetched once
    and filtered per review, matching ``review_findings``'s contract.
    """
    review_comments = list(github.paginate(f"pulls/{pr_number}/comments"))
    findings: list[dict[str, Any]] = []
    for review in reviews:
        findings.extend(
            review_findings(review_comments, int(review["id"]), codex_login)
        )
    return findings


def same_head_findings_reviews_since_clean(
    reviews: Sequence[Mapping[str, Any]],
    result_events: Sequence[tuple[datetime, int, str, str]],
    head_sha: str,
    codex_login: str,
) -> list[Mapping[str, Any]]:
    """Exact-head findings reviews still standing after the latest CLEAN.

    A later findings review does not resolve earlier comments on the same SHA.
    Only a CLEAN verdict (or a new head) does.  Reviews submitted at or before
    the latest CLEAN on this head are dropped; everything after it is kept.
    """
    wanted = head_sha.lower()
    last_clean_at: datetime | None = None
    for at, _order, head, kind in result_events:
        if kind == "clean" and str(head).lower() == wanted:
            last_clean_at = at
    standing: list[Mapping[str, Any]] = []
    for review in exact_head_codex_reviews(reviews, head_sha, codex_login):
        if (
            last_clean_at is not None
            and result_event_time(review.get("submitted_at")) <= last_clean_at
        ):
            continue
        standing.append(review)
    return standing


def latest_exact_head_review(
    reviews: Sequence[Mapping[str, Any]], head_sha: str, codex_login: str
) -> Mapping[str, Any] | None:
    """The latest Codex review submitted against this exact head."""
    exact = exact_head_codex_reviews(reviews, head_sha, codex_login)
    if not exact:
        return None
    indexed = list(enumerate(exact))
    return max(
        indexed,
        key=lambda item: (result_event_time(item[1].get("submitted_at")), item[0]),
    )[1]


def redeliver_standing_wake(
    github: GitHubApi,
    *,
    pull_request: Mapping[str, Any],
    pr_number: int,
    head_sha: str,
    reviews: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    reviewed_heads: Sequence[str],
    repository: str,
    codex_login: str,
    now: datetime,
) -> bool:
    """Re-deliver one standing verdict for an already-reviewed head.

    Every review-loop wake fires exactly once, from an event-driven path.  A
    seat that consumed the wake without acting produces no further GitHub event,
    so the standing findings become invisible: the only clock-driven leg skips
    reviewed heads outright.  This is the backstop for that silence — attention
    only.  It adds no review round, no repair authority, and no merge authority,
    and it never summons a reviewer.

    "Acted" is a head change or a trusted product-gate or noise marker on
    this head dated at or after the standing verdict. An unchanged head past
    the window with none of those is a stall.
    """
    gate_state = exhaustion_marker_state(comments, head_sha)
    if gate_state == "terminal":
        # An exhaustion gate's terminal action is "stop and hold for the human".
        # That is observationally identical to consumed-without-action, and the
        # attention contract's "silence means stop safely" is load-bearing:
        # re-delivering it would convert a deliberate stop into pressure to act.
        return False
    if gate_state == "stale-policy":
        # The gate was written under a different round policy; the situation it
        # stopped no longer exists, so it must not stay terminal (KRA-1122).
        print(
            f"stale-policy exhaustion marker ignored for {repository}"
            f"#{pr_number} (head={head_sha}; policy now {MAX_REVIEW_ROUNDS})"
        )

    verdict_at = standing_verdict_time(github, reviews, comments, head_sha, codex_login)
    if verdict_at is None:
        print(
            f"no timestamped Codex verdict for {repository}#{pr_number} "
            f"(head={head_sha}); nothing to re-deliver"
        )
        return False
    gated_at = trusted_marker_time(
        comments,
        (product_gate_marker(head_sha), legacy_product_gate_marker(head_sha)),
    )
    if gated_at is not None and verdict_at <= gated_at:
        print(
            f"product-gate recorded for {repository}#{pr_number} "
            f"(head={head_sha}); not a stall"
        )
        return False
    noised_at = trusted_marker_time(
        comments,
        (noise_marker(head_sha), legacy_noise_marker(head_sha)),
    )
    if noised_at is not None and verdict_at <= noised_at:
        print(
            f"noise recorded for {repository}#{pr_number} "
            f"(head={head_sha}); not a stall"
        )
        return False
    if (now - verdict_at).total_seconds() < WAKE_REDELIVERY_SECONDS:
        return False
    verdict_stamp = verdict_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if marker_comment_exists(
        comments,
        (
            redelivery_marker(head_sha, verdict_stamp),
            legacy_redelivery_marker(head_sha, verdict_stamp),
        ),
    ):
        return False

    # The later same-head *kind* is authoritative: CLEAN clears the standing
    # digest.  A later findings review does not — GitHub does not resolve the
    # earlier review's comments, so the digest is the union since the most
    # recent CLEAN.  Zero retrievable inlines is not CLEAN.
    result_events = codex_result_events(github, reviews, comments, codex_login)
    latest_kind = latest_result_kind_by_head(result_events).get(head_sha)
    latest_review = latest_exact_head_review(reviews, head_sha, codex_login)
    clean_verdict = latest_kind == "clean"
    if clean_verdict:
        findings = []
    elif latest_kind == "findings":
        findings = standing_findings(
            github,
            pr_number,
            same_head_findings_reviews_since_clean(
                reviews, result_events, head_sha, codex_login
            ),
            codex_login,
        )
    else:
        print(
            f"no authoritative same-head verdict for {repository}#{pr_number} "
            f"(head={head_sha}); nothing to re-deliver"
        )
        return False
    listed_labels = {
        str(label.get("name"))
        for label in pull_request.get("labels", [])
        if isinstance(label, Mapping)
    }
    if clean_verdict and "merge-on-green" not in listed_labels:
        # Cost pre-filter on the listed PR, re-checked authoritatively below: a
        # plain CLEAN head is parked at ready-for-review forever, and paying two
        # lookups per scan for a state that never redelivers is pure waste.
        return False
    substitute_standing = (
        standing_substitute_findings(comments, head_sha) if not clean_verdict else None
    )
    if not clean_verdict and not findings and substitute_standing is None:
        # A findings review whose inline comments are gone is not evidence of
        # CLEAN, and there is no digest to re-deliver.  A standing substitute
        # findings verdict IS a digest — its verdict comment — so it
        # re-delivers like any other standing findings state.
        return False
    if not clean_verdict and len(reviewed_heads) >= MAX_REVIEW_ROUNDS:
        # Defence behind the exhaustion marker above, for a head whose gate
        # comment failed to post: automation has stopped either way.
        return False

    resolved = author_for_pr(github, pr_number, head_sha)
    if resolved is None:
        return False
    author, author_actor = resolved

    pr_state = refresh_pr_at_head(
        github, pr_number, head_sha, repository, action="wake redelivery"
    )
    if pr_state is None:
        return False
    if str(pr_state.get("state") or "") != "open":
        print(
            f"ignored wake redelivery for a closed {repository}#{pr_number} "
            f"(head={head_sha})"
        )
        return False
    refreshed_head = pr_state.get("head")
    if not isinstance(refreshed_head, Mapping):
        raise TypeError("live pull_request.head is missing")
    labels = {
        str(label.get("name"))
        for label in pr_state.get("labels", [])
        if isinstance(label, Mapping)
    }
    has_merge_on_green = "merge-on-green" in labels
    if clean_verdict and not has_merge_on_green:
        # The live label, not the listed one: ready-for-review is a correct
        # parked state, and only merge-on-green CLEAN carries a standing
        # mechanical action to re-deliver.
        return False
    if not clean_verdict and not findings and substitute_standing is None:
        return False
    branch = str(refreshed_head["ref"])
    pr_url = str(pr_state.get("html_url") or f"{repository}#{pr_number}")
    review_round = result_round_for_head(reviewed_heads, head_sha)
    word = merge_word(has_merge_on_green)

    if clean_verdict:
        live_base_ref, _live_base_sha = pr_base(pr_state)
        stale_ref = stale_base_ref_for_closure(comments, head_sha, live_base_ref)
        if stale_ref is not None:
            print(
                f"refused clean redelivery for {repository}#{pr_number}: head "
                f"{head_sha} was reviewed against base ref `{stale_ref}` but "
                f"the PR now targets `{live_base_ref}` — the standing verdict "
                "no longer names the reviewed subject"
            )
            return False
        retargeted_at = base_retarget_after(github, pr_number, verdict_stamp)
        if retargeted_at is not None:
            print(
                f"refused clean redelivery for {repository}#{pr_number}: the "
                f"PR base was retargeted at `{retargeted_at}`, after the "
                "standing verdict was formed — it no longer names the "
                "reviewed subject"
            )
            return False
        messages = [
            clean_wake_message(
                author_actor=author_actor,
                author=author,
                head_sha=head_sha,
                review_round=review_round,
                review_state="clean-comment",
                word=word,
                pr_url=pr_url,
                branch=branch,
            )
        ]
    else:
        base_ref, base_sha = pr_base(pr_state)
        review_comments = github.paginate(f"pulls/{pr_number}/comments")
        history = round_history(
            reviewed_heads,
            reviews,
            review_comments,
            codex_login,
            latest_kind_by_head=latest_result_kind_by_head(result_events),
            issue_comments=comments,
        )
        # Mixed sources on one head (Codex inline findings AND a later
        # substitute verdict): the latest source is the primary verdict, and
        # the other source's digest still rides in the same wake — the burn
        # seat must never act on a partial evidence set.
        substitute_is_latest = substitute_standing is not None and (
            latest_review is None
            or substitute_standing[1]["at"]
            >= result_event_time(latest_review.get("submitted_at"))
        )
        if findings and not substitute_is_latest:
            assert latest_review is not None
            review_state = str(latest_review.get("state") or "unknown")
            substitute_payload = None
        else:
            assert substitute_standing is not None
            substitute_comment, substitute_verdict_parsed = substitute_standing
            review_state = f"substitute-findings:{substitute_verdict_parsed['actor']}"
            substitute_payload = {
                "actor": substitute_verdict_parsed["actor"],
                "counts": substitute_verdict_parsed["counts"],
                "body": str(substitute_comment.get("body") or ""),
            }
        messages = build_burn_messages(
            findings=findings,
            review_state=review_state,
            substitute=substitute_payload,
            word=word,
            pr_url=pr_url,
            branch=branch,
            head_sha=head_sha,
            base_ref=base_ref,
            base_sha=base_sha,
            repository=repository,
            author=author,
            author_actor=author_actor,
            has_merge_on_green=has_merge_on_green,
            review_round=review_round,
            history=history,
        )
    messages[0] = f"{redelivery_notice(verdict_stamp)}\n\n{messages[0]}"

    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    post_threaded_messages(slack, messages)
    # Slack first, then the marker. A Slack failure leaves no marker, so the next
    # tick retries — duplicate delivery is the safe direction. A marker failure
    # after a successful post raises out of the scan rather than being swallowed.
    github.post(
        f"issues/{pr_number}/comments",
        {"body": redelivery_comment_body(head_sha, verdict_stamp)},
    )
    print(
        f"re-delivered standing wake for {repository}#{pr_number} "
        f"(head={head_sha}, findings={len(findings)}, verdict={verdict_stamp})"
    )
    return True


def usage_meter_reading() -> dict[str, Any] | None:
    """Read the Codex pool from the AI-usage aggregator; ``None`` means absent.

    The meter is advisory routing input, never a gate: ANY failure — unset
    env, network, non-2xx, unparseable body, unknown pool — collapses to
    ``None`` and the caller behaves exactly as it did before the meter
    existed.  A dead meter must not add a way for the belt to hang.  The
    bearer token is used and never returned, logged, or embedded in output.
    """
    base_url = os.environ.get("AI_USAGE_URL", "").strip().rstrip("/")
    token = os.environ.get("AI_USAGE_READ_TOKEN", "").strip()
    pool_name = os.environ.get("AI_USAGE_CODEX_POOL", "").strip()
    if not base_url or not token:
        return None
    request = urllib.request.Request(
        f"{base_url}/v3/usage",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "weave-doctrine-review-loop",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - advisory meter: every failure is "absent".
        return None
    pools = payload.get("pools") if isinstance(payload, Mapping) else None
    if not isinstance(pools, Sequence):
        return None
    # Schema-3 pool selection: the pool `id` is an opaque identity digest, so
    # the default match is `provider == "codex"`; AI_USAGE_CODEX_POOL, when
    # set, narrows by exact `id` or human `label`.  With several connected
    # codex pools and no narrowing, payload order is not an identity — the
    # most-constrained window across EVERY matching pool governs routing
    # (route away if any connected pool is hot), never first-in-array.
    utilization: float | None = None
    resets_at: Any = None
    for candidate in pools:
        if not isinstance(candidate, Mapping):
            continue
        if candidate.get("provider") != "codex":
            continue
        if pool_name and pool_name not in (candidate.get("id"), candidate.get("label")):
            continue
        windows = candidate.get("windows")
        if not isinstance(windows, Sequence):
            continue
        for window in windows:
            if not isinstance(window, Mapping):
                continue
            value = window.get("utilization")
            if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                continue
            if utilization is None or float(value) > utilization:
                utilization = float(value)
                resets_at = window.get("resets_at")
    if utilization is None:
        return None
    return {
        "utilization": utilization,
        "resets_at": str(resets_at) if resets_at else None,
    }


def codex_threshold() -> float:
    raw = os.environ.get("AI_USAGE_CODEX_THRESHOLD", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return AI_USAGE_DEFAULT_THRESHOLD
    if not 0 < value <= 1:
        return AI_USAGE_DEFAULT_THRESHOLD
    return value


def normalize_substitute_actor(raw: str) -> str | None:
    """Map a configured actor onto the verdict-marker grammar, or ``None``.

    The marker contract is ``SUBSTITUTE_ACTOR_TOKEN``. Case and underscores
    are recoverable; anything else cannot become a parseable marker without
    inventing identity, so it is refused.
    """
    normalized = raw.strip().lower().replace("_", "-")
    if not SUBSTITUTE_ACTOR_RE.fullmatch(normalized):
        return None
    return normalized


def substitute_actor() -> str:
    raw = (
        os.environ.get("REVIEW_SUBSTITUTE_ACTOR", "").strip()
        or DEFAULT_SUBSTITUTE_ACTOR
    )
    actor = normalize_substitute_actor(raw)
    if actor is None:
        raise ValueError(
            f"REVIEW_SUBSTITUTE_ACTOR={raw!r} is outside the substitute "
            "verdict grammar [a-z0-9-]+ after normalization; refusing to "
            "summon an unparseable reviewer"
        )
    return actor


def quota_refusal_is_latest_codex_signal(
    reviews: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    codex_login: str,
) -> bool:
    """True when the newest thing Codex said on this PR is a quota refusal.

    Evidence-ordered, not clock-windowed: a refusal is live until Codex itself
    supersedes it with any later signal (a review, a clean comment, a verdict,
    even another error shape).  That needs no reset-period knob and cannot go
    stale silently — the moment Codex speaks again, the refusal stops routing.
    """
    latest_refusal: datetime | None = None
    latest_other: datetime | None = None
    for review in reviews:
        user = review.get("user")
        if not isinstance(user, Mapping) or user.get("login") != codex_login:
            continue
        at = result_event_time(review.get("submitted_at"))
        # A usage-limit refusal can arrive as a submitted review, not only a
        # comment (the connector auto-fires on push); classifying it as a
        # superseding "other" signal would flip the route straight back into
        # another refusal.
        body = str(review.get("body") or "")
        if is_quota_refusal_body(body):
            if latest_refusal is None or at > latest_refusal:
                latest_refusal = at
        elif latest_other is None or at > latest_other:
            latest_other = at
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") != codex_login:
            continue
        at = result_event_time(comment.get("created_at"))
        body = str(comment.get("body") or "")
        if is_quota_refusal_body(body):
            if latest_refusal is None or at > latest_refusal:
                latest_refusal = at
        elif latest_other is None or at > latest_other:
            latest_other = at
    if latest_refusal is None:
        return False
    # GitHub stamps these events to whole seconds, so a refusal and another
    # signal can tie. A tie keeps the refusal live: routing the substitute
    # once more is attention-only, routing back to Codex elicits another
    # refusal and loses the evidence.
    return latest_other is None or latest_refusal >= latest_other


def find_half_route(
    reviews: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    codex_login: str,
    *,
    usage_meter: Mapping[str, Any] | None | object = _FETCH_METER,
) -> tuple[str, str]:
    """Choose the find half for one summon: ``("codex" | "substitute", reason)``.

    A pure selection fold in front of the existing summon code — it never
    posts, never raises, and its reason string is published with the summon so
    the thread shows why a reviewer was chosen (R-3).  Fail-open by
    construction: with no meter and no observed refusal the answer is Codex,
    byte-identical to the pre-router loop.

    ``usage_meter`` is the scan's one account-wide reading when supplied;
    omitted, this fold fetches.  The scheduled scan must pass the reading so
    a black-holing aggregator cannot charge one timeout per stalled PR.
    """
    if quota_refusal_is_latest_codex_signal(reviews, comments, codex_login):
        return (
            "substitute",
            (
                "Codex's latest signal on this PR is a usage-limits refusal "
                "(unsuperseded), so a nudge would burn another refusal"
            ),
        )
    meter = usage_meter_reading() if usage_meter is _FETCH_METER else usage_meter
    if meter is None:
        return ("codex", "usage meter absent; default find half")
    threshold = codex_threshold()
    resets = meter["resets_at"] or "unknown"
    if meter["utilization"] >= threshold:
        return (
            "substitute",
            (
                f"Codex pool utilization {meter['utilization']:.2f} >= "
                f"threshold {threshold:.2f} (resets_at {resets})"
            ),
        )
    return (
        "codex",
        (
            f"Codex pool utilization {meter['utilization']:.2f} < "
            f"threshold {threshold:.2f} (resets_at {resets})"
        ),
    )


def substitute_summon_marker(head_sha: str) -> str:
    return f"{SUBSTITUTE_SUMMON_MARKER_PREFIX}{head_sha} -->"


def substitute_summon_covers_head(
    comments: Sequence[Mapping[str, Any]], head_sha: str
) -> bool:
    return marker_comment_exists(comments, substitute_summon_marker(head_sha))


def substitute_verdict_marker_line(head_sha: str, actor: str) -> str:
    """The marker contract quoted verbatim in the summon, so the substitute
    can copy it rather than reconstruct it."""
    prefix = f"{SUBSTITUTE_VERDICT_MARKER_PREFIX}{head_sha}:{actor}"
    return (
        f"CLEAN: `{prefix}:clean -->`\n"
        f"Findings: `{prefix}:findings:<p1>:<p2>:<p3> -->` "
        "(counts by severity), plus the findings themselves in the same comment."
    )


def substitute_summon_comment_body(
    *, head_sha: str, actor: str, reason: str, review_round: int
) -> str:
    """PR-side record of a substitute summon: dedupe marker + routing decision.

    This is the audit trail the nudge marker provides for Codex summons — a
    scan that finds it does not summon this head again, and a reader of the PR
    sees why Codex was not asked.
    """
    return (
        f"Review-loop router: summoned substitute reviewer `{actor}` for head "
        f"`{head_sha}` (round {review_round}/{MAX_REVIEW_ROUNDS}).\n"
        f"Route reason: {reason}.\n"
        f"{substitute_summon_marker(head_sha)}"
    )


def substitute_review_wake_message(
    *,
    actor: str,
    reason: str,
    pr_url: str,
    branch: str,
    head_sha: str,
    review_round: int,
    repository: str,
) -> str:
    return (
        f"WAKE: {actor}\n\n"
        "Review-loop router: the Codex find half is unavailable for this "
        f"summon ({reason}). You hold this review round.\n\n"
        f"PR: {pr_url}\n"
        f"Repo: `{repository}`\n"
        f"Branch: `{branch}`\n"
        f"Head: `{head_sha}` (round {review_round}/{MAX_REVIEW_ROUNDS})\n\n"
        "Post your exact-head verdict as a PR conversation comment that "
        "includes the matching marker line verbatim — the marker, not the "
        "prose, is what the loop reads:\n"
        f"{substitute_verdict_marker_line(head_sha, actor)}\n\n"
        "Do not nudge @codex (a summons while it is unavailable burns a "
        "refusal). Do not merge: closure and merge stay with the loop's "
        "composed boundary."
    )


class ScanCompletedWithErrors(RuntimeError):
    """The scheduled scan finished, but one or more PRs failed reconciliation.

    Raised *after* the loop so the workflow run still terminalizes red (R-3)
    without one poisoned PR blinding every PR enumerated after it.
    """

    def __init__(self, errors: Sequence[tuple[int, Exception]]) -> None:
        self.errors = list(errors)
        summary = "; ".join(
            f"#{number}: {type(error).__name__}: {error}"
            for number, error in self.errors
        )
        super().__init__(
            f"nudge scan completed with {len(self.errors)} PR error(s): {summary}"
        )


def _scan_open_pull(
    github: GitHubApi,
    pull_request: Mapping[str, Any],
    *,
    repository: str,
    codex_login: str,
    now: datetime,
    usage_meter: Mapping[str, Any] | None | object = _FETCH_METER,
) -> tuple[int, int, int]:
    """Reconcile one open PR; returns ``(nudged, summoned, redelivered)`` deltas."""
    head = pull_request.get("head")
    if not isinstance(head, Mapping):
        return (0, 0, 0)
    commit = github.get(f"commits/{head['sha']}")
    author = commit_author_name(commit)
    if author not in SEAT_ACTORS:
        return (0, 0, 0)
    committed_at = review_stall_anchor(commit, pull_request, now)
    if (now - committed_at).total_seconds() < REVIEW_STALL_SECONDS:
        return (0, 0, 0)
    pr_number = int(pull_request["number"])
    reviews = github.paginate(f"pulls/{pr_number}/reviews")
    head_sha = str(head["sha"])
    comments = github.paginate(f"issues/{pr_number}/comments")
    reviewed_heads = codex_result_heads(github, reviews, comments, codex_login)
    if head_sha in reviewed_heads:
        # A reviewed head needs no reviewer, but its verdict may still be
        # standing unacted. The commit-age gate above is already satisfied
        # here: a verdict old enough to re-deliver sits on a commit that is
        # older still, and the future-commit fallback clears twenty minutes
        # of PR quiet on its own.
        if redeliver_standing_wake(
            github,
            pull_request=pull_request,
            pr_number=pr_number,
            head_sha=head_sha,
            reviews=reviews,
            comments=comments,
            reviewed_heads=reviewed_heads,
            repository=repository,
            codex_login=codex_login,
            now=now,
        ):
            return (0, 0, 1)
        return (0, 0, 0)
    if len(reviewed_heads) >= MAX_REVIEW_ROUNDS:
        pr_url = str(pull_request.get("html_url") or f"{repository}#{pr_number}")
        gate = exhaustion_gate(
            pr_url=pr_url,
            head_sha=head_sha,
            reviewed_heads=len(reviewed_heads),
            reason=(
                "The current head has no exact-head Codex review after the "
                "bounded automatic review cycle."
            ),
        )
        gate_state = exhaustion_marker_state(comments, head_sha)
        if gate_state != "terminal":
            if gate_state == "stale-policy":
                print(
                    f"re-posting exhaustion gate for {repository}"
                    f"#{pr_number} (head={head_sha}): the prior gate "
                    "recorded a different policy"
                )
            pr_state = refresh_pr_at_head(
                github,
                pr_number,
                head_sha,
                repository,
                action="exhaustion gate",
            )
            if pr_state is None:
                return (0, 0, 0)
            github.post(f"issues/{pr_number}/comments", {"body": gate})
            print(f"exhausted PR #{pr_number}")
        return (0, 0, 0)
    # A standing substitute summon owns the head in both directions: it is an
    # assignment to a seat, and recomputing ownership when the meter moves
    # would post a second reviewer against the same exact head.
    if substitute_summon_covers_head(comments, head_sha):
        return (0, 0, 0)
    # A bare Codex nudge owns the head only while Codex has not refused it.
    # Treating the nudge marker as unconditional coverage is a permanent
    # stall: the refusal is (correctly) not a result, so the head never
    # enters reviewed_heads, and every scan would return here forever — the
    # exact black hole the router exists to close.
    if bare_nudge_covers_head(
        comments, head_sha
    ) and not quota_refusal_is_latest_codex_signal(reviews, comments, codex_login):
        return (0, 0, 0)
    route, route_reason = find_half_route(
        reviews, comments, codex_login, usage_meter=usage_meter
    )
    if route == "codex":
        if bare_nudge_covers_head(comments, head_sha):
            # Codex's refusal was superseded by later Codex activity, but the
            # standing nudge for this head was already posted — do not repeat.
            return (0, 0, 0)
        if (
            refresh_pr_at_head(
                github,
                pr_number,
                head_sha,
                repository,
                action="review nudge",
            )
            is None
        ):
            return (0, 0, 0)
        github.post(
            f"issues/{pr_number}/comments", {"body": nudge_comment_body(head_sha)}
        )
        print(f"nudged PR #{pr_number} ({route_reason})")
        return (1, 0, 0)
    if (
        refresh_pr_at_head(
            github,
            pr_number,
            head_sha,
            repository,
            action="substitute review summon",
        )
        is None
    ):
        return (0, 0, 0)
    actor = substitute_actor()
    review_round = result_round_for_head(reviewed_heads, head_sha)
    # Slack first, then the marker — the same publication rule as every
    # other wake leg.  Marker-first made a lost wake unrecoverable: a
    # Slack failure after the marker left the head "covered" with #hive
    # silent, the exact stall class this router exists to close.  With
    # Slack first, a marker failure raises out of the scan (R-3) and the
    # next tick re-summons; a duplicate summon is attention-only and the
    # seat dedupes it, which is the safe direction.
    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    slack.post_message(
        substitute_review_wake_message(
            actor=actor,
            reason=route_reason,
            pr_url=str(pull_request.get("html_url") or f"{repository}#{pr_number}"),
            branch=str(head.get("ref") or "unknown"),
            head_sha=head_sha,
            review_round=review_round,
            repository=repository,
        )
    )
    github.post(
        f"issues/{pr_number}/comments",
        {
            "body": substitute_summon_comment_body(
                head_sha=head_sha,
                actor=actor,
                reason=route_reason,
                review_round=review_round,
            )
        },
    )
    print(f"summoned substitute for PR #{pr_number} ({route_reason})")
    return (0, 1, 0)


def nudge_stalled_reviews() -> None:
    repository = required_env("GITHUB_REPOSITORY")
    codex_login = os.environ.get("CODEX_LOGIN", CODEX_LOGIN)
    github = GitHubApi(required_env("GITHUB_TOKEN"), repository)
    expected_login = required_env("CODEX_REVIEW_AUTHOR")
    authenticated_login = github.authenticated_login()
    if authenticated_login != expected_login:
        raise RuntimeError(
            "Codex review credential authenticates as "
            f"{authenticated_login}, expected {expected_login}"
        )
    now = datetime.now(timezone.utc)
    usage_meter = usage_meter_reading()
    nudged = 0
    summoned = 0
    redelivered = 0
    errors: list[tuple[int, Exception]] = []
    for pull_request in github.paginate("pulls", query={"state": "open"}):
        pr_number = int(pull_request.get("number") or 0)
        try:
            nudged_delta, summoned_delta, redelivered_delta = _scan_open_pull(
                github,
                pull_request,
                repository=repository,
                codex_login=codex_login,
                now=now,
                usage_meter=usage_meter,
            )
        except Exception as error:  # noqa: BLE001 - one PR must not blind the rest.
            print(
                f"scan failed for {repository}#{pr_number}: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            errors.append((pr_number, error))
            continue
        nudged += nudged_delta
        summoned += summoned_delta
        redelivered += redelivered_delta
    print(
        f"nudge scan complete (nudged={nudged}, substitute={summoned}, "
        f"redelivered={redelivered})"
    )
    if errors:
        raise ScanCompletedWithErrors(errors)


def send_canary() -> None:
    repository = required_env("GITHUB_REPOSITORY")
    run_id = required_env("GITHUB_RUN_ID")
    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    slack.post_message(
        "WAKE: talos\n\n"
        "@Talos-burn automation canary - no PR and no code changes. This is a "
        "controlled GitHub Actions -> Slack -> Hive -> RunPod -> Grok probe. "
        "Reply in this thread with exactly `TALOS REVIEW-LOOP CANARY OK`, your "
        "current `/workspace/weave-doctrine` short commit, and whether the edge "
        "is healthy. Do not modify anything or print secrets.\n\n"
        f"Source: `{repository}` workflow run `{run_id}`."
    )
    print(f"posted review-loop canary for {repository} run {run_id}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("route", "nudge", "canary"))
    args = parser.parse_args(argv)
    try:
        {
            "route": route_codex_result,
            "nudge": nudge_stalled_reviews,
            "canary": send_canary,
        }[args.command]()
    except Exception as error:  # noqa: BLE001 - CLI boundary must terminalize visibly.
        print(f"review-loop {args.command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
