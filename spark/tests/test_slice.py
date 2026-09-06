"""Falsifiers for ``afford spark slice`` — the wrapper owns the packet.

Spark names coordinates; the wrapper checks every one against the real files.
A widened path set or a span past end-of-file refuses the whole packet, and
``--render`` reads the real bytes, never the model's paraphrase.
"""

from __future__ import annotations

import itertools
import json
import os
import stat
import subprocess
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from afford_spark import cli, protocol
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


def test_directory_expansion_drops_symlinks(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = tmp_path / "outside.txt"
    secret.write_text("TOP SECRET\n")
    (repo / "leak.txt").symlink_to(secret)
    (repo / "alias.py").symlink_to(repo / "a.py")
    subprocess.run(["git", "add", "leak.txt", "alias.py"], cwd=repo, check=True)
    captured = tmp_path / "prompt.txt"
    fake_codex(f'cat > "{captured}"\n' + _emit_last_message(_packet()))
    result = _run(repo)
    assert result.exit_code == 0, result.output
    prompt = captured.read_text()
    assert "leak.txt" not in prompt
    assert "alias.py" not in prompt
    assert sorted(_read_telemetry()[-1]["allowed_paths"]) == ["a.py", "b.py"]


def test_render_refuses_a_span_that_became_a_symlink(fake_codex, tmp_path: Path) -> None:
    """Even an allowlisted path is re-checked at render time: no link hops."""
    repo = _repo(tmp_path)
    secret = tmp_path / "outside.txt"
    secret.write_text("TOP SECRET\n")
    fake_codex(
        _emit_last_message(_packet())
        + f'rm "{repo / "b.py"}" && ln -s "{secret}" "{repo / "b.py"}"\n'
    )
    result = _run(repo, "--render")
    assert result.exit_code == 5
    assert "TOP SECRET" not in result.output
    assert "b.py" in json.loads(result.output)["reason"]


def _submodule_repo(tmp_path: Path) -> Path:
    """A superproject with a tracked submodule at ``sub``."""
    inner = tmp_path / "inner"
    inner.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=inner, check=True)
    (inner / "lib.py").write_text("def lib():\n    return 2\n")
    subprocess.run(["git", "add", "."], cwd=inner, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=inner,
        check=True,
    )
    repo = _repo(tmp_path)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "sub"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sub"],
        cwd=repo,
        check=True,
    )
    return repo


def test_submodule_only_path_set_is_a_usage_error(fake_codex, tmp_path: Path) -> None:
    """`git ls-files` answers a submodule with the gitlink, which is a directory."""
    repo = _submodule_repo(tmp_path)
    fake_codex(_emit_last_message(_packet()))
    result = runner.invoke(app, ["spark", "slice", "t", "sub", "--root", str(repo)])
    assert result.exit_code == 2
    assert "path set is empty" in result.output


def test_submodule_gitlink_never_reaches_the_prompt(fake_codex, tmp_path: Path) -> None:
    repo = _submodule_repo(tmp_path)
    captured = tmp_path / "prompt.txt"
    fake_codex(f'cat > "{captured}"\n' + _emit_last_message(_packet()))
    assert _run(repo).exit_code == 0
    assert "- sub\n" not in captured.read_text()


def test_span_naming_a_submodule_gitlink_is_refused(fake_codex, tmp_path: Path) -> None:
    """Never `read_bytes()` on a directory: the gitlink is outside the allowlist."""
    repo = _submodule_repo(tmp_path)
    fake_codex(
        _emit_last_message(
            _packet(
                seam="sub",
                spans=[
                    {"path": "sub", "start_line": 1, "end_line": 1, "role": "owner", "why": "w"}
                ],
            )
        )
    )
    result = _run(repo)
    assert result.exit_code == 5
    assert "sub" in json.loads(result.output)["reason"]


def test_completion_without_an_owner_span_for_the_seam_is_incomplete(
    fake_codex, tmp_path: Path
) -> None:
    """Consumers and tests around a seam whose own code is missing is not a packet."""
    repo = _repo(tmp_path)
    fake_codex(
        _emit_last_message(
            _packet(
                spans=[
                    {"path": "b.py", "start_line": 3, "end_line": 3, "role": "consumer", "why": "w"}
                ]
            )
        )
    )
    result = _run(repo)
    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["status"] == "incomplete"
    assert payload["reason"] == "no owner span for the declared seam: a.py"


def test_owner_span_belonging_to_another_path_is_incomplete(fake_codex, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_codex(
        _emit_last_message(
            _packet(
                spans=[
                    {"path": "b.py", "start_line": 1, "end_line": 1, "role": "owner", "why": "w"}
                ]
            )
        )
    )
    result = _run(repo)
    assert result.exit_code == 3
    assert json.loads(result.output)["reason"] == "no owner span for the declared seam: a.py"


def test_telemetry_records_the_wrapper_audited_status(fake_codex, tmp_path: Path) -> None:
    """A packet the audit downgrades must not be logged with the model's claim."""
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
    rec = _read_telemetry()[-1]
    assert rec["status"] == "refused"
    assert rec["transport"] == "oneshot"


def test_render_refuses_a_span_it_cannot_reproduce(fake_codex, tmp_path: Path) -> None:
    """Undecodable bytes refuse the packet — U+FFFD is not the file."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"# caf\xe9 latin-1\ndef owner():\n    return 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    fake_codex(_emit_last_message(_packet()))
    result = _run(repo, "--render")
    assert result.exit_code == 5
    assert "�" not in result.output
    assert "a.py:1-2" in json.loads(result.output)["reason"]
    assert _read_telemetry()[-1]["status"] == "refused"


def test_render_preserves_crlf_line_endings(fake_codex, tmp_path: Path) -> None:
    """`.output` is normalized by the test harness; the wire bytes are not."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"def owner():\r\n    return 1\r\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    fake_codex(_emit_last_message(_packet()))
    result = _run(repo, "--render")
    assert result.exit_code == 0, result.output
    assert b"def owner():\r\n    return 1" in result.stdout_bytes


def test_render_fence_outlives_a_backtick_run_in_the_excerpt(fake_codex, tmp_path: Path) -> None:
    """A `contract` span is routinely a docstring holding its own fenced block."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_text('"""\n```\nfenced\n```\n"""\n')
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    fake_codex(
        _emit_last_message(
            _packet(
                spans=[
                    {"path": "a.py", "start_line": 1, "end_line": 5, "role": "owner", "why": "w"}
                ]
            )
        )
    )
    result = _run(repo, "--render")
    assert result.exit_code == 0, result.output
    tail = result.output[result.output.index("## a.py:1-5 [owner]") :].splitlines()
    fence = tail[2]
    assert set(fence) == {"`"} and len(fence) == 4, tail
    closes = [i for i, line in enumerate(tail[3:], start=3) if line.rstrip() == fence]
    assert closes[0] == 8, f"fence closed inside the excerpt at {closes}: {tail}"


def test_a_one_line_span_does_not_load_the_whole_file(fake_codex, tmp_path: Path) -> None:
    """Validation and render both stream: a tiny packet from a big log stays tiny."""
    repo = _repo(tmp_path)
    big = repo / "big.log"
    with big.open("w") as fh:
        for i in range(200_000):
            fh.write(f"line {i} " + "x" * 60 + "\n")
    subprocess.run(["git", "add", "big.log"], cwd=repo, check=True)
    fake_codex(
        _emit_last_message(
            _packet(
                spans=[
                    {"path": "a.py", "start_line": 1, "end_line": 2, "role": "owner", "why": "w"},
                    {
                        "path": "big.log",
                        "start_line": 5,
                        "end_line": 5,
                        "role": "producer",
                        "why": "w",
                    },
                ]
            )
        )
    )
    tracemalloc.start()
    result = _run(repo, "--render")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result.exit_code == 0, result.output
    assert "line 4 " in result.output
    assert peak < big.stat().st_size // 4, f"peak {peak} against a {big.stat().st_size}B file"


def test_a_span_past_eof_in_a_big_file_is_still_refused(fake_codex, tmp_path: Path) -> None:
    """The early-stopping count must not turn a short file into a long one."""
    repo = _repo(tmp_path)
    fake_codex(
        _emit_last_message(
            _packet(
                spans=[
                    {"path": "a.py", "start_line": 1, "end_line": 2, "role": "owner", "why": "w"},
                    {
                        "path": "b.py",
                        "start_line": 3,
                        "end_line": 99,
                        "role": "consumer",
                        "why": "w",
                    },
                ]
            )
        )
    )
    result = _run(repo)
    assert result.exit_code == 5
    assert "b.py:3-99" in json.loads(result.output)["reason"]


def test_render_preserves_a_trailing_bare_cr(fake_codex, tmp_path: Path) -> None:
    """A span whose last line ends in a bare ``\\r`` must not render as ``\\r\\n``.

    The closing fence starts a new line off the span's own terminator; supplying
    an LF for it instead rewrites the one line ending this render still claims.
    """
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"def owner():\r    return 1\r")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    fake_codex(_emit_last_message(_packet()))
    result = _run(repo, "--render")
    assert result.exit_code == 0, result.output
    assert b"def owner():\r    return 1\r```" in result.stdout_bytes
    assert b"return 1\r\n" not in result.stdout_bytes


def test_render_supplies_a_newline_for_an_unterminated_last_line(
    fake_codex, tmp_path: Path
) -> None:
    """The control for the bare-CR case: the fence still starts its own line."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"def owner():\n    return 1")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    fake_codex(_emit_last_message(_packet()))
    result = _run(repo, "--render")
    assert result.exit_code == 0, result.output
    assert b"    return 1\n```" in result.stdout_bytes


def test_the_line_reader_is_bytes_splitlines_at_every_chunk_boundary(tmp_path: Path) -> None:
    """The one line grammar, checked against the stdlib it claims to reproduce.

    Every string over ``{a, \\n, \\r}`` up to length 5, at read sizes that put a
    boundary inside a CRLF. This is what licenses scanning only the new chunk.
    """
    f = tmp_path / "f"
    original = cli._READ_CHUNK
    try:
        for chunk in (1, 2, 3, 5, 1 << 20):
            cli._READ_CHUNK = chunk
            for n in range(6):
                for combo in itertools.product([b"a", b"\n", b"\r"], repeat=n):
                    data = b"".join(combo)
                    f.write_bytes(data)
                    want = data.splitlines(keepends=True)
                    assert list(cli._iter_lines(f)) == want, (chunk, data)
                    for limit in range(1, 7):
                        assert cli._count_lines_upto(f, limit) == min(len(want), limit), (
                            chunk,
                            data,
                            limit,
                        )
    finally:
        cli._READ_CHUNK = original


def test_validating_a_span_in_a_single_line_file_does_not_hold_the_line(tmp_path: Path) -> None:
    """A minified bundle is one line the size of the file.

    Counting must neither re-split an accumulating prefix nor assemble a line no
    caller will read; both are what a growing ``carry`` costs.
    """
    big = tmp_path / "bundle.js"
    big.write_bytes(b"x" * (16 << 20))
    tracemalloc.start()
    counted = cli._count_lines_upto(big, 2)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert counted == 1
    assert peak < big.stat().st_size // 4, f"peak {peak} against a {big.stat().st_size}B file"


def test_a_timeout_is_recorded_as_a_timeout(
    fake_codex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrapper-owned telemetry classifies a timeout the way the engine does."""
    repo = _repo(tmp_path)
    fake_codex("cat > /dev/null; sleep 30\n")
    real = cli.run_spark

    def _short_timeout(*args: Any, **kwargs: Any) -> Any:
        kwargs["timeout_s"] = 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "run_spark", _short_timeout)
    result = _run(repo)
    assert result.exit_code == 1
    assert _read_telemetry()[-1]["status"] == "timeout"


def test_a_malformed_response_still_carries_an_output_hash(fake_codex, tmp_path: Path) -> None:
    """The only content-free correlator for a protocol failure is its hash."""
    repo = _repo(tmp_path)
    fake_codex(_emit_last_message({"status": "complete", "spans": "not a list"}))
    result = _run(repo)
    assert result.exit_code == 1
    rec = _read_telemetry()[-1]
    assert rec["status"] == "protocol_error"
    assert rec["output_hash"] is not None


def test_the_transport_is_probed_once_per_invocation(
    fake_codex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``auto`` opens a connection to the daemon socket to decide.

    The wrapper record takes the engine's single selection; probing again just
    to stamp it puts an extra empty frame on the daemon before every request.
    """
    repo = _repo(tmp_path)
    monkeypatch.setenv("AFFORD_SPARK_TRANSPORT", "auto")
    monkeypatch.setenv("AFFORD_SPARK_SOCKET", str(tmp_path / "no-such.sock"))
    probes: list[Path] = []
    monkeypatch.setattr(
        protocol, "socket_is_connectable", lambda path: bool(probes.append(path)) and False
    )
    fake_codex(_emit_last_message(_packet()))
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert len(probes) == 1, probes
    assert _read_telemetry()[-1]["transport"] == "oneshot"
