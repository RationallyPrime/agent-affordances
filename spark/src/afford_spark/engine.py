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
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

SPARK_MODEL = "gpt-5.3-codex-spark"
TELEMETRY_PATH = Path.home() / ".local" / "state" / "afford-spark" / "telemetry.jsonl"


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
    TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TELEMETRY_PATH.open("a") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def run_spark[M: BaseModel](
    prompt: str,
    *,
    verb: str,
    workdir: Path,
    schema: type[M],
    writable: bool = False,
    timeout_s: int = 300,
) -> M:
    """One bounded Spark invocation validated against the verb's result model.

    ``workdir`` bounds what codex can see (read-only sandbox) or touch
    (workspace-write, used only by ``transform`` inside an ephemeral worktree).
    The result model is handed to codex as ``--output-schema`` so the harness
    itself constrains the final message; we validate again on our side because
    the wrapper, not the model, owns the contract.
    """
    started = time.monotonic()
    sandbox = "workspace-write" if writable else "read-only"
    with tempfile.TemporaryDirectory(prefix="afford-spark-") as tmp:
        schema_path = Path(tmp) / "result.schema.json"
        out_path = Path(tmp) / "result.json"
        schema_path.write_text(json.dumps(_strictify(schema.model_json_schema())))
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
                for marker in ("usage limit", "rate limit", "unauthorized", "429", "401", "403")
            ):
                raise SparkUnavailableError(
                    f"Spark pool or entitlement refused the call (exit {proc.returncode}): "
                    f"{stderr_tail}"
                )
            raise SparkProtocolError(f"codex exec failed (exit {proc.returncode}): {stderr_tail}")

        if not out_path.exists():
            raise SparkProtocolError("codex exec exited 0 but wrote no last message")
        raw = out_path.read_text().strip()

    try:
        result = schema.model_validate_json(raw)
    except ValidationError as exc:
        raise SparkProtocolError(
            f"spark {verb} returned output violating its contract: {exc.error_count()} errors; "
            f"first 400 chars: {raw[:400]}"
        ) from exc

    _telemetry(
        {
            "at": datetime.now(UTC).isoformat(),
            "verb": verb,
            "workdir": str(workdir),
            "writable": writable,
            "model": SPARK_MODEL,
            "prompt_hash": _hash(prompt),
            "output_hash": _hash(raw),
            "status": getattr(result, "status", None),
            "latency_s": round(time.monotonic() - started, 2),
        }
    )
    return result
