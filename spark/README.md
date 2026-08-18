# afford spark

Semantic coreutils over GPT-5.3-Codex-Spark — a bounded coprocessor the seats
call like a syscall. Full contract: [SPEC.md](SPEC.md). Not an agent: no
memory, no delegation, no commits, no verdicts.

```bash
uv sync && uv run afford spark --help
```

First slice (of the nine-verb map in the SPEC):

```bash
# Semantic grep — records with spans, relationships, evidence
afford spark locate "every path where an absent API response becomes a silent default" src/

# One bounded edit — patch out, live checkout untouched, allowlist enforced structurally
afford spark transform "make name required in both functions" src/greet.py --root .

# Unix filter — noisy output in, relation map out
pytest -q 2>&1 | afford spark triage --kind pytest
```

Exit codes: `0` complete · `3` incomplete · `4` ambiguous · `5` refused ·
`1` engine/pool failure (a throttled pool is a plain error, never a silent
fallback to the metered Codex pool) · `2` usage.

Requires an authenticated `codex` CLI whose account carries the Spark
research-preview entitlement. Telemetry (caller, operation, base SHA, allowed
paths, hashes, model, pool, latency, changed files, result state, `transport`
`oneshot|daemon` — never content) appends to
`~/.local/state/afford-spark/telemetry.jsonl` on every invocation, including
failures. `AFFORD_SPARK_TELEMETRY` overrides the path; `AFFORD_SPARK_CALLER`
stamps the caller field. Subsequent verification is null in this slice.

### Warm daemon

Cold `codex exec` pays 20–30s of boot per call. To amortize it:

```bash
uv tool install -e .
systemctl --user link $PWD/systemd/user/afford-sparkd.socket \
                      $PWD/systemd/user/afford-sparkd.service
systemctl --user enable --now afford-sparkd.socket
```

`afford` then talks to the socket (`AFFORD_SPARK_SOCKET` overrides the path).
Each request is a **new conversation, dropped after delivery**. Auth expiry
and a protocol other than v1 fail loud. `AFFORD_SPARK_TRANSPORT=oneshot`
forces the original exec path; `=daemon` refuses to start if the socket is
missing. Default `auto` uses the daemon when the socket exists.
