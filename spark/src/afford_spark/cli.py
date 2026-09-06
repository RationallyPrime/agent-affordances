"""`afford spark <verb>` — semantic coreutils. See SPEC.md for the contract.

Exit codes: 0 complete · 3 incomplete · 4 ambiguous · 5 refused ·
1 engine/pool failure · 2 usage error.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from afford_spark.engine import (
    SparkProtocolError,
    SparkUnavailableError,
    choose_transport,
    emit_invocation,
    invocation_record,
    run_spark,
)
from afford_spark.models import (
    EXIT_CODES,
    LocateResult,
    ResultStatus,
    SliceResult,
    Span,
    TransformResult,
    TriageResult,
)
from afford_spark.prompts import locate_prompt, slice_prompt, transform_prompt, triage_prompt

app = typer.Typer(no_args_is_help=True, help="Affordance CLIs for the Weave seats.")
spark = typer.Typer(no_args_is_help=True, help="Semantic coreutils over Codex-Spark.")
app.add_typer(spark, name="spark")

MAX_INLINE_BYTES = 360_000  # ~90k tokens: stays inside Spark's 128k window with headroom


def _emit(result: LocateResult | SliceResult | TransformResult | TriageResult) -> None:
    typer.echo(result.model_dump_json(indent=2))
    raise typer.Exit(EXIT_CODES[result.status])


def _die(message: str) -> NoReturn:
    typer.echo(f"afford spark: {message}", err=True)
    raise typer.Exit(1)


def _usage(message: str) -> NoReturn:
    typer.echo(f"afford spark: {message}", err=True)
    raise typer.Exit(2)


def _wrapper_owned_record(
    *,
    verb: str,
    prompt: str,
    root: Path,
    writable: bool,
    allow: list[str],
    base_sha: str | None = None,
    started: float,
) -> dict[str, object]:
    """The telemetry record a verb whose wrapper re-derives the result owns itself.

    ``run_spark``'s own emission carries the model's self-reported status and a
    hash of its raw output. ``slice`` and ``transform`` audit that answer and
    can downgrade it, so both pass ``emit_telemetry=False`` and publish through
    ``_publish_invocation`` after the audit — one record, describing the result
    the caller actually receives. A transport that will not resolve is recorded
    here because no invocation will follow to record it.
    """

    def _record(transport: str | None) -> dict[str, object]:
        return invocation_record(
            verb=verb,
            prompt=prompt,
            workdir=root,
            writable=writable,
            base_sha=base_sha,
            allowed_paths=allow,
            repo=root,
            transport=transport,
        )

    try:
        transport = choose_transport()
    except SparkProtocolError as exc:
        with contextlib.suppress(OSError):
            emit_invocation(_record(None), started=started, status="protocol_error")
        _die(str(exc))
    return _record(transport)


def _publish_invocation(
    record: dict[str, object],
    *,
    started: float,
    status: str | None,
    raw: str | None = None,
    changed_files: list[str] | None = None,
) -> None:
    """Emit the audited record. A write failure is loud, and terminal on its own,
    but it never masks an exit already in flight."""
    inflight = sys.exc_info()[0]
    try:
        emit_invocation(
            record, started=started, status=status, raw=raw, changed_files=changed_files
        )
    except OSError as exc:
        typer.echo(f"afford spark: telemetry write failed: {exc}", err=True)
        if inflight is None:
            raise typer.Exit(1) from exc


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
    """Tracked + untracked-but-not-ignored files under ``absolute``; ``rglob``
    only outside a work tree. ``--exclude-standard`` is what keeps ``.venv``
    and caches out — dropping ``--others`` would also drop the file an agent
    just wrote, and an empty ``complete`` would read as evidence of absence."""
    if _git(root, "rev-parse", "--is-inside-work-tree").strip() == "true":
        rel = absolute.relative_to(root).as_posix()
        listed = _git_paths(
            root,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            rel if rel != "." else ".",
        )
    else:
        listed = [
            str(f.relative_to(root))
            for f in sorted(absolute.rglob("*"))
            if f.is_file() and ".git" not in f.parts
        ]
    # Git lists tracked symlinks as files. A link's bytes are its target's, and
    # the target may live outside the root, so links are dropped here: an
    # in-root target is listed on its own, an out-of-root one is out of scope.
    return [rel for rel in listed if _plain_file_in_root(root / rel, root)]


def _plain_file_in_root(path: Path, root: Path) -> bool:
    """A regular file whose real location is under ``root`` — no link hops."""
    return not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(root)


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


_READ_CHUNK = 1 << 20


def _iter_lines(path: Path) -> Generator[bytes]:
    """Stream the file's lines, each carrying its own terminator.

    This is ``bytes.splitlines(keepends=True)`` semantics — ``\\n``, ``\\r\\n``
    and a bare ``\\r`` all end a line — computed a megabyte at a time, so the
    coordinates the wrapper audits are the ones a reader of the file sees
    without the file ever being resident. Terminators are kept because a span's
    bytes are the file's bytes: rejoining the lines of a span reproduces it
    exactly, CRLF included.
    """
    with path.open("rb") as fh:
        carry = b""
        while chunk := fh.read(_READ_CHUNK):
            lines = (carry + chunk).splitlines(keepends=True)
            # The last element may be an unterminated tail, or a lone trailing
            # ``\r`` whose ``\n`` is in the next read. Both re-split correctly.
            carry = lines.pop() if lines else b""
            yield from lines
        if carry:
            yield carry


def _count_lines_upto(path: Path, limit: int) -> int:
    """Lines in ``path``, counted no further than ``limit``.

    Validating a one-line span from a 30 MB log must cost one line, not 30 MB:
    the count only has to distinguish "at least ``limit`` lines" from the real
    length of a file that is shorter.
    """
    counted = 0
    with contextlib.closing(_iter_lines(path)) as lines:
        for _ in lines:
            counted += 1
            if counted >= limit:
                break
    return counted


def _span_bytes(root: Path, spans: Sequence[Span]) -> dict[int, bytes]:
    """Exact bytes of every span, one streaming pass per path (span index -> bytes).

    Spans sharing a path share the pass, and the pass stops at the deepest end
    line any of them names.
    """
    by_path: dict[str, list[int]] = {}
    for index, span in enumerate(spans):
        by_path.setdefault(span.path, []).append(index)
    out: dict[int, bytes] = {}
    for rel, indexes in by_path.items():
        deepest = max(spans[i].end_line for i in indexes)
        parts: dict[int, list[bytes]] = {i: [] for i in indexes}
        with contextlib.closing(_iter_lines(root / rel)) as lines:
            for number, line in enumerate(lines, start=1):
                if number > deepest:
                    break
                for i in indexes:
                    if spans[i].start_line <= number <= spans[i].end_line:
                        parts[i].append(line)
        out.update({i: b"".join(chunks) for i, chunks in parts.items()})
    return out


def _owned_slice(model: SliceResult, *, allow: list[str], root: Path) -> SliceResult:
    """Audit every coordinate against the real files; the wrapper owns the packet.

    A span outside the allowlist is a widened path set; a span past the end of
    its file is a fabricated coordinate. Either one refuses the whole packet
    rather than silently dropping the span, so a caller never reads a packet
    the model partly invented.
    """
    allowed = set(allow)
    widened = sorted({sp.path for sp in model.spans if sp.path not in allowed})
    if model.seam is not None and model.seam not in allowed:
        widened = sorted({*widened, model.seam})
    widened += [
        p
        for p in sorted({sp.path for sp in model.spans} - set(widened))
        if not _plain_file_in_root(root / p, root)
    ]
    if widened:
        return SliceResult(
            status="refused",
            spans=model.spans,
            unresolved=model.unresolved,
            searched_paths=model.searched_paths,
            reason=f"packet references paths outside the allowlist: {', '.join(widened)}",
        )
    deepest: dict[str, int] = {}
    for sp in model.spans:
        deepest[sp.path] = max(deepest.get(sp.path, 0), sp.end_line)
    lengths = {rel: _count_lines_upto(root / rel, need) for rel, need in deepest.items()}
    invalid = [
        f"{sp.path}:{sp.start_line}-{sp.end_line}"
        for sp in model.spans
        if sp.end_line > lengths[sp.path]
    ]
    if invalid:
        return SliceResult(
            status="refused",
            seam=model.seam,
            spans=model.spans,
            unresolved=model.unresolved,
            searched_paths=model.searched_paths,
            reason=f"spans past end of file: {', '.join(invalid)}",
        )
    if model.status == "complete" and (model.seam is None or not model.spans):
        return SliceResult(
            status="incomplete",
            seam=model.seam,
            spans=model.spans,
            unresolved=model.unresolved,
            searched_paths=model.searched_paths,
            reason="model claimed completion without a seam and at least one span",
        )
    # The seam is the path that owns the behavior, and the prompt defines the
    # ``owner`` role as the seam itself. A packet of consumers and tests around
    # a seam whose code is not in it fails the command's whole contract, so a
    # completion needs one span that is both.
    if model.status == "complete" and not any(
        sp.role == "owner" and sp.path == model.seam for sp in model.spans
    ):
        return SliceResult(
            status="incomplete",
            seam=model.seam,
            spans=model.spans,
            unresolved=model.unresolved,
            searched_paths=model.searched_paths,
            reason=f"no owner span for the declared seam: {model.seam}",
        )
    return model


def _fence(excerpt: str) -> str:
    """A fence longer than the longest backtick run inside ``excerpt``.

    A ``contract`` span is routinely a docstring or a Markdown rule that itself
    contains a triple-backtick block. A fixed fence closes inside such an
    excerpt, and everything after it reads as packet markup rather than file
    content — the structural separation between spans is exactly what the
    packet is for.
    """
    longest = max((len(run) for run in re.findall(r"`+", excerpt)), default=0)
    return "`" * max(3, longest + 1)


def _render_slice(result: SliceResult, root: Path) -> tuple[SliceResult, str | None]:
    """Deterministic packet text: the exact bytes of every audited span.

    Returns the packet with its rendered text, or a refusal with no text when a
    span cannot be reproduced losslessly. The excerpt is the file's own bytes —
    no encoding substitution, no line-ending normalization — because a caller
    reading the packet to make an edit gets the coordinates wrong if the text
    is not what is on disk. A span the default encoding cannot decode refuses
    the packet rather than shipping U+FFFD as though it were the file.
    """
    bodies = _span_bytes(root, result.spans)
    excerpts: list[str] = []
    for index, sp in enumerate(result.spans):
        try:
            excerpts.append(bodies[index].decode())
        except UnicodeDecodeError:
            return (
                SliceResult(
                    status="refused",
                    seam=result.seam,
                    spans=result.spans,
                    unresolved=result.unresolved,
                    searched_paths=result.searched_paths,
                    reason=(
                        "span cannot be rendered losslessly (not valid UTF-8): "
                        f"{sp.path}:{sp.start_line}-{sp.end_line}"
                    ),
                ),
                None,
            )
    lines = [f"# slice — seam: {result.seam}", ""]
    for sp, excerpt in zip(result.spans, excerpts, strict=True):
        # Drop the last line's own terminator so the closing fence starts a
        # line; every other byte of the span, ``\r`` included, is kept.
        body = excerpt[:-1] if excerpt.endswith("\n") else excerpt
        fence = _fence(body)
        lines += [
            f"## {sp.path}:{sp.start_line}-{sp.end_line} [{sp.role}]",
            f"why: {sp.why}",
            fence,
            body,
            fence,
            "",
        ]
    if result.unresolved:
        lines += ["## unresolved", *[f"- {u}" for u in result.unresolved], ""]
    return result, "\n".join(lines)


@spark.command()
def slice(
    task: Annotated[str, typer.Argument(help="The task the caller is about to perform.")],
    paths: Annotated[list[Path], typer.Argument(help="Files or directories in scope.")],
    root: Annotated[
        Path | None, typer.Option("--root", help="Working root the paths live under.")
    ] = None,
    render: Annotated[
        bool,
        typer.Option("--render", help="Print the packet as text with the real span bytes."),
    ] = False,
) -> None:
    """Smallest sufficient context packet: seam + spans + why, never a summary."""
    root = (root or Path.cwd()).resolve()
    files = _resolve_paths(paths, root)
    prompt = slice_prompt(task, files)
    started = time.monotonic()
    record = _wrapper_owned_record(
        verb="slice", prompt=prompt, root=root, writable=False, allow=files, started=started
    )
    status: str | None = None
    raw: str | None = None
    result: SliceResult | None = None
    text: str | None = None
    try:
        try:
            model = run_spark(
                prompt,
                verb="slice",
                workdir=root,
                schema=SliceResult,
                allowed_paths=files,
                repo=root,
                emit_telemetry=False,
                telemetry_record=record,
            )
        except SparkUnavailableError as exc:
            status = "unavailable"
            _die(str(exc))
        except SparkProtocolError as exc:
            status = "protocol_error"
            _die(f"slice failed: {exc}")
        except Exception as exc:
            status = "error"
            _die(f"slice failed: {exc}")
        result = _owned_slice(model, allow=files, root=root)
        # Rendering is part of the audit: a span that cannot be reproduced
        # losslessly refuses the packet, and that is the state the caller and
        # the telemetry both have to carry.
        if render and result.status == "complete":
            result, text = _render_slice(result, root)
        status = result.status
        raw = result.model_dump_json()
    finally:
        _publish_invocation(record, started=started, status=status, raw=raw)
    if text is not None:
        typer.echo(text)
        raise typer.Exit(EXIT_CODES[result.status])
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
    # --verify --quiet: a bare rev-parse echoes an unresolvable rev to stdout
    # and signals failure only in the return code, so an emptiness guard never
    # fires and the typo flows into telemetry as a fake base_sha.
    base_sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not base_sha:
        _usage(f"cannot resolve base rev {base!r} in {root}")

    prompt = transform_prompt(rule, allow, base_sha)
    started = time.monotonic()
    record = _wrapper_owned_record(
        verb="transform",
        prompt=prompt,
        root=root,
        writable=True,
        allow=allow,
        base_sha=base_sha,
        started=started,
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
                    telemetry_record=record,
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
                _publish_invocation(
                    record, started=started, status=status, raw=raw, changed_files=changed
                )
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
