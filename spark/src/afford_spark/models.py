"""Typed result contracts — the output type IS the seam.

Every verb resolves to exactly one of four first-class result states. A utility
has an error value; it does not improvise when it cannot deliver. Exit codes:
``complete``=0, ``incomplete``=3, ``ambiguous``=4, ``refused``=5 (engine and
usage failures use 1/2 via the CLI layer).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResultStatus = Literal["complete", "incomplete", "ambiguous", "refused"]

EXIT_CODES: dict[str, int] = {
    "complete": 0,
    "incomplete": 3,
    "ambiguous": 4,
    "refused": 5,
}


class SparkModel(BaseModel):
    """Closed, frozen base — an extra field in a result is a contract breach."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Match(SparkModel):
    """One evidence coordinate from ``locate``. Spans, never prose."""

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    relationship: str
    evidence: str


class LocateResult(SparkModel):
    status: ResultStatus
    matches: tuple[Match, ...] = ()
    searched_paths: int = 0
    uncertainty: tuple[str, ...] = ()
    reason: str | None = None


SpanRole = Literal["owner", "producer", "consumer", "test", "contract"]


class Span(SparkModel):
    """One span in a ``slice`` packet, with the reason it is in the packet."""

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    role: SpanRole
    why: str

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if self.end_line < self.start_line:
            raise ValueError("end_line precedes start_line")
        return self


class SliceResult(SparkModel):
    """Smallest sufficient context packet for a task — evidence, not a summary.

    ``seam`` names the path that owns the behavior the task is about; ``spans``
    are the minimal reads, each with a role and a why; ``unresolved`` lists
    references the packet needed but could not find inside the path set. The
    wrapper audits every coordinate against the real files.
    """

    status: ResultStatus
    seam: str | None = None
    spans: tuple[Span, ...] = ()
    unresolved: tuple[str, ...] = ()
    searched_paths: int = 0
    reason: str | None = None


class TransformResult(SparkModel):
    status: ResultStatus
    base_sha: str | None = None
    touched_paths: tuple[str, ...] = ()
    patch: str | None = None
    claims: tuple[str, ...] = ()
    reason: str | None = None
    decision_required: str | None = None


class TriageGroup(SparkModel):
    """One root class of related items — the relation, not a summary."""

    label: str
    kind: Literal["duplicate", "downstream", "stale", "same-quantifier", "independent"]
    items: tuple[str, ...]
    rationale: str


class TriageResult(SparkModel):
    status: ResultStatus
    groups: tuple[TriageGroup, ...] = ()
    read_first: tuple[str, ...] = ()
    reason: str | None = None
