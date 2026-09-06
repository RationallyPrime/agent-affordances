"""Prompt contracts per verb — the boundary rules live IN the prompt too.

The wrapper enforces the hard rules structurally (sandbox, allowlist audit,
schema validation); the prompt restates them because Spark has no reasoning
phase to infer them. Both layers exist on purpose.
"""

from __future__ import annotations

BOUNDARY = """\
You are a bounded utility, not an agent. Rules that override everything else:
- Examine ONLY the paths listed below. Never widen the set.
- Do not run tests, commands, or tools beyond reading the listed files.
- Do not summarize, editorialize, or propose next steps.
- If you cannot deliver, return status "ambiguous" or "refused" with a reason —
  never guess silently.
- Your entire final message must be a single JSON object matching the provided
  schema. No prose outside it.
"""


def locate_prompt(question: str, paths: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in paths)
    return f"""{BOUNDARY}
Operation: LOCATE — semantic search over the listed paths.

Question:
{question}

Paths in scope:
{listed}

Return every code span that answers the question as a match record:
path, start_line, end_line (1-indexed, from the actual file), a short
"relationship" phrase naming HOW the span answers the question, and an
"evidence" quote of the decisive line(s). Set searched_paths to the number of
files you actually examined. List genuinely uncertain candidates in
"uncertainty" instead of forcing them into matches. Zero matches with
status "complete" is a valid answer.
"""


def slice_prompt(task: str, paths: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in paths)
    return f"""{BOUNDARY}
Operation: SLICE — the smallest sufficient context packet for a task.

Task the caller is about to perform:
{task}

Paths in scope:
{listed}

Name the owning seam: the single path whose code owns the behavior the task
is about (null only when status is not "complete"). Then return the minimal
set of spans a reader must have in front of them to do the task correctly,
each as path, start_line, end_line (1-indexed, from the actual file), a role
("owner" — the seam itself; "producer" — writes the state or value involved;
"consumer" — reads or depends on it; "test" — establishes current behavior;
"contract" — a schema, type, doctrine rule or docstring the task must honor),
and one short "why" naming what the reader loses without that span. Prefer
fewer, tighter spans over wide ones; never include a span you cannot name a
loss for. Do NOT summarize the repository or describe the files — the packet
is coordinates and reasons only. List every symbol or path the packet depends
on that is NOT in the listed paths under "unresolved" instead of guessing.
Set searched_paths to the number of files you actually examined. Return
status "ambiguous" with a reason when two different seams could own the task
and the task text does not decide between them.
"""


def transform_prompt(rule: str, paths: list[str], base_sha: str) -> str:
    listed = "\n".join(f"- {p}" for p in paths)
    return f"""{BOUNDARY}
Operation: TRANSFORM — one bounded edit, applied to the working tree.

Transformation rule:
{rule}

Files you may edit (the ONLY files you may touch):
{listed}

Base commit: {base_sha}

Apply the rule by editing the listed files in place. Perform no design work:
if the rule underdetermines the edit — two incompatible contracts are visible,
or the rule's precondition does not hold — edit NOTHING and return status
"ambiguous" with "decision_required" naming the single choice a human must
make. On success return status "complete" with touched_paths and 1-5 short
"claims" stating what provably did and did not change (e.g. "no exported
signature changed"). Leave "patch" null — the wrapper captures the diff itself.
"""


def triage_prompt(kind: str, content: str) -> str:
    return f"""{BOUNDARY}
Operation: TRIAGE — structured compression of noisy output (kind: {kind}).

Input follows the marker line. Group the items into relation groups:
- "duplicate": failures/findings sharing one cause
- "downstream": likely caused by an earlier item in the input
- "stale": asserts something no longer true of the current bytes (only when the
  input itself proves it)
- "same-quantifier": instances of one unclosed universal claim
- "independent": stands alone
Each group: a short label, its member items (quoted identifiers or first
lines), and one-sentence rationale naming the shared cause. Then "read_first":
the smallest ordered set of items worth reading before acting. You prepare the
packet; you never decide the verdict.

INPUT:
{content}
"""
