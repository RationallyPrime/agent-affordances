"""Falsifiers for the wrapper's structural guarantees.

A fake ``codex`` binary on PATH plays the model, so every test pins WRAPPER
behavior — the contracts that hold no matter what Spark returns: schema
validation, pool-refusal classification, path-escape refusal, allowlist
enforcement, empty-diff honesty, and the live checkout staying untouched.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from afford_spark.cli import app
from afford_spark.engine import (
    SparkProtocolError,
    SparkUnavailableError,
    _strictify,
    _telemetry_path,
    run_spark,
)
from afford_spark.models import LocateResult

runner = CliRunner()


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a scriptable fake codex; the test writes its behavior."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # telemetry writes under a throwaway HOME
    (tmp_path / "home").mkdir()

    def install(body: str) -> None:
        script.write_text("#!/bin/bash\n" + body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    return install


def _emit_last_message(payload: dict) -> str:
    """Bash body that writes payload to the --output-last-message path."""
    blob = json.dumps(json.dumps(payload))
    return f"""
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--output-last-message" ]; then out="$arg"; fi
  prev="$arg"
done
cat > /dev/null  # drain stdin
printf '%s' {blob} > "$out"
"""


def test_valid_output_round_trips(fake_codex, tmp_path: Path) -> None:
    fake_codex(
        _emit_last_message(
            {
                "status": "complete",
                "matches": [],
                "searched_paths": 3,
                "uncertainty": [],
                "reason": None,
            }
        )
    )
    result = run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    assert result.status == "complete"
    assert result.searched_paths == 3


def test_contract_violating_output_is_a_protocol_error_not_a_guess(
    fake_codex, tmp_path: Path
) -> None:
    fake_codex(_emit_last_message({"status": "complete", "matches": "not-a-list"}))
    with pytest.raises(SparkProtocolError, match="violating its contract"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)


def test_pool_refusal_is_classified_not_swallowed(fake_codex, tmp_path: Path) -> None:
    fake_codex('cat > /dev/null; echo "429 usage limit reached" >&2; exit 1\n')
    with pytest.raises(SparkUnavailableError, match="refused the call"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)


def _assert_all_required(node: object) -> None:
    assert isinstance(node, dict)
    props = node.get("properties")
    assert isinstance(props, dict)
    required = node.get("required")
    assert isinstance(required, list)
    assert sorted(str(k) for k in required) == sorted(str(k) for k in props)


def test_strictify_requires_every_property() -> None:
    schema = _strictify(LocateResult.model_json_schema())
    _assert_all_required(schema)
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        for sub in defs.values():
            if isinstance(sub, dict) and "properties" in sub:
                _assert_all_required(sub)


def test_locate_refuses_a_path_escaping_the_root(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    result = runner.invoke(app, ["spark", "locate", "q", "../outside.py", "--root", str(tmp_path)])
    assert result.exit_code == 2
    assert "escapes the working root" in result.output


def _git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    (path / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "-f", "a.py", "b.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_transform_refuses_edits_outside_the_allowlist(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    # The fake model edits BOTH files while claiming completion on one.
    fake_codex(
        'workdir=""\nprev=""\nfor arg in "$@"; do\n'
        '  if [ "$prev" = "-C" ]; then workdir="$arg"; fi\n  prev="$arg"\ndone\n'
        'echo "z = 3" >> "$workdir/a.py"\necho "z = 3" >> "$workdir/b.py"\n'
        + _emit_last_message(
            {
                "status": "complete",
                "base_sha": None,
                "touched_paths": ["a.py"],
                "patch": None,
                "claims": ["only a.py changed"],
                "reason": None,
                "decision_required": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "refused"
    assert "b.py" in payload["reason"]
    assert result.exit_code == 5
    # The live checkout is untouched regardless.
    assert (repo / "a.py").read_text() == "x = 1\n"


def test_transform_empty_diff_downgrades_a_completion_claim(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    fake_codex(
        _emit_last_message(
            {
                "status": "complete",
                "base_sha": None,
                "touched_paths": [],
                "patch": None,
                "claims": ["did the thing"],
                "reason": None,
                "decision_required": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "transform", "do nothing", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "incomplete"
    assert "empty" in payload["reason"]
    assert result.exit_code == 3


def test_triage_requires_stdin(fake_codex) -> None:
    result = runner.invoke(app, ["spark", "triage", "--kind", "pytest"], input="")
    assert result.exit_code == 2
    assert "nothing on stdin" in result.output


def _workdir_prelude() -> str:
    return (
        'workdir=""\nprev=""\nfor arg in "$@"; do\n'
        '  if [ "$prev" = "-C" ]; then workdir="$arg"; fi\n  prev="$arg"\ndone\n'
    )


def _complete_payload() -> dict:
    return {
        "status": "complete",
        "base_sha": None,
        "touched_paths": ["a.py"],
        "patch": None,
        "claims": ["only a.py changed"],
        "reason": None,
        "decision_required": None,
    }


def _read_telemetry() -> list[dict]:
    path = _telemetry_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_transform_refuses_a_path_escaping_the_root(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    invoked = tmp_path / "invoked"
    fake_codex(f'printf invoked > "{invoked}"\n' + _emit_last_message(_complete_payload()))
    result = runner.invoke(app, ["spark", "transform", "rule", str(outside), "--root", str(repo)])
    assert result.exit_code == 2
    assert "escapes the working root" in result.output
    assert not invoked.exists()


def test_transform_refuses_untracked_file_outside_the_allowlist(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + 'echo "evil" > "$workdir/evil.py"\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "refused"
    assert "evil.py" in payload["reason"]
    assert result.exit_code == 5
    assert (repo / "a.py").read_text() == "x = 1\n"


def test_transform_staged_edit_is_captured_not_downgraded(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _git_repo(repo)
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + 'git -C "$workdir" add a.py\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "complete"
    assert payload["base_sha"] == sha
    assert "a.py" in payload["touched_paths"]
    assert payload["patch"] and "z = 3" in payload["patch"]
    assert result.exit_code == 0


def test_transform_committed_edit_is_captured_not_downgraded(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _git_repo(repo)
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + 'git -C "$workdir" add a.py\n'
        + 'git -C "$workdir" -c user.email=t@t -c user.name=t commit -qm x\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "complete"
    assert payload["base_sha"] == sha
    assert "a.py" in payload["touched_paths"]
    assert payload["patch"] and "z = 3" in payload["patch"]
    assert result.exit_code == 0


def test_transform_rebuilds_non_complete_result(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _git_repo(repo)
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + _emit_last_message(
            {
                "status": "ambiguous",
                "base_sha": "not-the-real-one",
                "touched_paths": [],
                "patch": "FABRICATED PATCH TEXT",
                "claims": [],
                "reason": None,
                "decision_required": "pick one",
            }
        )
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "ambiguous"
    assert payload["base_sha"] == sha
    assert "a.py" in payload["touched_paths"]
    assert payload["patch"] != "FABRICATED PATCH TEXT"
    assert payload["patch"] and "z = 3" in payload["patch"]
    assert payload["decision_required"] == "pick one"
    assert result.exit_code == 4


def test_telemetry_lands_under_throwaway_home(fake_codex, tmp_path: Path) -> None:
    fake_codex(
        _emit_last_message(
            {
                "status": "complete",
                "matches": [],
                "searched_paths": 1,
                "uncertainty": [],
                "reason": None,
            }
        )
    )
    run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    records = _read_telemetry()
    assert records
    rec = records[-1]
    assert rec["operation"] == "locate"
    assert rec["pool"] == "spark"
    assert rec["status"] == "complete"
    assert rec["verification"] is None
    assert rec["input_hash"]
    assert rec["output_hash"]
    assert str(tmp_path / "home") in str(_telemetry_path())


def test_telemetry_records_a_codex_reported_deadline_as_a_timeout(
    fake_codex, tmp_path: Path
) -> None:
    """The oneshot half of the same rule the daemon half keeps.

    ``subprocess.TimeoutExpired`` is not the only way this transport runs out
    of time: ``codex exec`` can print its own deadline and exit non-zero. One
    predicate rules on both transports so ``status`` stays comparable across
    ``transport`` — the reason the refusal axis was centralised first.
    """
    fake_codex('cat > /dev/null; echo "stream error: request timed out" >&2; exit 1\n')
    with pytest.raises(SparkProtocolError) as caught:
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    assert caught.value.status == "timeout"
    assert _read_telemetry()[-1]["status"] == "timeout"


def test_telemetry_records_an_ordinary_exec_failure_as_protocol(fake_codex, tmp_path: Path) -> None:
    """Control arm: an exit-1 that reports no deadline keeps the default."""
    fake_codex('cat > /dev/null; echo "error: sandbox denied write" >&2; exit 1\n')
    with pytest.raises(SparkProtocolError) as caught:
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    assert caught.value.status == "protocol_error"
    assert _read_telemetry()[-1]["status"] == "protocol_error"


def test_a_deadline_before_the_rendered_tail_is_still_a_deadline(
    fake_codex, tmp_path: Path
) -> None:
    """Classify the whole report, not the 2000-char tail we render.

    A chatty failure pushes its own first line out of the message the caller
    reads; the verdict must not depend on where in stderr the deadline landed.
    """
    fake_codex(
        'cat > /dev/null; echo "stream error: request timed out" >&2; '
        'head -c 4000 /dev/zero | tr "\\0" "x" >&2; exit 1\n'
    )
    with pytest.raises(SparkProtocolError) as caught:
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    assert "timed out" not in str(caught.value), "the tail must not carry the marker"
    assert caught.value.status == "timeout"


def test_telemetry_records_pool_refusal(fake_codex, tmp_path: Path) -> None:
    fake_codex('cat > /dev/null; echo "429 usage limit reached" >&2; exit 1\n')
    with pytest.raises(SparkUnavailableError, match="refused the call"):
        run_spark("q", verb="locate", workdir=tmp_path, schema=LocateResult)
    rec = _read_telemetry()[-1]
    assert rec["status"] == "unavailable"
    assert rec["output_hash"] is None


def test_transform_telemetry_uses_wrapper_audit(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _git_repo(repo)
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    assert result.exit_code == 0
    rec = _read_telemetry()[-1]
    assert rec["operation"] == "transform"
    assert rec["base_sha"] == sha
    assert rec["allowed_paths"] == ["a.py"]
    assert rec["changed_files"] == ["a.py"]
    assert rec["status"] == "complete"
    assert rec["workdir"] == str(repo)


def test_transform_refuses_gitignored_creation_outside_the_allowlist(
    fake_codex, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / ".gitignore").write_text(".env\nbuild/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore"], cwd=repo, check=True)
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + 'echo secret > "$workdir/.env"\n'
        + 'mkdir -p "$workdir/build"\n'
        + 'echo z > "$workdir/build/x.py"\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "refused"
    assert ".env" in payload["reason"]
    assert "build/x.py" in payload["reason"]
    assert result.exit_code == 5
    assert (repo / "a.py").read_text() == "x = 1\n"


def test_transform_refuses_excludesfile_creation_outside_the_allowlist(
    fake_codex, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    excludes = tmp_path / "excludes"
    excludes.write_text("notes.txt\n")
    subprocess.run(["git", "config", "core.excludesFile", str(excludes)], cwd=repo, check=True)
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + 'echo secret > "$workdir/notes.txt"\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "refused"
    assert "notes.txt" in payload["reason"]
    assert result.exit_code == 5


def test_transform_accepts_non_ascii_allowlisted_path(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    named = repo / "é.py"
    named.write_text("x = 1\n")
    subprocess.run(["git", "add", "-f", "é.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/é.py"\n'
        + _emit_last_message(
            {
                "status": "complete",
                "base_sha": None,
                "touched_paths": ["é.py"],
                "patch": None,
                "claims": ["only é.py changed"],
                "reason": None,
                "decision_required": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "é.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "complete"
    assert payload["base_sha"] == sha
    assert "é.py" in payload["touched_paths"]
    assert result.exit_code == 0


def test_transform_telemetry_failure_still_removes_worktree(
    fake_codex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("AFFORD_SPARK_TELEMETRY", str(blocker / "telemetry.jsonl"))
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    assert result.exit_code == 1
    assert "telemetry write failed" in result.output
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "afford-spark-wt" not in listed
    assert (repo / "a.py").read_text() == "x = 1\n"


def _b64(rel: str) -> str:
    return base64.b64encode(rel.encode()).decode()


def _append_rel(rel: str) -> str:
    """Bash snippet: append ``z = 3`` to ``$workdir/<rel>`` (any bytes in the name)."""
    return f'python3 -c {APPEND_REL!r} "$workdir" {_b64(rel)}\n'


APPEND_REL = (
    "from pathlib import Path; import sys, base64; "
    "p = Path(sys.argv[1]) / base64.b64decode(sys.argv[2]).decode(); "
    "p.write_text(p.read_text() + 'z = 3\\n')"
)


def _init_named_repo(repo: Path, name: str) -> str:
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / name).write_text("x = 1\n")
    subprocess.run(["git", "add", "-f", "--", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_transform_accepts_cquoted_allowlisted_path(fake_codex, tmp_path: Path) -> None:
    """quotePath=false still C-quotes `\"` `\\` and controls; -z is what keeps the real name."""
    repo = tmp_path / "repo"
    named = 'say"hi.py'
    sha = _init_named_repo(repo, named)
    fake_codex(
        _workdir_prelude()
        + _append_rel(named)
        + _emit_last_message(
            {
                "status": "complete",
                "base_sha": None,
                "touched_paths": [named],
                "patch": None,
                "claims": [f"only {named} changed"],
                "reason": None,
                "decision_required": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "transform", "append z", named, "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "complete"
    assert payload["base_sha"] == sha
    assert named in payload["touched_paths"]
    assert result.exit_code == 0


def test_transform_accepts_newline_allowlisted_path(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    named = "foo\nbar.py"
    sha = _init_named_repo(repo, named)
    fake_codex(
        _workdir_prelude()
        + _append_rel(named)
        + _emit_last_message(
            {
                "status": "complete",
                "base_sha": None,
                "touched_paths": [named],
                "patch": None,
                "claims": ["only newline-named file changed"],
                "reason": None,
                "decision_required": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "transform", "append z", named, "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "complete"
    assert payload["base_sha"] == sha
    assert named in payload["touched_paths"]
    assert result.exit_code == 0


def test_transform_refuses_cquoted_untracked_by_real_name(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    evil = 'evil"file.py'
    fake_codex(
        _workdir_prelude()
        + 'echo "z = 3" >> "$workdir/a.py"\n'
        + f'python3 -c {CREATE_REL!r} "$workdir" {_b64(evil)}\n'
        + _emit_last_message(_complete_payload())
    )
    result = runner.invoke(app, ["spark", "transform", "append z", "a.py", "--root", str(repo)])
    payload = json.loads(result.output)
    assert payload["status"] == "refused"
    assert evil in payload["reason"]
    assert result.exit_code == 5


CREATE_REL = (
    "from pathlib import Path; import sys, base64; "
    "Path(sys.argv[1], base64.b64decode(sys.argv[2]).decode()).write_text('evil\\n')"
)


def test_transform_usage_exit_survives_telemetry_failure(
    fake_codex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("AFFORD_SPARK_TELEMETRY", str(blocker / "telemetry.jsonl"))
    invoked = tmp_path / "invoked"
    fake_codex(f'printf invoked > "{invoked}"\n' + _emit_last_message(_complete_payload()))
    result = runner.invoke(app, ["spark", "transform", "rule", "missing.py", "--root", str(repo)])
    assert result.exit_code == 2
    assert "allowlisted paths absent" in result.output
    assert "telemetry write failed" in result.output
    assert not invoked.exists()
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "afford-spark-wt" not in listed


def test_locate_pool_refusal_survives_telemetry_failure(
    fake_codex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("AFFORD_SPARK_TELEMETRY", str(blocker / "telemetry.jsonl"))
    fake_codex('cat > /dev/null; echo "429 usage limit reached" >&2; exit 1\n')
    result = runner.invoke(app, ["spark", "locate", "q", "a.py", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "refused the call" in result.output
    assert "locate failed:" not in result.output


def test_locate_directory_expands_tracked_files_not_venv(fake_codex, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / ".gitignore").write_text(".venv/\n")
    venv_file = repo / ".venv" / "lib" / "site-packages" / "noise.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("noise\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore"], cwd=repo, check=True)
    captured = tmp_path / "prompt.txt"
    fake_codex(
        f'cat > "{captured}"\n'
        + _emit_last_message(
            {
                "status": "complete",
                "matches": [],
                "searched_paths": 1,
                "uncertainty": [],
                "reason": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "locate", "q", ".", "--root", str(repo)])
    assert result.exit_code == 0
    prompt = captured.read_text()
    assert "a.py" in prompt
    assert ".venv" not in prompt
    rec = _read_telemetry()[-1]
    assert "a.py" in rec["allowed_paths"]
    assert not any(".venv" in p for p in rec["allowed_paths"])


def test_locate_directory_outside_repo_still_rglobs(fake_codex, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2\n")
    captured = tmp_path / "prompt.txt"
    fake_codex(
        f'cat > "{captured}"\n'
        + _emit_last_message(
            {
                "status": "complete",
                "matches": [],
                "searched_paths": 1,
                "uncertainty": [],
                "reason": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "locate", "q", "sub", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "sub/b.py" in captured.read_text()


def test_transform_unresolvable_base_is_a_usage_error_not_an_engine_failure(
    fake_codex, tmp_path: Path
) -> None:
    """A bare ``rev-parse`` echoes the typo to stdout and fails only in its
    return code, so the emptiness guard never fired: ``--base mian`` exited 1
    (engine) and landed the literal string ``mian`` in telemetry's base_sha."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    invoked = tmp_path / "invoked"
    fake_codex(f'printf invoked > "{invoked}"\n' + _emit_last_message(_complete_payload()))
    result = runner.invoke(
        app, ["spark", "transform", "rule", "a.py", "--root", str(repo), "--base", "mian"]
    )
    assert result.exit_code == 2
    assert "cannot resolve base rev 'mian'" in result.output
    assert not invoked.exists()


def test_transform_empty_repo_head_is_a_usage_error(fake_codex, tmp_path: Path) -> None:
    """An empty repository resolves ``HEAD`` to nothing; the guard must refuse
    with usage semantics instead of handing ``HEAD`` to ``worktree add``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    invoked = tmp_path / "invoked"
    fake_codex(f'printf invoked > "{invoked}"\n' + _emit_last_message(_complete_payload()))
    result = runner.invoke(app, ["spark", "transform", "rule", "a.py", "--root", str(repo)])
    assert result.exit_code == 2
    assert "cannot resolve base rev" in result.output
    assert not invoked.exists()


def test_locate_directory_includes_untracked_files_but_not_ignored_ones(
    fake_codex, tmp_path: Path
) -> None:
    """Tracked-only expansion made a just-written file invisible, and an empty
    ``complete`` read as evidence of absence. ``--others --exclude-standard``
    keeps the untracked file while still excluding the ignored noise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / ".gitignore").write_text(".venv/\n")
    venv_file = repo / ".venv" / "noise.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("noise\n")
    (repo / "brand_new.py").write_text("answer = 42\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore"], cwd=repo, check=True)
    captured = tmp_path / "prompt.txt"
    fake_codex(
        f'cat > "{captured}"\n'
        + _emit_last_message(
            {
                "status": "complete",
                "matches": [],
                "searched_paths": 1,
                "uncertainty": [],
                "reason": None,
            }
        )
    )
    result = runner.invoke(app, ["spark", "locate", "q", ".", "--root", str(repo)])
    assert result.exit_code == 0
    prompt = captured.read_text()
    assert "brand_new.py" in prompt
    assert ".venv" not in prompt
