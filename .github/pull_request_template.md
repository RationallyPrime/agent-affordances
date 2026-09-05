<!-- Weave PR conventions:
  - Title: conventional-commit format (feat:/fix:/chore:/docs:/refactor: ...)
    AND a descriptive name — the title says what the change is for and does.
    A reader seeing only the title in a Slack line or merge queue knows what
    this PR is. Bare mechanics ("fix tests") and bare ticket ids are not titles.
  - A PR that resolves a Linear ticket MUST carry `Closes KRA-<n>` in the body
    (a bare id only links; Closes resolves).
  - Failures stay visible (R-3): red output goes in the PR verbatim, never
    summarized away.
-->

## What

<!-- The change itself: what is different after merge. -->

## Why

<!-- The cause: ticket, incident, audit finding, doctrine. -->

## Author pass — done BEFORE summoning any reviewer

<!-- The belt is a safety net, not a first reader. Every unchecked box is a
finding you chose to buy at review price instead of author price. -->

- [ ] **Adversarial self-read of the full diff** — read it as the reviewer
      will, hunting for what's wrong. Prose the diff contradicts (docstrings,
      READMEs, comments, shipped YAML) updated in this same pass.
- [ ] **Reviewer-grade gates run at THIS head** — the gates CI and the
      reviewer will run, listed below with honest results; green claims come
      from the pushed head's check-run, never a local run on different bytes.
- [ ] **Blast radius matched** — a shared-core or cross-package change ran the
      FULL matrix, not just the touched package's suite; a runbook or skill
      change traced its commands against the real machines they name.
- [ ] **Terminal state named** — the body says what "done" looks like (which
      checks, which verdict, whose merge authority), so a verifier can testify
      without asking.

## Gates

<!-- Name the gates that ran AND the ones you deliberately skipped, with the
reason a skipped gate could not have caught anything in this diff. "Not run —
<reason>" is a legal entry; silence is not. e.g. "prek pre-push (ty,
check-imports) passed; full pytest skipped — no Python touched, no test
observes this diff." -->

## Review scope

<!-- Weave-internal single-operator tools (hive, swifties, weave-doctrine, …):
reliability findings only — no secrecy ceremony. Delete this section in
public/customer-facing repos, where the full threat model applies. -->

Closes KRA-
