"""The codex-exec substrate: one bounded invocation, no session, no fallback.

Every call shells out to ``codex exec`` with the Spark model pinned. There is
deliberately no retry-with-a-different-model path: a throttled or absent pool
is a plain nonzero exit, never a silent fall-through to the metered Codex pool.
Each invocation starts empty — no resume, no MCP, no web.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from afford_spark.client import DaemonConnectError, ProtocolPinError, request_daemon
from afford_spark.protocol import (
    DaemonRequest,
    resolve_transport,
    socket_path,
    transport_mode,
)

SPARK_MODEL = "gpt-5.3-codex-spark"
SPARK_POOL = "spark"


def _telemetry_path() -> Path:
    """Resolve at write time so HOME (and tests) can redirect the file."""
    override = os.environ.get("AFFORD_SPARK_TELEMETRY")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "afford-spark" / "telemetry.jsonl"


class SparkUnavailableError(RuntimeError):
    """The pool or entitlement refused us. The caller hears it plainly."""


class SparkProtocolError(RuntimeError):
    """codex exec ran but its output did not honor the invocation contract."""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


type JsonDict = dict[str, "JsonValue"]
type JsonValue = JsonDict | list["JsonValue"] | str | int | float | bool | None


def _strictify(schema: JsonDict) -> JsonDict:
    """Make a Pydantic JSON schema acceptable to OpenAI strict structured output.

    Strict mode requires every key in ``properties`` to appear in ``required``
    (optionality is expressed in the type, not by omission). Defaults are
    stripped because a strict schema may not carry them.
    """
    props = schema.get("properties")
    if isinstance(props, dict):
        schema["required"] = list(props.keys())
        schema["additionalProperties"] = False
        for prop in props.values():
            if isinstance(prop, dict):
                prop.pop("default", None)
                _strictify(prop)
    for key in ("$defs", "definitions"):
        defs = schema.get(key)
        if isinstance(defs, dict):
            for sub in defs.values():
                if isinstance(sub, dict):
                    _strictify(sub)
    items = schema.get("items")
    if isinstance(items, dict):
        _strictify(items)
    for variant_key in ("anyOf", "oneOf"):
        variants = schema.get(variant_key)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    _strictify(variant)
    return schema


def _telemetry(record: dict[str, object]) -> None:
    path = _telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def invocation_record(
    *,
    verb: str,
    prompt: str,
    workdir: Path,
    writable: bool,
    caller: str | None = None,
    base_sha: str | None = None,
    allowed_paths: list[str] | None = None,
    repo: Path | None = None,
    transport: str | None = None,
) -> dict[str, object]:
    """SPEC v1 telemetry fields. ``verification`` is always null this slice.

    ``transport`` is resolved by the caller at the typed boundary. This
    builder must not read env that can raise.
    """
    return {
        "at": datetime.now(UTC).isoformat(),
        "caller": caller or os.environ.get("AFFORD_SPARK_CALLER"),
        "operation": verb,
        "base_sha": base_sha,
        "allowed_paths": list(allowed_paths) if allowed_paths is not None else None,
        "input_hash": _hash(prompt),
        "model": SPARK_MODEL,
        "pool": SPARK_POOL,
        "workdir": str(repo or workdir),
        "writable": writable,
        "verification": None,
        "transport": transport,
    }


def emit_invocation(
    record: dict[str, object],
    *,
    started: float,
    status: str | None,
    raw: str | None = None,
    changed_files: list[str] | None = None,
) -> None:
    _telemetry(
        {
            **record,
            "latency_s": round(time.monotonic() - started, 2),
            "output_hash": _hash(raw) if raw else None,
            "changed_files": changed_files,
            "status": status,
        }
    )


def run_spark[M: BaseModel](
    prompt: str,
    *,
    verb: str,
    workdir: Path,
    schema: type[M],
    writable: bool = False,
    timeout_s: int = 300,
    caller: str | None = None,
    base_sha: str | None = None,
    allowed_paths: list[str] | None = None,
    changed_files: list[str] | None = None,
    repo: Path | None = None,
    emit_telemetry: bool = True,
    telemetry_record: dict[str, object] | None = None,
) -> M:
    """One bounded Spark invocation validated against the verb's result model.

    ``workdir`` bounds what codex can see (read-only sandbox) or touch
    (workspace-write, used only by ``transform`` inside an ephemeral worktree).
    The result model is handed to codex as ``--output-schema`` so the harness
    itself constrains the final message; we validate again on our side because
    the wrapper, not the model, owns the contract.

    When ``afford-sparkd`` is reachable (or ``AFFORD_SPARK_TRANSPORT=daemon``)
    the invocation is a thin unix-socket call against the warm app-server.
    Otherwise this is still a oneshot ``codex exec``. ``transport`` is stamped
    on the telemetry record with the path that actually ran.

    Telemetry is written in ``finally`` so pool refusals, protocol errors, and
    timeouts leave a record. ``verification`` is always null in this slice —
    subsequent gate outcome is a later correlator, not something the invocation
    can know.
    """
    started = time.monotonic()
    record: dict[str, object] | None = None
    sandbox = "workspace-write" if writable else "read-only"
    status: str | None = None
    raw: str | None = None
    try:
        transport = choose_transport()
        record = invocation_record(
            verb=verb,
            prompt=prompt,
            workdir=workdir,
            writable=writable,
            caller=caller,
            base_sha=base_sha,
            allowed_paths=allowed_paths,
            repo=repo,
            transport=transport,
        )
        target = telemetry_record if telemetry_record is not None else record
        target["transport"] = transport
        record["transport"] = transport
        schema_obj = _strictify(schema.model_json_schema())
        if transport == "daemon":
            raw = _run_via_daemon(
                prompt,
                verb=verb,
                workdir=workdir,
                writable=writable,
                schema_obj=schema_obj,
                timeout_s=timeout_s,
            )
        else:
            raw = _run_via_exec(
                prompt,
                verb=verb,
                workdir=workdir,
                sandbox=sandbox,
                schema_obj=schema_obj,
                timeout_s=timeout_s,
            )

        try:
            result = schema.model_validate_json(raw)
        except ValidationError as exc:
            status = "protocol_error"
            raise SparkProtocolError(
                f"spark {verb} returned output violating its contract: "
                f"{exc.error_count()} errors; first 400 chars: {raw[:400]}"
            ) from exc

        status = getattr(result, "status", None)
        return result
    except SparkUnavailableError:
        status = status or "unavailable"
        raise
    except SparkProtocolError as exc:
        if record is None:
            record = invocation_record(
                verb=verb,
                prompt=prompt,
                workdir=workdir,
                writable=writable,
                caller=caller,
                base_sha=base_sha,
                allowed_paths=allowed_paths,
                repo=repo,
                transport=None,
            )
        if status is None:
            status = "timeout" if "timed out" in str(exc).lower() else "protocol_error"
        raise
    finally:
        if emit_telemetry and record is not None:
            inflight = sys.exc_info()[0]
            try:
                emit_invocation(
                    record,
                    started=started,
                    status=status,
                    raw=raw,
                    changed_files=changed_files,
                )
            except OSError as exc:
                print(f"afford spark: telemetry write failed: {exc}", file=sys.stderr)
                if inflight is None:
                    raise


def choose_transport() -> str:
    try:
        return resolve_transport()
    except ValueError as exc:
        raise SparkProtocolError(str(exc)) from exc


def _run_via_daemon(
    prompt: str,
    *,
    verb: str,
    workdir: Path,
    writable: bool,
    schema_obj: JsonDict,
    timeout_s: int,
) -> str:
    path = socket_path()
    if transport_mode() == "daemon" and not path.exists():
        raise SparkUnavailableError(f"afford-sparkd socket missing: {path}")
    req = DaemonRequest(
        id=uuid.uuid4().hex,
        verb=verb,
        prompt=prompt,
        workdir=str(workdir),
        writable=writable,
        output_schema=schema_obj,
        timeout_s=timeout_s,
    )
    try:
        response = request_daemon(req, path)
    except DaemonConnectError as exc:
        raise SparkUnavailableError(str(exc)) from exc
    except ProtocolPinError as exc:
        raise SparkProtocolError(str(exc)) from exc
    except TimeoutError as exc:
        raise SparkProtocolError(f"spark {verb} timed out after {timeout_s}s") from exc
    if not response.ok:
        message = response.message or "afford-sparkd refused the call"
        if response.error in {"unavailable", "auth"}:
            raise SparkUnavailableError(message)
        if response.error == "timeout":
            raise SparkProtocolError(f"spark {verb} timed out after {timeout_s}s")
        raise SparkProtocolError(message)
    if not response.raw:
        raise SparkProtocolError("afford-sparkd returned ok with no payload")
    return response.raw


def _run_via_exec(
    prompt: str,
    *,
    verb: str,
    workdir: Path,
    sandbox: str,
    schema_obj: JsonDict,
    timeout_s: int,
) -> str:
    with tempfile.TemporaryDirectory(prefix="afford-spark-") as tmp:
        schema_path = Path(tmp) / "result.schema.json"
        out_path = Path(tmp) / "result.json"
        schema_path.write_text(json.dumps(schema_obj))
        cmd = [
            "codex",
            "exec",
            "--model",
            SPARK_MODEL,
            "-c",
            "model_reasoning_effort=low",
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "-C",
            str(workdir),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(out_path),
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except FileNotFoundError as exc:
            raise SparkUnavailableError(
                "codex CLI not found on PATH — install codex and authenticate first"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SparkProtocolError(f"spark {verb} timed out after {timeout_s}s") from exc

        stderr_tail = proc.stderr[-2000:] if proc.stderr else ""
        if proc.returncode != 0:
            lowered = (proc.stderr + proc.stdout).lower()
            if any(
                marker in lowered
                for marker in (
                    "usage limit",
                    "rate limit",
                    "unauthorized",
                    "429",
                )
            ):
                raise SparkUnavailableError(
                    f"Spark pool or entitlement refused the call (exit {proc.returncode}): "
                    f"{stderr_tail}"
                )
            raise SparkProtocolError(f"codex exec failed (exit {proc.returncode}): {stderr_tail}")

        if not out_path.exists():
            raise SparkProtocolError("codex exec exited 0 but wrote no last message")
        return out_path.read_text().strip()
