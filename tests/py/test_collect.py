"""Unit tests for kedge.collect — volume/stack-file collection, metadata."""

import json
import subprocess

from kedge.collect import collect_stack_files, collect_volumes, write_metadata

SAMPLE_CONFIG = {
    "volumes": {"webdata": {}},
    "services": {"web": {"image": "nginx:alpine"}},
}


def _dispatch(handlers):
    def fake_run(cmd, **kwargs):
        for prefix, handler in handlers.items():
            if cmd[: len(prefix)] == list(prefix):
                return handler(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return fake_run


def test_collect_volumes_direct_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "kedge.collect.resolve_volume_name", lambda pattern: "mystack_webdata"
    )
    monkeypatch.setattr(
        "kedge.collect.resolve_volume_path", lambda vol: str(tmp_path / "docker-vol")
    )
    (tmp_path / "docker-vol").mkdir()

    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("du",): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="12M\t/x\n"),
    }))

    paths = collect_volumes(SAMPLE_CONFIG, tmp_path / "volmap", exclude_volumes=[])
    assert paths == [str(tmp_path / "docker-vol")]


def test_collect_volumes_excluded_volume_skipped(monkeypatch, tmp_path):
    called = {"resolve": False}

    def fail_resolve(pattern):
        called["resolve"] = True
        return "should-not-be-called"

    monkeypatch.setattr("kedge.collect.resolve_volume_name", fail_resolve)
    paths = collect_volumes(SAMPLE_CONFIG, tmp_path / "volmap", exclude_volumes=["webdata"])
    assert paths == []
    assert called["resolve"] is False


def test_collect_volumes_not_found_in_docker(monkeypatch, tmp_path):
    monkeypatch.setattr("kedge.collect.resolve_volume_name", lambda pattern: "")
    paths = collect_volumes(SAMPLE_CONFIG, tmp_path / "volmap", exclude_volumes=[])
    assert paths == []


def test_collect_volumes_tar_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("kedge.collect.resolve_volume_name", lambda pattern: "mystack_webdata")
    monkeypatch.setattr("kedge.collect.resolve_volume_path", lambda vol: "")  # not accessible

    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("docker", "run"): lambda cmd: subprocess.CompletedProcess(cmd, 0),
        ("du",): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="5M\t/x\n"),
    }))

    vol_map_dir = tmp_path / "volmap"
    paths = collect_volumes(SAMPLE_CONFIG, vol_map_dir, exclude_volumes=[])
    assert paths == []  # tar fallback doesn't add a direct restic path


def test_collect_stack_files_copies_compose_and_env(monkeypatch, tmp_path):
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    (stack_dir / "docker-compose.yml").write_text("services: {}\n")
    (stack_dir / ".env").write_text("FOO=bar\n")

    target_dir = tmp_path / "staging"
    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("rsync",): lambda cmd: subprocess.CompletedProcess(cmd, 0),
    }))

    collect_stack_files(stack_dir, {"services": {}}, target_dir, exclude_mounts=[])

    assert (target_dir / "docker-compose.yml").is_file()
    assert (target_dir / ".env").is_file()


def test_collect_stack_files_external_mount_tarred(monkeypatch, tmp_path):
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    external = tmp_path / "external-data"
    external.mkdir()
    (external / "f.txt").write_text("hi\n")

    config = {"services": {"app": {"volumes": [{"type": "bind", "source": str(external), "target": "/x"}]}}}
    target_dir = tmp_path / "staging"

    tar_calls = []
    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("rsync",): lambda cmd: subprocess.CompletedProcess(cmd, 0),
        ("tar",): lambda cmd: tar_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    }))

    collect_stack_files(stack_dir, config, target_dir, exclude_mounts=[])

    assert len(tar_calls) == 1
    assert (target_dir / "external-mounts").is_dir()


def test_collect_stack_files_excluded_mount_skipped(monkeypatch, tmp_path):
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    config = {"services": {"app": {"volumes": [{"type": "bind", "source": "/proc", "target": "/x"}]}}}
    target_dir = tmp_path / "staging"

    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("rsync",): lambda cmd: subprocess.CompletedProcess(cmd, 0),
    }))

    collect_stack_files(stack_dir, config, target_dir, exclude_mounts=["/proc"])
    assert not (target_dir / "external-mounts").exists()


def test_write_metadata(monkeypatch, tmp_path):
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    target = tmp_path / "staging"
    target.mkdir()

    ps_output = json.dumps({"Name": "stack-web-1", "Image": "nginx:alpine", "State": "running"})
    monkeypatch.setattr("kedge.collect.resolve_volume_name", lambda pattern: "mystack_webdata")
    monkeypatch.setattr("kedge.collect.resolve_volume_path", lambda vol: "/var/lib/docker/volumes/x/_data")

    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("docker", "compose", "ps"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout=ps_output + "\n"),
        ("docker", "--version"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="Docker version 27.0.0\n"),
    }))

    write_metadata(stack_dir, SAMPLE_CONFIG, ["docker", "compose"], target)

    meta = json.loads((target / "meta.json").read_text())
    assert meta["format_version"] == "1.0.0"
    assert meta["containers"][0]["name"] == "stack-web-1"
    assert meta["volume_mapping"]["webdata"] == "mystack_webdata"
    assert meta["volume_paths"]["webdata"] == "/var/lib/docker/volumes/x/_data"
    assert meta["docker_version"] == "Docker version 27.0.0"
