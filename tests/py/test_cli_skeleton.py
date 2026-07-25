"""Phase 1 skeleton smoke tests — CLI wiring, not yet the ported logic."""

from click.testing import CliRunner

from kedge.cli import main


def test_help_lists_all_shell_commands():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("backup", "init", "list", "check", "prune", "discover", "restore", "snapshots"):
        assert cmd in result.output


def test_version_flag():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "kedge" in result.output


def test_unported_commands_fail_loud_not_silent():
    for cmd in ("backup", "init", "list", "check", "prune"):
        result = CliRunner().invoke(main, [cmd])
        assert result.exit_code != 0
        assert isinstance(result.exception, NotImplementedError)
