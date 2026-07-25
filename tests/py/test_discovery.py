"""Unit tests for kedge.discovery — parity checks against backup.sh behavior."""

import subprocess

import pytest

from kedge.discovery import (
    build_discover_report,
    check_hot_safety,
    compose_config,
    detect_db_type,
    discover_bind_mounts,
    discover_services,
    discover_volumes,
    format_discover_report,
    is_excluded_mount,
    is_excluded_volume,
    is_hot_safe_image,
    resolve_volume_name,
    resolve_volume_path,
)

SAMPLE_CONFIG = {
    "volumes": {"zeta_data": {}, "alpha_data": {}, "mid_data": {}},
    "services": {
        "web": {"image": "traefik:v3.1"},
        "db": {"image": "mariadb:11.5"},
        "app": {
            "image": "myapp:1.0",
            "volumes": [
                {"type": "bind", "source": "/opt/app/config", "target": "/config"},
                {"type": "bind", "source": "/opt/app/data", "target": "/data"},
                {"type": "volume", "source": "zeta_data", "target": "/var/data"},
                "shorthand:/nope",
            ],
        },
        "worker": {},  # no image key -> "build"
    },
}


# --- detect_db_type (backup.sh:248-259) ---------------------------------

@pytest.mark.parametrize(
    "image,expected",
    [
        ("mariadb:11.5", "mysql"),
        ("mysql:8.0", "mysql"),
        ("postgres:16", "postgres"),
        ("postgis/postgis:16-3.4", "postgres"),
        ("valkey:7", "valkey"),
        ("redis:7-alpine", "valkey"),
        ("mongo:7", "mongo"),
        ("xwiki:16.9.0-mariadb-tomcat", ""),  # db name only in tag suffix -> no match
        ("nginx:1.27", ""),
    ],
)
def test_detect_db_type(image, expected):
    assert detect_db_type(image) == expected


# --- is_hot_safe_image (backup.sh:264-303) ------------------------------

@pytest.mark.parametrize(
    "image,expected",
    [
        ("traefik:v3.1", True),
        ("prom/prometheus:v2.53", True),
        ("vaultwarden/server:1.32", True),
        ("myapp:1.0", False),
    ],
)
def test_is_hot_safe_image(image, expected):
    assert is_hot_safe_image(image) == expected


# --- discover_* (parsing compose config) --------------------------------

def test_discover_volumes_sorted_like_jq_keys():
    assert discover_volumes(SAMPLE_CONFIG) == ["alpha_data", "mid_data", "zeta_data"]


def test_discover_volumes_missing_key():
    assert discover_volumes({}) == []


def test_discover_bind_mounts_only_type_bind_deduped_sorted():
    assert discover_bind_mounts(SAMPLE_CONFIG) == ["/opt/app/config", "/opt/app/data"]


def test_discover_services_preserves_order_and_build_fallback():
    assert discover_services(SAMPLE_CONFIG) == [
        ("web", "traefik:v3.1"),
        ("db", "mariadb:11.5"),
        ("app", "myapp:1.0"),
        ("worker", "build"),
    ]


# --- check_hot_safety (backup.sh:305-330) -------------------------------

def test_check_hot_safety_flags_unknown_and_build_images():
    all_safe, warnings = check_hot_safety(SAMPLE_CONFIG)
    assert all_safe is False
    assert any("app" in w and "myapp:1.0" in w for w in warnings)
    assert any("worker" in w and "build image" in w for w in warnings)
    # web (hot-safe) and db (pre-hook) must not be flagged
    assert not any("web" in w for w in warnings)
    assert not any("'db'" in w for w in warnings)


def test_check_hot_safety_all_safe():
    cfg = {"services": {"proxy": {"image": "traefik:v3.1"}}}
    all_safe, warnings = check_hot_safety(cfg)
    assert all_safe is True
    assert warnings == []


# --- exclude filters -----------------------------------------------------

def test_is_excluded_volume():
    assert is_excluded_volume("cache_data", ["cache_data", "tmp_data"])
    assert not is_excluded_volume("real_data", ["cache_data"])


def test_is_excluded_mount_exact_and_prefix():
    excludes = ["/proc", "/sys"]
    assert is_excluded_mount("/proc", excludes)
    assert is_excluded_mount("/proc/self", excludes)
    assert not is_excluded_mount("/proceed", excludes)  # not a path-prefix match


# --- compose_config (subprocess boundary) --------------------------------

def test_compose_config_parses_json(monkeypatch, tmp_path):
    def fake_run(cmd, cwd, capture_output, text, check):
        assert cmd == ["docker", "compose", "config", "--format", "json"]
        return subprocess.CompletedProcess(cmd, 0, stdout='{"services": {}}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert compose_config(tmp_path, ["docker", "compose"]) == {"services": {}}


def test_compose_config_failure_returns_empty(monkeypatch, tmp_path):
    def fake_run(cmd, cwd, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert compose_config(tmp_path, ["docker", "compose"]) == {}


def test_compose_config_command_not_found(monkeypatch, tmp_path):
    def fake_run(*a, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert compose_config(tmp_path, ["docker-compose"]) == {}


# --- resolve_volume_name / path (subprocess boundary) ---------------------

def test_resolve_volume_name_matches_suffix(monkeypatch):
    def fake_run(cmd, capture_output, text, check):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="mystack_zeta_data\nother_volume\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_volume_name("zeta_data") == "mystack_zeta_data"


def test_resolve_volume_name_no_match(monkeypatch):
    def fake_run(cmd, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 0, stdout="unrelated_volume\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_volume_name("zeta_data") == ""


def test_resolve_volume_path(monkeypatch):
    def fake_run(cmd, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 0, stdout="/var/lib/docker/volumes/x/_data\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_volume_path("mystack_zeta_data") == "/var/lib/docker/volumes/x/_data"


def test_resolve_volume_path_not_found(monkeypatch):
    def fake_run(cmd, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such volume")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_volume_path("nope") == ""


# --- report assembly + rendering -----------------------------------------

def test_build_and_format_discover_report(tmp_path, monkeypatch):
    monkeypatch.setattr("kedge.discovery.resolve_volume_name", lambda pattern: "")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("FOO=bar\n")

    report = build_discover_report(
        stack_dir=tmp_path,
        compose_config_dict=SAMPLE_CONFIG,
        exclude_volumes=["alpha_data"],
        exclude_mounts=["/opt/app/config"],
        system_paths=["/etc"],
        system_paths_exclude=["/etc/shadow"],
    )

    assert report["stack_dir"] == str(tmp_path)
    assert report["compose_files"] == ["docker-compose.yml"]
    assert report["env_files"] == [".env"]
    assert {"path": "/etc", "exists": True} in report["system_paths"]
    vol_by_name = {v["name"]: v for v in report["volumes"]}
    assert vol_by_name["alpha_data"]["excluded"] is True
    mount_by_path = {m["path"]: m for m in report["bind_mounts"]}
    assert mount_by_path["/opt/app/config"]["excluded"] is True

    text = format_discover_report(report)
    assert "=== Stack:" in text
    assert "--- Named Volumes ---" in text
    assert "[EXCLUDED]" in text
