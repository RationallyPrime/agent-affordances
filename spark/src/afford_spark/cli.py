"""`afford spark <verb>` — semantic coreutils. See SPEC.md for the contract.

Exit codes: 0 complete · 3 incomplete · 4 ambiguous · 5 refused ·
1 engine/pool failure · 2 usage error.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

import typer

from afford_spark.engine import (
    SparkProtocolError,
    SparkUnavailableError,
    emit_invocation,
    invocation_record,
    run_spark,
)
from afford_spark.models import (
    EXIT_CODES,
    LocateResult,
    ResultStatus,
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


def _usage(message: str) -> None:
    typer.echo(f"afford spark: {message}", err=True)
    raise typer.Exit(2)


def _under_root(path: Path, root: Path) -> Path:
    """Resolve ``path`` against ``root`` and refuse anything that escapes it."""
    absolute = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not absolute.is_relative_to(root):
        _usage(f"path escapes the working root: {path}")
    return absolute


def _git(wt: Path, *args: str) -> str:
    """Git in ``wt``. Path listings must pass ``-z``; ``quotePath`` is not enough."""
    return subprocess.run(
        ["git", "-C", str(wt), "-c", "core.quotePath=false", *args],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def _git_paths(wt: Path, *args: str) -> list[str]:
    """Split a ``-z`` git path listing. ``args`` must include ``-z``."""
    return [p for p in _git(wt, *args).split("\0") if p]


def _expand_dir(absolute: Path, root: Path) -> list[str]:
    """Tracked files under ``absolute``; ``rglob`` only outside a work tree."""
    if _git(root, "rev-parse", "--is-inside-work-tree").strip() == "true":
        rel = absolute.relative_to(root).as_posix()
        return _git_paths(root, "ls-files", "-z", "--", rel if rel != "." else ".")
    return [
        str(f.relative_to(root))
        for f in sorted(absolute.rglob("*"))
        if f.is_file() and ".git" not in f.parts
    ]


def _resolve_paths(paths: list[Path], root: Path) -> list[str]:
    """Expand the caller's path set to concrete files, refusing escapes."""
    files: list[str] = []
    for p in paths:
        absolute = _under_root(p, root)
        if absolute.is_dir():
            files.extend(_expand_dir(absolute, root))
        elif absolute.is_file():
            files.append(str(absolute.relative_to(root)))
        else:
            _usage(f"no such path: {p}")
    if not files:
        _usage("the resolved path set is empty")
    return files


def _worktree_changes(wt: Path, base_sha: str) -> tuple[str, list[str]]:
    """Working-tree changes vs ``base_sha``, including staged, committed, untracked, ignored."""
    diff = _git(wt, "diff", "--patch", base_sha)
    touched: list[str] = []
    for rec in _git_paths(wt, "diff", "--numstat", "-z", base_sha):
        parts = rec.split("\t", 2)
        path = parts[2] if len(parts) == 3 else rec
        if path and path not in touched:
            touched.append(path)
    for path in (
        *_git_paths(wt, "ls-files", "-z", "--others", "--exclude-standard"),
        *_git_paths(wt, "ls-files", "-z", "--others", "--ignored", "--exclude-standard"),
    ):
        if path not in touched:
            touched.append(path)
    return diff, touched


def _owned_transform(
    model: TransformResult,
    *,
    base_sha: str,
    allow: list[str],
    diff: str,
    touched: list[str],
) -> TransformResult:
    """Rebuild every branch so the wrapper, not the model, owns structural fields."""
    out_of_scope = sorted(set(touched) - set(allow))
    patch = diff if diff.strip() else None
    if out_of_scope:
        return TransformResult(
            status="refused",
            base_sha=base_sha,
            touched_paths=tuple(touched),
            patch=patch,
            reason=f"edited outside the allowlist: {', '.join(out_of_scope)}",
        )
    if model.status == "complete" and patch is None:
        return TransformResult(
            status="incomplete",
            base_sha=base_sha,
            touched_paths=tuple(touched),
            reason="model claimed completion but the worktree diff is empty",
        )
    if model.status == "complete":
        return TransformResult(
            status="complete",
            base_sha=base_sha,
            touched_paths=tuple(touched),
            patch=diff,
            claims=model.claims,
        )
    status: ResultStatus = model.status
    return TransformResult(
        status=status,
        base_sha=base_sha,
        touched_paths=tuple(touched),
        patch=patch,
        claims=model.claims,
        reason=model.reason,
        decision_required=model.decision_required,
    )


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
            allowed_paths=files,
            repo=root,
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
        _usage(f"--root must be a git repository: {root}")
    # Refuse escapes against the live root before any worktree or model call.
    allow = [str(_under_root(p, root).relative_to(root)) for p in paths]
    if not allow:
        _usage("the resolved path set is empty")
    base_sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", base],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not base_sha:
        _usage(f"cannot resolve base rev {base!r} in {root}")

    prompt = transform_prompt(rule, allow, base_sha)
    started = time.monotonic()
    record = invocation_record(
        verb="transform",
        prompt=prompt,
        workdir=root,
        writable=True,
        base_sha=base_sha,
        allowed_paths=allow,
        repo=root,
    )
    status: str | None = None
    raw: str | None = None
    changed: list[str] | None = None
    result: TransformResult | None = None
    with tempfile.TemporaryDirectory(prefix="afford-spark-wt-") as tmp:
        wt = Path(tmp) / "wt"
        try:
            add = subprocess.run(
                ["git", "-C", str(root), "worktree", "add", "--detach", str(wt), base_sha],
                capture_output=True,
                text=True,
                check=False,
            )
            if add.returncode != 0:
                status = "error"
                _die(f"worktree creation failed: {add.stderr.strip()}")
            missing = [p for p in allow if not (wt / p).is_file()]
            if missing:
                status = "error"
                _usage(f"allowlisted paths absent at {base_sha[:8]}: {', '.join(missing)}")
            try:
                model = run_spark(
                    prompt,
                    verb="transform",
                    workdir=wt,
                    schema=TransformResult,
                    writable=True,
                    base_sha=base_sha,
                    allowed_paths=allow,
                    repo=root,
                    emit_telemetry=False,
                )
            except SparkUnavailableError as exc:
                status = "unavailable"
                _die(str(exc))
            except SparkProtocolError as exc:
                status = "protocol_error"
                _die(f"transform failed: {exc}")
            except Exception as exc:
                status = "error"
                _die(f"transform failed: {exc}")

            diff, touched = _worktree_changes(wt, base_sha)
            result = _owned_transform(
                model, base_sha=base_sha, allow=allow, diff=diff, touched=touched
            )
            status = result.status
            changed = list(result.touched_paths)
            raw = result.model_dump_json()
        finally:
            try:
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "remove", "--force", str(wt)],
                    capture_output=True,
                    check=False,
                )
            finally:
                inflight = sys.exc_info()[0]
                try:
                    emit_invocation(
                        record, started=started, status=status, raw=raw, changed_files=changed
                    )
                except OSError as exc:
                    typer.echo(f"afford spark: telemetry write failed: {exc}", err=True)
                    if inflight is None:
                        raise typer.Exit(1) from exc
    if result is None:
        _die("transform produced no result")
    _emit(result)


@spark.command()
def triage(
    kind: Annotated[str, typer.Option("--kind", help="pytest | diff | findings | log")],
) -> None:
    """Unix filter: noisy stdin in, relation map out. Prepares packets, never verdicts."""
    content = sys.stdin.read()
    if not content.strip():
        _usage("nothing on stdin to triage")
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
