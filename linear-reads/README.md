# linear-reads

Token-lean **read-only** CLI for Linear, built for agent consumption. Writes
stay in the Linear MCP — this tool exists because reads are the bulk of token
spend, and an agent piping `linear-reads` output pays for exactly the fields it
asked for and nothing else: the `--fields` list maps 1:1 onto the GraphQL
selection set, and rendering is plain text or compact JSONL (no box-drawing,
no colors, no headers).

## Install

```sh
uv tool install ./linear-reads   # from the repo root
# or, for development:
cd linear-reads && uv sync
```

## Auth & defaults

| Env var | Meaning |
| --- | --- |
| `LINEAR_API_KEY` | Personal API key; wins over any file |
| `LINEAR_API_KEY_FILE` | Explicit path to a key file; a missing path is an error |
| `LINEAR_TEAM` | Default team key for `issues` and `states` |

With no env var the key is read from the seat's profile key file — the Weave
`<profile>/secrets/<name>` convention — at the first of:

1. `$CLAUDE_CONFIG_DIR/secrets/linear_api_key` (a Claude Code seat's pinned profile)
2. `$CODEX_HOME/secrets/linear_api_key` (a Codex seat's)
3. `~/.claude/secrets/linear_api_key` (an unpinned machine)

The file holds the key alone, one line, **mode 600** — a file readable by
group or world is refused, not used. Place it with:

```sh
install -d -m 700 ~/.claude/secrets
umask 077 && pbpaste > ~/.claude/secrets/linear_api_key   # key on the clipboard
```

The error for a missing key names every path that was consulted.

## Commands

```sh
linear-reads issue KRA-123            # header + title + raw markdown body
linear-reads issue KRA-123 --no-body --comments
linear-reads issues                   # sweep, defaults to $LINEAR_TEAM
linear-reads issues --state started --assignee me --updated-since 7d
linear-reads issues --label bug --project "Wake router" --query dedupe --limit 20
linear-reads comments KRA-123
linear-reads teams / states / labels / projects / users   # metadata lookups
```

Filters combine with AND. `--assignee me` uses the API key's identity.
`--updated-since` accepts `30m`, `12h`, `7d`, `2w`, or an ISO date.
`issue --comments` follows the complete comment connection; the standalone
`comments` command uses its explicit `--limit` (default 50).

## Output

`--format table|jsonl|json`. Default is **table on a TTY, JSONL when piped**,
so agents get machine-readable lines without asking:

```
$ linear-reads issues --state started
KRA-142  started  gnomon  Fix wake dedupe race
KRA-138  started  -       Token audit for MCP reads
2 issues

$ linear-reads issues --state started | head -1
{"id":"KRA-142","state":"started","assignee":"gnomon","title":"Fix wake dedupe race"}
```

`--fields` controls both the query and the output. Valid issue fields:
`id, uuid, title, state, assignee, labels, project, team, priority, estimate,
created, updated, url, body`.

```sh
linear-reads issues --fields id,estimate,updated --format jsonl
```

## Development

```sh
uv sync
uv run pytest
uv run ruff format --check . && uv run ruff check .
uv run ty check
```

Tests run against mocked httpx transports; no API key or network needed.
