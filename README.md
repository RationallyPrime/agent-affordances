# agent-affordances

Agent tooling useful beyond any single project. Each tool lives in its own
directory as an independent uv project.

| Tool | What it does |
| --- | --- |
| [`linear-reads`](linear-reads/) | Token-lean read-only CLI for Linear (issue lookups, board sweeps, label queries) |

## House standards

- uv + `pyproject.toml`, Python 3.13
- Typer for CLIs, httpx for HTTP (never requests), pydantic v2 for models
- ruff (lint + format) and ty (types), full type hints
- Output designed for agent consumption: token-lean, pipe-friendly
