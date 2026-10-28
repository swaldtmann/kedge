"""Version/status file written per backup/prune run (/var/lib/kedge/<stack>.version)."""

import stat

from click.testing import CliRunner

from kedge import __version__
from kedge.cli import main
from kedge.errors import KedgeError


def _read(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def _env(tmp_path, stack="stackA", state=None):
    stack_dir = tmp_path / stack
    stack_dir.mkdir(exist_ok=True)
    state_dir = state if state is not None else tmp_path / "state"
    return {"STACK_DIR": str(stack_dir), "KEDGE_STATE_DIR": str(state_dir)}


def test_success_writes_status_file(tmp_path, monkeypatch):
    monkeypatch.setattr("kedge.cli.cmd_prune", lambda cfg: None)
    result = CliRunner().invoke(main, ["prune"], env=_env(tmp_path))
    assert result.exit_code == 0
    f = tmp_path / "state" / "stackA.version"
    data = _read(f)
    assert list(data) == ["kedge", "stack", "run", "started", "finished", "result"]
    assert data["kedge"] == __version__
    assert data["stack"] == "stackA"
    assert data["run"] == "prune"
    assert data["result"] == "ok"
    for k in ("started", "finished"):
        assert len(data[k]) == 20 and data[k].endswith("Z") and data[k][10] == "T"
    assert stat.S_IMODE(f.stat().st_mode) == 0o644
    assert [p.name for p in f.parent.iterdir()] == ["stackA.version"]  # no tmp left


def test_backup_run_name(tmp_path, monkeypatch):
    monkeypatch.setattr("kedge.cli.cmd_backup", lambda cfg: None)
    CliRunner().invoke(main, ["backup"], env=_env(tmp_path))
    assert _read(tmp_path / "state" / "stackA.version")["run"] == "backup"


def test_failure_writes_failed_and_keeps_exit_code(tmp_path, monkeypatch):
    def boom(cfg):
        raise KedgeError("kaputt")

    monkeypatch.setattr("kedge.cli.cmd_backup", boom)
    result = CliRunner().invoke(main, ["backup"], env=_env(tmp_path))
    assert result.exit_code == 1
    data = _read(tmp_path / "state" / "stackA.version")
    assert data["result"] == "failed"
    assert data["exit"] == "1"
    assert list(data)[-2:] == ["result", "exit"]


def test_failure_keeps_systemexit_code(tmp_path, monkeypatch):
    def exit3(cfg):
        raise SystemExit(3)

    monkeypatch.setattr("kedge.cli.cmd_prune", exit3)
    result = CliRunner().invoke(main, ["prune"], env=_env(tmp_path))
    assert result.exit_code == 3
    assert _read(tmp_path / "state" / "stackA.version")["exit"] == "3"


def test_unexpected_exception_is_failed_exit_1(tmp_path, monkeypatch):
    def boom(cfg):
        raise RuntimeError("x")

    monkeypatch.setattr("kedge.cli.cmd_prune", boom)
    result = CliRunner().invoke(main, ["prune"], env=_env(tmp_path))
    assert isinstance(result.exception, RuntimeError)
    data = _read(tmp_path / "state" / "stackA.version")
    assert data["result"] == "failed" and data["exit"] == "1"


def test_unwritable_dir_warns_and_run_continues(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("kedge.cli.cmd_prune", lambda cfg: called.append(1))
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not dir")
    result = CliRunner().invoke(main, ["prune"], env=_env(tmp_path, state=blocker / "sub"))
    assert result.exit_code == 0
    assert called == [1]
    assert "version file" in result.output.lower()


def test_unwritable_dir_failed_run_keeps_exit(tmp_path, monkeypatch):
    def boom(cfg):
        raise KedgeError("kaputt")

    monkeypatch.setattr("kedge.cli.cmd_prune", boom)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    result = CliRunner().invoke(main, ["prune"], env=_env(tmp_path, state=blocker / "sub"))
    assert result.exit_code == 1


def test_two_stacks_two_files(tmp_path, monkeypatch):
    monkeypatch.setattr("kedge.cli.cmd_prune", lambda cfg: None)
    CliRunner().invoke(main, ["prune"], env=_env(tmp_path, "stackA"))
    CliRunner().invoke(main, ["prune"], env=_env(tmp_path, "calcom"))
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == ["calcom.version", "stackA.version"]
    assert _read(tmp_path / "state" / "calcom.version")["stack"] == "calcom"


def test_overwrites_previous(tmp_path, monkeypatch):
    monkeypatch.setattr("kedge.cli.cmd_prune", lambda cfg: None)
    monkeypatch.setattr("kedge.cli.cmd_backup", lambda cfg: None)
    CliRunner().invoke(main, ["backup"], env=_env(tmp_path))
    CliRunner().invoke(main, ["prune"], env=_env(tmp_path))
    assert _read(tmp_path / "state" / "stackA.version")["run"] == "prune"


def test_version_unknown_for_source_fallback(monkeypatch):
    from kedge import state

    monkeypatch.setattr(state, "__version__", "0.0.0+source")
    assert state.kedge_version() == "unknown"


def test_default_state_dir(monkeypatch):
    from kedge import state

    monkeypatch.delenv("KEDGE_STATE_DIR", raising=False)
    assert str(state.state_dir()) == "/var/lib/kedge"
