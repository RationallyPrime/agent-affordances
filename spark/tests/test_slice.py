"""Falsifiers for ``afford spark slice`` — the wrapper owns the packet.

Spark names coordinates; the wrapper checks every one against the real files.
A widened path set or a span past end-of-file refuses the whole packet, and
``--render`` reads the real bytes, never the model's paraphrase.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from afford_spark.cli import app
from afford_spark.engine import _telemetry_path
from afford_spark.models import SliceResult, Span

runner = CliRunner()


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Scriptable fake codex on PATH; telemetry lands under a throwaway HOME."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    def install(body: str) -> None:
        script.write_text("#!/bin/bash\n" + body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    return install


def _emit_last_message(payload: dict) -> str:
    blob = json.dumps(json.dumps(payload))
    return f"""
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--output-last-message" ]; then out="$arg"; fi
  prev="$arg"
done
cat > /dev/null
printf '%s' {blob} > "$out"
"""


def _read_telemetry() -> list[dict]:
    path = _telemetry_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.py").write_text("def owner():\n    return state()\n\n\ndef state():\n    return 1\n")
    (repo / "b.py").write_text("from a import owner\n\nprint(owner())\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def _packet(**overrides: object) -> dict:
    base: dict = {
        "status": "complete",
        "seam": "a.py",
        "spans": [
            {"path": "a.py", "start_line": 1, "end_line": 2, "role": "owner", "why": "owns it"},
            {"path": "b.py", "start_line": 3, "end_line": 3, "role": "consumer", "why": "calls it"},
        ],
        "unresolved": [],
        "searched_paths": 2,
        "reason": None,
    }
    return {**base, **overrides}


def _run(repo: Path, *extra: str):
    return runner.invoke(app, ["spark", "slice", "rename owner", ".", "--root", str(repo), *extra])


def test_valid_packet_round_trips(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(_emit_last_message(_packet()))
    result = _run(repo)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["seam"] == "a.py"
    assert [s["role"] for s in payload["spans"]] == ["owner", "consumer"]
    rec = _read_telemetry()[-1]
    assert rec["operation"] == "slice"
    assert sorted(rec["allowed_paths"]) == ["a.py", "b.py"]
    assert rec["status"] == "complete"


def test_span_outside_the_allowlist_refuses_the_packet(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "c.py").write_text("x = 1\n")
    fake_codex(
        _emit_last_message(
            _packet(
                spans=[
                    *_packet()["spans"],
                    {"path": "c.py", "start_line": 1, "end_line": 1, "role": "test", "why": "w"},
                ]
            )
        )
    )
    result = runner.invoke(app, ["spark", "slice", "t", "a.py", "b.py", "--root", str(repo)])
    assert result.exit_code == 5
    payload = json.loads(result.output)
    assert payload["status"] == "refused"
    assert "c.py" in payload["reason"]
    assert len(payload["spans"]) == 3  # nothing silently dropped


def test_seam_outside_the_allowlist_refuses_the_packet(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(_emit_last_message(_packet(seam="../elsewhere.py")))
    result = _run(repo)
    assert result.exit_code == 5
    assert "elsewhere.py" in json.loads(result.output)["reason"]


def test_span_past_end_of_file_refuses_the_packet(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(
        _emit_last_message(
            _packet(
                spans=[
                    {"path": "a.py", "start_line": 5, "end_line": 40, "role": "owner", "why": "w"}
                ]
            )
        )
    )
    result = _run(repo)
    assert result.exit_code == 5
    assert "a.py:5-40" in json.loads(result.output)["reason"]


def test_completion_without_spans_is_incomplete(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(_emit_last_message(_packet(spans=[])))
    result = _run(repo)
    assert result.exit_code == 3
    assert json.loads(result.output)["status"] == "incomplete"


def test_ambiguous_passes_through_with_reason(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(
        _emit_last_message(
            _packet(status="ambiguous", seam=None, spans=[], reason="two seams could own this")
        )
    )
    result = _run(repo)
    assert result.exit_code == 4
    assert json.loads(result.output)["reason"] == "two seams could own this"


def test_render_prints_real_bytes_not_the_model_text(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(_emit_last_message(_packet()))
    result = _run(repo, "--render")
    assert result.exit_code == 0, result.output
    assert "# slice — seam: a.py" in result.output
    assert "## a.py:1-2 [owner]" in result.output
    assert "def owner():\n    return state()" in result.output
    assert "print(owner())" in result.output
    assert "def state" not in result.output  # outside every span


def test_render_falls_back_to_json_when_not_complete(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(_emit_last_message(_packet(spans=[])))
    result = _run(repo, "--render")
    assert result.exit_code == 3
    assert json.loads(result.output)["status"] == "incomplete"


def test_prompt_lists_paths_and_task(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    captured = tmp_path / "prompt.txt"
    fake_codex(f'cat > "{captured}"\n' + _emit_last_message(_packet()))
    assert _run(repo).exit_code == 0
    prompt = captured.read_text()
    assert "Operation: SLICE" in prompt
    assert "rename owner" in prompt
    assert "- a.py" in prompt and "- b.py" in prompt


def test_span_end_before_start_is_a_contract_breach() -> None:
    with pytest.raises(ValueError, match="end_line precedes start_line"):
        Span(path="a.py", start_line=5, end_line=2, role="owner", why="w")


def test_extra_field_is_a_contract_breach() -> None:
    with pytest.raises(ValueError):
        SliceResult.model_validate({"status": "complete", "summary": "the repo does X"})
