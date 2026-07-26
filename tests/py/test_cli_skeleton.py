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


def test_restore_without_restic_env_fails_cleanly():
    """restore is ported (KEDGE-W-003) — invoking it without RESTIC_* env
    now fails the same way every other command does (KedgeError -> exit 1),
    not with a NotImplementedError placeholder."""
    result = CliRunner().invoke(main, ["restore"], env={"RESTIC_REPOSITORY": "", "RESTIC_PASSWORD": ""})
    assert result.exit_code == 1
    assert "RESTIC_REPOSITORY not set" in result.output
