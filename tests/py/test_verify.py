"""Unit tests for kedge.verify — port of verify.sh (cmd_verify/cmd_burn)."""

from __future__ import annotations

import subprocess

import pytest

from kedge import verify
from kedge.config import Config
from kedge.errors import KedgeError


def _cfg(**overrides):
    defaults = dict(stack_dir=None, restic_repository="/backup/x", restic_password="secret")
    defaults.update(overrides)
    return Config(**defaults)


# --- VerifyConfig.from_env ---------------------------------------------------

def test_verify_config_from_env_defaults(monkeypatch):
    for key in ("HCLOUD_CONTEXT", "HCLOUD_TOKEN", "VERIFY_SERVER_TYPE", "VERIFY_LOCATION",
                "SSH_KEY_NAME", "RESTORE_TARGET", "VERIFY_POST_HOOK", "VERIFY_FAIL_HOOK"):
        monkeypatch.delenv(key, raising=False)
    vcfg = verify.VerifyConfig.from_env()
    assert vcfg.hcloud_context == "kigulls-test"
    assert vcfg.server_type == "cpx23"
    assert vcfg.location == "nbg1"
    assert vcfg.ssh_key_name == "stephan@waldtmann.de"
    assert vcfg.restore_target == "/opt/stack"


def test_verify_config_from_env_overrides(monkeypatch):
    monkeypatch.setenv("HCLOUD_CONTEXT", "my-ctx")
    monkeypatch.setenv("VERIFY_SERVER_TYPE", "cax21")
    vcfg = verify.VerifyConfig.from_env()
    assert vcfg.hcloud_context == "my-ctx"
    assert vcfg.server_type == "cax21"


# --- check_verify_prereqs -----------------------------------------------------

def test_check_verify_prereqs_missing_tools(monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: None)
    with pytest.raises(KedgeError, match="Required:"):
        verify.check_verify_prereqs(_cfg(), verify.VerifyConfig())


def test_check_verify_prereqs_missing_restic_repository(monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: "/usr/bin/" + tool)
    with pytest.raises(KedgeError, match="RESTIC_REPOSITORY not set"):
        verify.check_verify_prereqs(_cfg(restic_repository=""), verify.VerifyConfig())


def test_check_verify_prereqs_hcloud_context_switch_failure(monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: "/usr/bin/" + tool)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["hcloud", "context", "active"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="other-ctx\n")
        if cmd[:3] == ["hcloud", "context", "use"]:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(KedgeError, match="hcloud context 'kigulls-test' not found"):
        verify.check_verify_prereqs(_cfg(), verify.VerifyConfig())


def test_check_verify_prereqs_ok(monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="kigulls-test\n"))
    verify.check_verify_prereqs(_cfg(), verify.VerifyConfig())  # must not raise


# --- resolve_snapshot ----------------------------------------------------------

def test_resolve_snapshot_passthrough_for_explicit_id():
    assert verify.resolve_snapshot(_cfg(), "abc123") == "abc123"


def test_resolve_snapshot_latest_resolves_via_restic(monkeypatch):
    from kedge import restic

    monkeypatch.setattr(restic, "latest_snapshot_short_id", lambda cfg: "def456")
    assert verify.resolve_snapshot(_cfg(), "latest") == "def456"


def test_resolve_snapshot_latest_raises_when_empty(monkeypatch):
    from kedge import restic

    monkeypatch.setattr(restic, "latest_snapshot_short_id", lambda cfg: "unknown")
    with pytest.raises(KedgeError, match="No snapshots found"):
        verify.resolve_snapshot(_cfg(), "latest")


# --- create_box / burn_box ----------------------------------------------------

def test_create_box_first_combo_succeeds(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["hcloud", "server", "create"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:4] == ["hcloud", "server", "list", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="kedge-verify-1200  1.2.3.4\n")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ip = verify.create_box(verify.VerifyConfig(), "kedge-verify-1200")
    assert ip == "1.2.3.4"
    create_calls = [c for c in calls if c[:3] == ["hcloud", "server", "create"]]
    assert len(create_calls) == 1


def test_create_box_falls_back_through_combos(monkeypatch):
    attempts = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["hcloud", "server", "create"]:
            attempts.append((cmd[cmd.index("--type") + 1], cmd[cmd.index("--location") + 1]))
            # succeed only on the 2nd attempt
            return subprocess.CompletedProcess(cmd, 0 if len(attempts) == 2 else 1)
        if cmd[:3] == ["hcloud", "server", "delete"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:4] == ["hcloud", "server", "list", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="box  5.6.7.8\n")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ip = verify.create_box(verify.VerifyConfig(server_type="cpx23", location="nbg1"), "box")
    assert ip == "5.6.7.8"
    assert len(attempts) == 2


def test_create_box_all_combos_fail_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["hcloud", "server", "create"]:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(KedgeError, match="all type/location combinations failed"):
        verify.create_box(verify.VerifyConfig(), "box")


def test_burn_box_deletes_when_present(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["hcloud", "server", "list", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="kedge-verify-1200\nother-box\n")
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    verify.burn_box(verify.VerifyConfig(), "kedge-verify-1200")
    assert ["hcloud", "server", "delete", "kedge-verify-1200"] in calls


def test_burn_box_noop_when_absent(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["hcloud", "server", "list", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="other-box\n")
        raise AssertionError("delete must not be called for an absent box")

    monkeypatch.setattr(subprocess, "run", fake_run)
    verify.burn_box(verify.VerifyConfig(), "kedge-verify-1200")


# --- wait_for_ssh ----------------------------------------------------------------

def test_wait_for_ssh_succeeds_immediately(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    verify.wait_for_ssh("1.2.3.4")  # must not raise/sleep


def test_wait_for_ssh_times_out(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ssh-keygen":
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 1)  # never succeeds

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(KedgeError, match="SSH timeout"):
        verify.wait_for_ssh("1.2.3.4", max_wait=10, interval=5)


# --- bootstrap_box / run_health_checks --------------------------------------------

def test_bootstrap_box_raises_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(KedgeError, match="Bootstrap failed"):
        verify.bootstrap_box("1.2.3.4")


def test_bootstrap_box_ok(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    verify.bootstrap_box("1.2.3.4")  # must not raise


def test_run_health_checks_all_passed(monkeypatch, capsys):
    output = "--- Health Checks ---\nCHECKS=4\nFAILURES=0\nCONTAINERS=3/3\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=output))
    ok, containers = verify.run_health_checks("1.2.3.4", "/opt/stack")
    assert ok is True
    assert containers == "3/3"


def test_run_health_checks_failures_reported(monkeypatch):
    output = "CHECKS=4\nFAILURES=2\nCONTAINERS=1/3\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=output))
    ok, containers = verify.run_health_checks("1.2.3.4", "/opt/stack")
    assert ok is False
    assert containers == "1/3"


# --- cmd_verify (full orchestration, everything mocked) --------------------------

def test_cmd_verify_success_burns_box_by_default(monkeypatch):
    monkeypatch.setattr(verify, "check_verify_prereqs", lambda cfg, vcfg: None)
    monkeypatch.setattr(verify, "resolve_snapshot", lambda cfg, sid: "abc123")
    monkeypatch.setattr(verify, "create_box", lambda vcfg, name: "1.2.3.4")
    monkeypatch.setattr(verify, "wait_for_ssh", lambda ip: None)
    monkeypatch.setattr(verify, "bootstrap_box", lambda ip: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(verify, "run_health_checks", lambda ip, target: (True, "2/2"))

    burned = []
    monkeypatch.setattr(verify, "burn_box", lambda vcfg, name: burned.append(name))

    hooks_run = []
    monkeypatch.setattr(verify, "run_hook", lambda cmd, name, ctx: hooks_run.append(name) if cmd else None)

    ok = verify.cmd_verify(_cfg(), verify.VerifyConfig(post_hook="echo ok"), "latest", keep_box=False)

    assert ok is True
    assert len(burned) == 1
    assert hooks_run == ["verify-post-hook"]


def test_cmd_verify_keep_box_skips_burn(monkeypatch):
    monkeypatch.setattr(verify, "check_verify_prereqs", lambda cfg, vcfg: None)
    monkeypatch.setattr(verify, "resolve_snapshot", lambda cfg, sid: "abc123")
    monkeypatch.setattr(verify, "create_box", lambda vcfg, name: "1.2.3.4")
    monkeypatch.setattr(verify, "wait_for_ssh", lambda ip: None)
    monkeypatch.setattr(verify, "bootstrap_box", lambda ip: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(verify, "run_health_checks", lambda ip, target: (True, "2/2"))

    def fail_burn(*a, **kw):
        raise AssertionError("must not burn when --keep is set")

    monkeypatch.setattr(verify, "burn_box", fail_burn)
    verify.cmd_verify(_cfg(), verify.VerifyConfig(), "latest", keep_box=True)


def test_cmd_verify_failed_health_check_fires_fail_hook_and_still_burns(monkeypatch):
    monkeypatch.setattr(verify, "check_verify_prereqs", lambda cfg, vcfg: None)
    monkeypatch.setattr(verify, "resolve_snapshot", lambda cfg, sid: "abc123")
    monkeypatch.setattr(verify, "create_box", lambda vcfg, name: "1.2.3.4")
    monkeypatch.setattr(verify, "wait_for_ssh", lambda ip: None)
    monkeypatch.setattr(verify, "bootstrap_box", lambda ip: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(verify, "run_health_checks", lambda ip, target: (False, "1/2"))

    burned = []
    monkeypatch.setattr(verify, "burn_box", lambda vcfg, name: burned.append(name))
    hooks_run = []
    monkeypatch.setattr(verify, "run_hook", lambda cmd, name, ctx: hooks_run.append((name, ctx.error)) if cmd else None)

    ok = verify.cmd_verify(_cfg(), verify.VerifyConfig(fail_hook="echo fail"), "latest", keep_box=False)

    assert ok is False
    assert len(burned) == 1
    assert hooks_run == [("verify-fail-hook", "Restore verification failed")]


def test_cmd_verify_burns_box_even_when_restore_step_raises(monkeypatch):
    """Box creation succeeded but the remote restore command failed
    (check=True raises CalledProcessError) — box must still get burned,
    not leaked."""
    monkeypatch.setattr(verify, "check_verify_prereqs", lambda cfg, vcfg: None)
    monkeypatch.setattr(verify, "resolve_snapshot", lambda cfg, sid: "abc123")
    monkeypatch.setattr(verify, "create_box", lambda vcfg, name: "1.2.3.4")
    monkeypatch.setattr(verify, "wait_for_ssh", lambda ip: None)
    monkeypatch.setattr(verify, "bootstrap_box", lambda ip: None)

    def fake_run(cmd, **kwargs):
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    burned = []
    monkeypatch.setattr(verify, "burn_box", lambda vcfg, name: burned.append(name))
    monkeypatch.setattr(verify, "run_hook", lambda *a, **kw: None)

    with pytest.raises(subprocess.CalledProcessError):
        verify.cmd_verify(_cfg(), verify.VerifyConfig(), "latest", keep_box=False)

    assert len(burned) == 1


# --- cmd_burn ----------------------------------------------------------------

def test_cmd_burn_deletes_all_prefixed_boxes(monkeypatch):
    deleted = []

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["hcloud", "server", "list", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="kedge-verify-1200\nkedge-verify-1300\nother-box\n")
        deleted.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    verify.cmd_burn(verify.VerifyConfig())

    delete_targets = {c[3] for c in deleted if c[:3] == ["hcloud", "server", "delete"]}
    assert delete_targets == {"kedge-verify-1200", "kedge-verify-1300"}
