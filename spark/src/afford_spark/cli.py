"""`afford spark <verb>` — semantic coreutils. See SPEC.md for the contract.

Exit codes: 0 complete · 3 incomplete · 4 ambiguous · 5 refused ·
1 engine/pool failure · 2 usage error.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from afford_spark.engine import SparkUnavailableError, run_spark
from afford_spark.models import (
    EXIT_CODES,
    LocateResult,
    TransformResult,
    TriageResult,
)
from afford_spark.prompts import locate_prompt, transform_prompt, triage_prompt

app = typer.Typer(no_args_is_help=True, help="Affordance CLIs for the Weave seats.")
spark = typer.Typer(no_args_is_help=True, help="Semantic coreutils over Codex-Spark.")
app.add_typer(spark, name="spark")

MAX_INLINE_BYTES = 360_000  # ~90k tokens: stays inside Spark's 128k window with headroom


def _emit(result: LocateResult | TransformResult | TriageResult) -> None:
    typer.echo(result.model_dump_json(indent=2))
    raise typer.Exit(EXIT_CODES[result.status])


def _die(message: str) -> None:
    typer.echo(f"afford spark: {message}", err=True)
    raise typer.Exit(1)


def _resolve_paths(paths: list[Path], root: Path) -> list[str]:
    """Expand the caller's path set to concrete files, refusing escapes."""
    files: list[str] = []
    for p in paths:
        absolute = (root / p).resolve() if not p.is_absolute() else p.resolve()
        if not absolute.is_relative_to(root):
            _die(f"path escapes the working root: {p}")
        if absolute.is_dir():
            files.extend(
                str(f.relative_to(root))
                for f in sorted(absolute.rglob("*"))
                if f.is_file() and ".git" not in f.parts
            )
        elif absolute.is_file():
            files.append(str(absolute.relative_to(root)))
        else:
            _die(f"no such path: {p}")
    if not files:
        _die("the resolved path set is empty")
    return files


@spark.command()
def locate(
    question: Annotated[str, typer.Argument(help="The semantic/relational question.")],
    paths: Annotated[list[Path], typer.Argument(help="Files or directories in scope.")],
    root: Annotated[
        Path | None, typer.Option("--root", help="Working root the paths live under.")
    ] = None,
) -> None:
    """Semantic grep: spans + relationships + evidence, never prose."""
    root = (root or Path.cwd()).resolve()
    files = _resolve_paths(paths, root)
    try:
        result = run_spark(
            locate_prompt(question, files),
            verb="locate",
            workdir=root,
            schema=LocateResult,
        )
    except SparkUnavailableError as exc:
        _die(str(exc))
    except Exception as exc:
        _die(f"locate failed: {exc}")
    _emit(result)


@spark.command()
def transform(
    rule: Annotated[str, typer.Argument(help="The exact transformation to perform.")],
    paths: Annotated[list[Path], typer.Argument(help="The ONLY files Spark may edit.")],
    root: Annotated[Path | None, typer.Option("--root", help="Git repository root.")] = None,
    base: Annotated[
        str, typer.Option("--base", help="Base rev for the ephemeral worktree.")
    ] = "HEAD",
) -> None:
    """One bounded edit in an ephemeral worktree; returns a patch, never writes here."""
    root = (root or Path.cwd()).resolve()
    if not (root / ".git").exists():
        _die(f"--root must be a git repository: {root}")
    base_sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", base],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not base_sha:
        _die(f"cannot resolve base rev {base!r} in {root}")

    # Normalize so "./a.py" and "a.py" compare equal against git's numstat paths.
    allow = [str(Path(p)) for p in paths]
    with tempfile.TemporaryDirectory(prefix="afford-spark-wt-") as tmp:
        wt = Path(tmp) / "wt"
        add = subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(wt), base_sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if add.returncode != 0:
            _die(f"worktree creation failed: {add.stderr.strip()}")
        try:
            missing = [p for p in allow if not (wt / p).is_file()]
            if missing:
                _die(f"allowlisted paths absent at {base_sha[:8]}: {', '.join(missing)}")
            try:
                result = run_spark(
                    transform_prompt(rule, allow, base_sha),
                    verb="transform",
                    workdir=wt,
                    schema=TransformResult,
                    writable=True,
                )
            except SparkUnavailableError as exc:
                _die(str(exc))
            except Exception as exc:
                _die(f"transform failed: {exc}")

            diff = subprocess.run(
                ["git", "-C", str(wt), "diff", "--patch"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            touched = [
                line.split("\t", 2)[2]
                for line in subprocess.run(
                    ["git", "-C", str(wt), "diff", "--numstat"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.splitlines()
                if "\t" in line
            ]
            out_of_scope = sorted(set(touched) - set(allow))
            if out_of_scope:
                # Structural allowlist enforcement: an edit outside the set is
                # a refusal regardless of what the model claimed.
                result = TransformResult(
                    status="refused",
                    base_sha=base_sha,
                    touched_paths=tuple(touched),
                    reason=f"edited outside the allowlist: {', '.join(out_of_scope)}",
                )
            elif result.status == "complete" and not diff.strip():
                result = TransformResult(
                    status="incomplete",
                    base_sha=base_sha,
                    reason="model claimed completion but the worktree diff is empty",
                )
            elif result.status == "complete":
                result = TransformResult(
                    status="complete",
                    base_sha=base_sha,
                    touched_paths=tuple(touched),
                    patch=diff,
                    claims=result.claims,
                )
        finally:
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
                check=False,
            )
    _emit(result)


@spark.command()
def triage(
    kind: Annotated[str, typer.Option("--kind", help="pytest | diff | findings | log")],
) -> None:
    """Unix filter: noisy stdin in, relation map out. Prepares packets, never verdicts."""
    content = sys.stdin.read()
    if not content.strip():
        _die("nothing on stdin to triage")
    if len(content.encode()) > MAX_INLINE_BYTES:
        content = content[: MAX_INLINE_BYTES // 2] + "\n\n[TRUNCATED BY WRAPPER]\n"
    with tempfile.TemporaryDirectory(prefix="afford-spark-triage-") as tmp:
        try:
            result = run_spark(
                triage_prompt(kind, content),
                verb="triage",
                workdir=Path(tmp),
                schema=TriageResult,
            )
        except SparkUnavailableError as exc:
            _die(str(exc))
        except Exception as exc:
            _die(f"triage failed: {exc}")
    _emit(result)


if __name__ == "__main__":
    app()
