"""Unit tests for kedge.hooks — Phase 1 stub (real dumps land in #4)."""

from kedge.hooks import run_pre_hooks


def test_no_db_containers_returns_zero(tmp_path):
    config = {"services": {"web": {"image": "nginx:alpine"}}}
    dump_dir = tmp_path / "dumps"
    assert run_pre_hooks(config, dump_dir) == 0
    assert dump_dir.is_dir()  # backup.sh creates it unconditionally, matched for parity


def test_db_container_detected_warns_but_does_not_dump(tmp_path, capsys):
    config = {"services": {"db": {"image": "mariadb:11.5"}}}
    result = run_pre_hooks(config, tmp_path / "dumps")
    assert result == 0
    captured = capsys.readouterr()
    assert "not yet ported" in captured.out
    assert "mysql" in captured.out
