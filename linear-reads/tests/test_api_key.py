from __future__ import annotations

from pathlib import Path

import pytest

from linear_reads.cli import app
from linear_reads.client import KEY_FILE_RELATIVE, MissingAPIKeyError, resolve_api_key


def write_key(path: Path, key: str = "lin_api_file", mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_env_var_wins_over_every_file(tmp_path: Path) -> None:
    write_key(tmp_path / ".claude" / KEY_FILE_RELATIVE)
    assert resolve_api_key({"LINEAR_API_KEY": "lin_api_env"}, home=tmp_path) == "lin_api_env"


def test_claude_profile_file_is_read(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "gnomon"
    write_key(profile / KEY_FILE_RELATIVE, "lin_api_profile")
    env = {"CLAUDE_CONFIG_DIR": str(profile)}
    assert resolve_api_key(env, home=tmp_path) == "lin_api_profile"


def test_codex_profile_file_is_read_when_claude_is_unpinned(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "ariadne"
    write_key(profile / KEY_FILE_RELATIVE, "lin_api_codex")
    assert resolve_api_key({"CODEX_HOME": str(profile)}, home=tmp_path) == "lin_api_codex"


def test_home_dot_claude_is_the_unpinned_fallback(tmp_path: Path) -> None:
    write_key(tmp_path / ".claude" / KEY_FILE_RELATIVE, "lin_api_home")
    assert resolve_api_key({}, home=tmp_path) == "lin_api_home"


def test_explicit_key_file_wins_over_the_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "gnomon"
    write_key(profile / KEY_FILE_RELATIVE, "lin_api_profile")
    explicit = write_key(tmp_path / "elsewhere" / "key", "lin_api_explicit")
    env = {"CLAUDE_CONFIG_DIR": str(profile), "LINEAR_API_KEY_FILE": str(explicit)}
    assert resolve_api_key(env, home=tmp_path) == "lin_api_explicit"


def test_a_missing_explicit_key_file_is_an_error_not_a_fallthrough(tmp_path: Path) -> None:
    write_key(tmp_path / ".claude" / KEY_FILE_RELATIVE, "lin_api_home")
    missing = tmp_path / "nope"
    with pytest.raises(MissingAPIKeyError, match=r"LINEAR_API_KEY_FILE=.*nope does not exist"):
        resolve_api_key({"LINEAR_API_KEY_FILE": str(missing)}, home=tmp_path)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660])
def test_a_group_or_world_readable_key_file_is_refused(tmp_path: Path, mode: int) -> None:
    write_key(tmp_path / ".claude" / KEY_FILE_RELATIVE, mode=mode)
    with pytest.raises(MissingAPIKeyError, match="chmod 600"):
        resolve_api_key({}, home=tmp_path)


def test_an_empty_key_file_is_an_error(tmp_path: Path) -> None:
    write_key(tmp_path / ".claude" / KEY_FILE_RELATIVE, "   ")
    with pytest.raises(MissingAPIKeyError, match="is empty"):
        resolve_api_key({}, home=tmp_path)


def test_nothing_found_names_every_place_it_looked(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "gnomon"
    with pytest.raises(MissingAPIKeyError) as excinfo:
        resolve_api_key({"CLAUDE_CONFIG_DIR": str(profile)}, home=tmp_path)
    message = str(excinfo.value)
    assert "LINEAR_API_KEY is not set" in message
    assert str(profile / KEY_FILE_RELATIVE) in message
    assert str(tmp_path / ".claude" / KEY_FILE_RELATIVE) in message


def test_the_cli_reads_the_profile_key_file(runner, monkeypatch, tmp_path: Path) -> None:
    """The CLI needs no env var when the profile carries the key file."""
    profile = tmp_path / "profiles" / "gnomon"
    write_key(profile / KEY_FILE_RELATIVE, "lin_api_profile")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    seen: dict[str, str] = {}

    class Recorder:
        def __init__(self, api_key: str | None = None, transport=None) -> None:
            seen["key"] = api_key or ""
            raise RuntimeError("stop here")

    from linear_reads import cli as cli_module
    from linear_reads import client as client_module

    monkeypatch.setattr(
        cli_module, "LinearClient", lambda: Recorder(client_module.resolve_api_key())
    )
    with pytest.raises(RuntimeError, match="stop here"):
        cli_module._make_client()
    assert seen["key"] == "lin_api_profile"


def test_the_cli_error_names_the_key_file_convention(runner) -> None:
    result = runner.invoke(app, ["teams"])
    assert result.exit_code == 2
    assert "LINEAR_API_KEY is not set" in result.stderr
    assert "secrets/linear_api_key" in result.stderr
