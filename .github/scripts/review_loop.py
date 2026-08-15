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
    re-deliver a standing verdict once per *reviewed* head whose seat never
    acted.  A wake fires exactly once from an event-driven path; a consumed
    wake with no new GitHub event is otherwise silence forever.
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
MAX_REVIEW_ROUNDS = 3
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
CODEX_COMMENT_UNKNOWN = "unknown"
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


def post_threaded_messages(slack: SlackApi, messages: Sequence[str]) -> None:
    """Post the first message as a Slack root; later chunks reply in its thread.

    Hive binds a seat to the thread of a WAKE delivery. Continuations posted as
    their own roots never share that affinity, so a chunked retrospective would
    leave Theoros with only the first chunk.
    """
    if not messages:
        return
    thread_ts = slack.post_message(messages[0])
    for message in messages[1:]:
        slack.post_message(message, thread_ts=thread_ts)


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


def review_findings(
    comments: Sequence[Mapping[str, Any]], review_id: int, codex_login: str
) -> list[dict[str, Any]]:
    """Select one review's inline Codex findings from the PR's comment list.

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
        asserted = _opening_sentence(statement).rstrip(".*_! ").strip()
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
    events.sort()
    return events


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


def round_history(
    reviewed_heads: Sequence[str],
    reviews: Sequence[Mapping[str, Any]],
    review_comments: Sequence[Mapping[str, Any]],
    codex_login: str,
    *,
    latest_kind_by_head: Mapping[str, str] | None = None,
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
    history: list[dict[str, Any]] = []
    for index, head in enumerate(reviewed_heads, start=1):
        findings = list(findings_by_head.get(head, []))
        kind = latest_kind.get(head)
        if head.startswith(UNRESOLVED_CLEAN_PREFIX) or kind == "clean":
            verdict = (
                "CLEAN (clean comment whose commit no longer resolves)"
                if head.startswith(UNRESOLVED_CLEAN_PREFIX)
                else "CLEAN (Codex clean comment)"
            )
            findings = []
        elif head not in findings_by_head:
            verdict = "CLEAN (Codex clean comment)"
        elif findings:
            verdict = "findings review"
        else:
            verdict = (
                "findings review submitted, 0 inline comments retrievable "
                "(not evidence of CLEAN)"
            )
        history.append(
            {
                "round": index,
                "rounds": len(reviewed_heads),
                "head": head,
                "verdict": verdict,
                "counts": severity_counts(findings),
                "locations": [
                    f"{finding.get('path', '?')}:{finding.get('line', '?')}"
                    for finding in findings
                ],
            }
        )
    return history


def exhaustion_marker(head_sha: str) -> str:
    return f"{EXHAUSTED_MARKER_PREFIX}{head_sha} -->"


def nudge_marker(head_sha: str) -> str:
    return f"{NUDGE_MARKER_PREFIX}{head_sha} -->"


def nudge_comment_body(head_sha: str) -> str:
    """Codex trigger plus an exact-head marker so force-pushes cannot reuse it."""
    return f"@codex review\n{nudge_marker(head_sha)}"


def redelivery_marker(head_sha: str) -> str:
    return f"{REDELIVERY_MARKER_PREFIX}{head_sha} -->"


def redelivery_comment_body(head_sha: str, verdict_at: str) -> str:
    """The once-per-head record that a standing verdict was re-delivered.

    Deliberately carries no ``@codex`` trigger: redelivery re-delivers attention
    to a seat, it never summons another review.  The nudge leg owns summoning.
    """
    return (
        "Review-loop: the standing verdict for this head was re-delivered to "
        f"#hive (original verdict `{verdict_at}`; the head has not moved "
        "since). One redelivery per head — a push starts a fresh review cycle.\n"
        f"{redelivery_marker(head_sha)}"
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


def ensure_comment_at_head(
    github: GitHubApi,
    pr_number: int,
    marker: str,
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
    marker: str,
) -> bool:
    """Trust a control marker only when a trusted control identity authored it."""
    trusted = trusted_control_logins()
    for comment in comments:
        user = comment.get("user")
        if not isinstance(user, Mapping) or user.get("login") not in trusted:
            continue
        if marker in str(comment.get("body") or ""):
            return True
    return False


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
    messages: list[str] = []
    prefix = header
    current: list[str] = []
    used = len(prefix)
    for block in blocks:
        separator = 1 if current else 0
        if current and used + separator + len(block) > budget:
            messages.append(prefix + "\n".join(current))
            prefix = f"({label} continued — part {len(messages) + 1})\n\n"
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
    repository: str,
    author: str,
    author_actor: str,
    has_merge_on_green: bool,
    review_round: int,
) -> list[str]:
    counts = severity_counts(findings)
    verdict = (
        f"Codex findings: {counts['p1']} P1 / {counts['p2']} P2 / "
        f"{counts['p3']} P3 (review state: {review_state})."
    )
    header = (
        "WAKE: talos\n\n"
        "@Talos-burn — load skill `talos-burn` and burn these findings.\n\n"
        f"Review-loop hook: {verdict} {word}\n\n"
        f"PR: {pr_url}\n"
        f"Branch: `{branch}`\n"
        f"Head: `{head_sha}`\n"
        f"Repo: `{repository}`\n"
        f"Author: `{author}` (seat `{author_actor}`)\n"
        f"merge-on-green: `{'yes' if has_merge_on_green else 'no'}`\n\n"
        f"Exact-head review: `yes` — round {review_round}/{MAX_REVIEW_ROUNDS}.\n"
        "Any repair changes the head and invalidates this review closure. Push "
        "one coherent burn, then wait for a fresh exact-head Codex review.\n\n"
        "Doctrine: in-scope findings are fixed; out-of-scope findings become "
        "follow-up tickets, then seek fresh exact-head closure. Never interleave "
        "fixing with merging. "
        "If this seat cannot access the repo, re-WAKE the authoring seat with "
        "the digest intact (R-3: failures stay visible).\n\n"
        "## Finding digest\n"
    )
    return chunk_digest(header, finding_blocks(findings), label="finding digest")


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
        if not counts["total"]:
            blocks.append(f"{title}\nVerdict: {entry['verdict']}.\n")
            continue
        verdict = (
            f"Verdict: {entry['verdict']} — {counts['p1']} P1 / "
            f"{counts['p2']} P2 / {counts['p3']} P3 ({counts['total']} total)."
        )
        loc_lines = location_lines([str(item) for item in entry["locations"]])
        if not loc_lines:
            blocks.append(f"{title}\n{verdict}\n")
            continue
        blocks.append(f"{title}\n{verdict}\n{loc_lines[0]}\n")
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
    repository: str,
    author: str,
    author_actor: str,
    review_round: int,
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
        f"Repo: `{repository}`\n"
        f"Author: `{author}` (seat `{author_actor}`)\n"
        f"Rounds consumed: {review_round} (automatic bound: "
        f"{MAX_REVIEW_ROUNDS}) — a PR whose rounds predate the bound reads "
        "above it.\n\n"
        "The human gate is already on the PR and the authoring seat is already "
        "woken; nothing waits on this verdict.\n\n"
        "What is asked:\n\n"
        "1. **Name the structural cause** — which property was being "
        "established, and why repeated repair could not establish it. "
        '"No structural cause; the finding classes were independent" is a '
        "valid and valuable verdict — some PRs are simply large. Never "
        "conclude from absence: report the gap and stop.\n"
        "2. **Codify what generalises** — a new scar-assert on an existing "
        "skill (preferred; `skills/writing-skills/SKILL.md` §4 makes "
        "refinement the default) or, rarely, a new skill. Provenance is "
        "mandatory: PR, findings, round count, claimed scope. Silence is a "
        "legitimate outcome — a one-off does not earn a corpus entry, and a "
        "corpus of noise is worse than a small one.\n"
        "3. **Post the verdict on the PR** so it is attached to the evidence, "
        "not only to this thread. Skill changes ride a review-ready PR to "
        "weave-doctrine; merge stays Hákon's word.\n\n"
        "The evidence below is the shape of the sequence: did severity fall, "
        "did the same file keep reappearing, did each burn draw new findings.\n\n"
        "## Per-round history, then the current head's findings\n"
    )
    blocks = [
        *round_history_blocks(history, head_sha),
        *finding_blocks(findings),
    ]
    return chunk_digest(header, blocks, label="retrospective evidence")


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
) -> str:
    return (
        f"WAKE: {author_actor}\n\n"
        f"Review-loop hook: Codex review is CLEAN on the exact current head "
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

    if not findings:
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
        marker = exhaustion_marker(head_sha)
        comment_state = ensure_comment_at_head(
            github,
            pr_number,
            marker,
            gate,
            expected_head=head_sha,
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
        history = round_history(
            reviewed_heads,
            [*reviews, review],
            review_comments,
            codex_login,
            latest_kind_by_head=latest_result_kind_by_head(result_events),
        )
        post_threaded_messages(
            slack,
            build_retrospective_messages(
                findings=findings,
                history=history,
                pr_url=pr_url,
                branch=branch,
                head_sha=head_sha,
                repository=repository,
                author=author,
                author_actor=author_actor,
                review_round=review_round,
            ),
        )
        print(
            f"woke theoros for {repository}#{pr_number} retrospective "
            f"(rounds={len(history)})"
        )
        return

    messages = build_burn_messages(
        findings=findings,
        review_state=review_state,
        word=word,
        pr_url=pr_url,
        branch=branch,
        head_sha=head_sha,
        repository=repository,
        author=author,
        author_actor=author_actor,
        has_merge_on_green=has_merge_on_green,
        review_round=review_round,
    )
    for message in messages:
        slack.post_message(message)
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
    if kind in (CODEX_COMMENT_TASK_REPORT, CODEX_COMMENT_CONNECTOR_ERROR):
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


def route_codex_result() -> None:
    """Route either Codex's findings review or its clean issue comment."""
    event = load_event()
    if isinstance(event.get("review"), Mapping):
        route_review(event)
        return
    if isinstance(event.get("comment"), Mapping):
        route_clean_comment(event)
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
    return marker_comment_exists(comments, nudge_marker(head_sha))


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
    return max(stamps) if stamps else None


def standing_findings(
    github: GitHubApi,
    pr_number: int,
    reviews: Sequence[Mapping[str, Any]],
    codex_login: str,
) -> list[dict[str, Any]]:
    """Every inline finding still standing on one unchanged head.

    Reading only the newest review undercounts: a re-review of unchanged bytes
    emits a second review whose inline set can be disjoint from the first.  On a
    head that has not moved, nothing has resolved either set — only a push
    resolves a finding, and a push changes the head — so the standing set is the
    union.  Each inline comment belongs to exactly one review, so the union is a
    concatenation with no deduplication to do.  The comment list is fetched once
    and filtered per review, matching ``review_findings``'s contract.
    """
    review_comments = list(github.paginate(f"pulls/{pr_number}/comments"))
    findings: list[dict[str, Any]] = []
    for review in reviews:
        findings.extend(
            review_findings(review_comments, int(review["id"]), codex_login)
        )
    return findings


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

    "Acted" is a head change: any push produces a new head and a fresh review
    cycle, so an unchanged head past the window is a stall by definition.
    """
    if marker_comment_exists(comments, exhaustion_marker(head_sha)):
        # An exhaustion gate's terminal action is "stop and hold for the human".
        # That is observationally identical to consumed-without-action, and the
        # attention contract's "silence means stop safely" is load-bearing:
        # re-delivering it would convert a deliberate stop into pressure to act.
        return False
    if marker_comment_exists(comments, redelivery_marker(head_sha)):
        return False

    verdict_at = standing_verdict_time(github, reviews, comments, head_sha, codex_login)
    if verdict_at is None:
        print(
            f"no timestamped Codex verdict for {repository}#{pr_number} "
            f"(head={head_sha}); nothing to re-deliver"
        )
        return False
    if (now - verdict_at).total_seconds() < WAKE_REDELIVERY_SECONDS:
        return False
    verdict_stamp = verdict_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    exact_reviews = exact_head_codex_reviews(reviews, head_sha, codex_login)
    # No exact-head review means the verdict came from a clean comment, which
    # carries no inline findings by construction — so this costs no API call.
    findings = (
        standing_findings(github, pr_number, exact_reviews, codex_login)
        if exact_reviews
        else []
    )
    listed_labels = {
        str(label.get("name"))
        for label in pull_request.get("labels", [])
        if isinstance(label, Mapping)
    }
    if not findings and "merge-on-green" not in listed_labels:
        # Cost pre-filter on the listed PR, re-checked authoritatively below: a
        # plain CLEAN head is parked at ready-for-review forever, and paying two
        # lookups per scan for a state that never redelivers is pure waste.
        return False
    if findings and len(reviewed_heads) >= MAX_REVIEW_ROUNDS:
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
    if not findings and not has_merge_on_green:
        # The live label, not the listed one: ready-for-review is a correct
        # parked state, and only merge-on-green CLEAN carries a standing
        # mechanical action to re-deliver.
        return False
    branch = str(refreshed_head["ref"])
    pr_url = str(pr_state.get("html_url") or f"{repository}#{pr_number}")
    review_round = result_round_for_head(reviewed_heads, head_sha)
    word = merge_word(has_merge_on_green)

    if findings:
        messages = build_burn_messages(
            findings=findings,
            review_state=str(exact_reviews[-1].get("state") or "unknown"),
            word=word,
            pr_url=pr_url,
            branch=branch,
            head_sha=head_sha,
            repository=repository,
            author=author,
            author_actor=author_actor,
            has_merge_on_green=has_merge_on_green,
            review_round=review_round,
        )
    else:
        messages = [
            clean_wake_message(
                author_actor=author_actor,
                author=author,
                head_sha=head_sha,
                review_round=review_round,
                review_state=(
                    str(exact_reviews[-1].get("state") or "unknown")
                    if exact_reviews
                    else "clean-comment"
                ),
                word=word,
                pr_url=pr_url,
                branch=branch,
            )
        ]
    messages[0] = f"{redelivery_notice(verdict_stamp)}\n\n{messages[0]}"

    slack = SlackApi(required_env("HIVE_BOT_TOKEN"), required_env("HIVE_CHANNEL"))
    for message in messages:
        slack.post_message(message)
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
    nudged = 0
    redelivered = 0
    for pull_request in github.paginate("pulls", query={"state": "open"}):
        head = pull_request.get("head")
        if not isinstance(head, Mapping):
            continue
        commit = github.get(f"commits/{head['sha']}")
        author = commit_author_name(commit)
        if author not in SEAT_ACTORS:
            continue
        committed_at = review_stall_anchor(commit, pull_request, now)
        if (now - committed_at).total_seconds() < REVIEW_STALL_SECONDS:
            continue
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
                redelivered += 1
            continue
        if len(reviewed_heads) >= MAX_REVIEW_ROUNDS:
            marker = exhaustion_marker(head_sha)
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
            if not marker_comment_exists(comments, marker):
                if (
                    refresh_pr_at_head(
                        github,
                        pr_number,
                        head_sha,
                        repository,
                        action="exhaustion gate",
                    )
                    is None
                ):
                    continue
                github.post(f"issues/{pr_number}/comments", {"body": gate})
                print(f"exhausted PR #{pr_number}")
            continue
        if bare_nudge_covers_head(comments, head_sha):
            continue
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
            continue
        github.post(
            f"issues/{pr_number}/comments", {"body": nudge_comment_body(head_sha)}
        )
        nudged += 1
        print(f"nudged PR #{pr_number}")
    print(f"nudge scan complete (nudged={nudged}, redelivered={redelivered})")


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
