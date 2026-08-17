# `afford spark` — semantic coreutils over GPT-5.3-Codex-Spark

Design ratified by Hákon 2026-08-17 (verbatim intent preserved; this file is the
build contract). Spark is OpenAI's Cerebras-served real-time Codex variant:
text-only, 128k context, >1000 tok/s, research preview, **its own subscription
usage pool** separate from the main Codex pool. The pool otherwise expires
unused.

**The abstraction: a learned Unix utility — a semantic coprocessor invoked by
Fable and Ariadne to perform one bounded transformation and then disappear.**
Not an agent. Never an agent.

```
explicit inputs + explicit operation
    → typed result, evidence, or patch
    → meaningful exit status
```

No memory. No delegation. No deciding what task to do next. No commits. No
pushes. No open-ended repository wandering. The caller owns purpose and
acceptance; Spark performs the expensive fuzzy operation in the middle.

**Do NOT expose a general `ask` command** — it metastasizes back into an agent.

## Verb family (full map; build order below)

| Verb | One line |
|---|---|
| `locate` | semantic grep: relational queries ("every producer of this state", "all consequence-copies of one doctrine rule") → records with path/span/relationship/evidence |
| `slice` | smallest sufficient context packet for a task: owning seam, minimal spans, producers/consumers/tests/contracts, why each span, unresolved refs. Loss-minimized evidence packet, never a repo summary |
| `drift` | semantic consistency: do two artifacts still assert the same contract despite different forms (Pydantic↔OpenAPI↔CUE↔SQL, law↔call-sites, skill↔wake payload, PR body↔diff, comment↔code, declared terminal state↔what tests establish) |
| `transform` | ONE bounded edit on an explicit file set, returned as a patch + claims; `ambiguous` with `decision_required` is a first-class outcome — a utility has an error value, it does not improvise architecture |
| `codemod` | compile before/after examples + invariants into a DETERMINISTIC codemod (ast-grep rule, LibCST transformer, script). Spark infers the transformation; deterministic code enumerates and applies; existing gates verify. The model is a codemod compiler, not the codemod runtime |
| `triage` | Unix filter: pytest/diff/findings JSONL in → relation map out (duplicate failures sharing a cause, stale vs current-bytes findings, instances of one unclosed quantifier, smallest set worth reading). Prepares the burn packet, NEVER decides the verdict |
| `falsify` | compile a prose claim into the cheapest discriminating probe: focused regression test, mutation that should break an existing test, race sequence, input partition table. "Scars are asserts" compiler |
| `query` | compile natural language into another Unix language: `rg`, `ast-grep`, `jq`, SQL, Logfire, Git, CUE. Model emits query + assumptions + expected shape; deterministic engine executes |
| `claims` | specialized drift over a diff (`--base origin/main --head HEAD`): behavioral claims introduced, universal quantifiers now owned, new durable states, added preconditions, affected callers/consequence-copies, PR-body claims no longer true at HEAD |

Read-only half (Fable-leaning): locate, slice, drift, triage, claims, issue-prep.
Transformation half (Ariadne-leaning): transform, codemod, finding→patch,
falsifier generation, semantic rename rejecting homonyms.

## First vertical slice — build ONLY these three

```
locate     semantic path-and-span search
transform  bounded patch generation in an ephemeral worktree
triage     structured compression of logs, diffs, and findings
```

(`slice` is predicted to become the most valuable verb, `codemod` the coolest —
but these three establish whether Spark deserves the rest.)

### Output contracts (primitive on the model side, typed on ours)

`locate` returns records, not prose:

```json
{
  "status": "complete",
  "matches": [
    {"path": "src/edge/example.ts", "start_line": 117, "end_line": 139,
     "relationship": "converts absence to fallback identity", "evidence": "..."}
  ],
  "searched_paths": 84,
  "uncertainty": []
}
```

`transform` returns either a patch:

```json
{"status": "complete", "base_sha": "...", "touched_paths": [...],
 "patch_path": "...", "claims": ["No exported signature changed", "..."]}
```

or a refusal with a decision:

```json
{"status": "ambiguous",
 "reason": "Two incompatible ownership contracts are visible",
 "decision_required": "Choose whether identity belongs to claim or provider start"}
```

`complete`, `incomplete`, `ambiguous`, `refused` are first-class result states
everywhere. No silent guessing.

## Execution substrate

`codex exec` non-interactive mode: stdin prompt, model selection, JSONL
emission, final-response JSON-Schema validation, separate last-message file,
read-only / workspace-write sandboxes.

```
codex exec \
  --model gpt-5.3-codex-spark \
  --profile spark-utility \
  --sandbox read-only \
  --json \
  --output-schema result.schema.json \
  --output-last-message result.json \
  -
```

Resolve the exact model identifier once through the Codex model catalog at
startup — do not assume the alias forever. Preflight entitlement + pool state;
throttled/absent → nonzero exit with a plain message, NEVER a silent fallback
to the metered pool. Spark silently ignores reasoning config — carry none.

### Write protocol (transform only)

1. Ephemeral worktree at the declared base SHA.
2. `workspace-write` granted only there.
3. Explicit path allowlist.
4. Spark edits.
5. Capture patch + hashes.
6. Destroy the worktree.
7. Return the patch; the CALLING seat applies it and runs the real gates.

Spark never touches a seat's live checkout.

## Hard rules (enforced structurally by the wrapper)

- Read-only by default.
- No generic free-form command in the public interface.
- No session resume; each invocation starts empty.
- No MCP servers, web access, or external integrations.
- No committing, pushing, ticketing, or messaging.
- No widening the supplied path set.
- No running tests unless the verb is explicitly a test/probe verb.
- No output without evidence coordinates or a patch.
- No direct multi-file mass rewrite when a deterministic codemod can be emitted.
- No final architectural, review, or acceptance verdicts.
- Telemetry on every invocation, including pool refusals, protocol errors, and
  timeouts (written in a `finally`, classified as the state). v1 record:
  caller (`AFFORD_SPARK_CALLER` or null), operation, base SHA, allowed paths,
  input hash, model, pool (`spark`), latency, output hash, changed files
  (wrapper-audited on `transform`; null otherwise), result state, subsequent
  verification outcome (always null this slice — a later correlator fills it),
  `transport` (`oneshot` | `daemon`). A few hundred calls tell us empirically
  which verbs Spark deserves.

## Warm daemon (`afford-sparkd`) — second slice

Boot of `codex exec` dominates a cold locate (field-measured 20–30s). The
second slice keeps the wrapper contract and amortizes boot:

- `afford-sparkd` is a socket-activated systemd **user** unit that holds one
  warm `codex app-server` process.
- `afford` is a thin unix-socket client when the socket is present
  (`$AFFORD_SPARK_SOCKET`, else `$XDG_RUNTIME_DIR/afford-sparkd/sparkd.sock`).
  `AFFORD_SPARK_TRANSPORT=oneshot|daemon|auto` (default `auto`: socket if
  connectable, otherwise oneshot `codex exec`). A leftover socket file
  with no listener is oneshot, not a hard-down.
- **Fresh conversation per request, dropped after delivery.** Isolation is the
  conversation layer, not process recycling. Consecutive calls must not share
  a thread.
- Protocol is pinned at v1. A different version fails loud — it is not a
  silent fallback to oneshot.
- Auth expiry fails loud (`SparkUnavailableError`); the daemon does not retry
  and does not fall through to the metered Codex pool.
- Each request has its own timeout (default 300s). The daemon is strictly
  serial (one app-server turn at a time). The budget starts when the request
  is received and includes time queued behind a predecessor; a hung turn
  cannot stall the next caller past that budget — the queued caller receives
  a typed timeout instead of waiting on the client's blind deadline.

Units live in `spark/systemd/user/`. Enable with
`systemctl --user enable --now afford-sparkd.socket`.

## Later: the quota scavenger (NOT in the first slice)

A scheduler over a finite queue of declared read-only jobs — not an agent:

```
when Spark remaining > threshold
and reset_at - now < scavenging_window
then execute the next declared maintenance job
```

Jobs: annotate changed blobs (purpose/side-effects/invariants/producers/
consumers), refresh semantic file-routing indexes, precompute context packets
for ready-for-agent issues, schema-drift scans, behavioral-vs-implementation
test classification, duplicate-contract detection, code-contradicted comments,
belt-finding scar clustering, repeated-edit-shape detection, module capsules.

Results are an ADVISORY CACHE, content-addressed by: repository, base commit,
input blob SHAs, operation + schema version, prompt-contract hash, model id.
Changed bytes invalidate automatically; no generated prose becomes truth.

## Eval corpus — seed with problems the belt already paid for

Consequence-copy drift · producer enumeration misses · caller identity inferred
from row state · an optional state that should be unrepresentable · a comment
fixed while executable text stayed · a guard widened without counting newly
reached cells · tests asserting implementation not behavior · a stale PR-body
design claim.

Measure: `locate` recall/precision + evidence-coordinate validity; `transform`
patch application rate, out-of-scope edit count, gate pass rate; `triage`
root-class grouping accuracy + information retained per token.

## House style

Python 3.13 + Typer + uv, full type hints, Pydantic v2 result models (the
output type IS the seam), ruff + ty, pytest. Lives here in `agent-affordances`
under `spark/` — no new repository.
