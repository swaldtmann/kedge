"""Unit tests for kedge.lifecycle_hooks — BACKUP_PRE/POST/FAIL_HOOK + healthcheck."""

import subprocess

from kedge.lifecycle_hooks import HookContext, ping_healthcheck, run_hook


def test_hook_context_as_env():
    ctx = HookContext(duration="42", size="1.2 GiB", snapshot="abc123", hostname="h", stack="s", timestamp="t", error="e")
    env = ctx.as_env()
    assert env == {
        "BACKUP_DURATION": "42",
        "BACKUP_SIZE": "1.2 GiB",
        "BACKUP_SNAPSHOT": "abc123",
        "BACKUP_HOSTNAME": "h",
        "BACKUP_STACK": "s",
        "BACKUP_TIMESTAMP": "t",
        "BACKUP_ERROR": "e",
    }


def test_run_hook_noop_when_empty():
    def fail_run(*a, **kw):
        raise AssertionError("should not be called")
    import kedge.lifecycle_hooks as mod
    orig = subprocess.run
    subprocess.run = fail_run
    try:
        run_hook("", "pre-hook", HookContext())
    finally:
        subprocess.run = orig


def test_run_hook_success_logs_ok(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    run_hook("echo hi", "post-hook", HookContext())
    assert "post-hook completed" in capsys.readouterr().out


def test_run_hook_failure_warns_but_does_not_raise(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 3))
    run_hook("false", "fail-hook", HookContext())
    assert "fail-hook failed (exit 3)" in capsys.readouterr().out


def test_run_hook_passes_context_vars_via_env(monkeypatch):
    captured = {}

    def fake_run(cmd, shell, env, check):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = HookContext(duration="5", stack="mystack")
    run_hook('echo "$BACKUP_STACK"', "post-hook", ctx)

    assert captured["env"]["BACKUP_STACK"] == "mystack"
    assert captured["env"]["BACKUP_DURATION"] == "5"


def test_ping_healthcheck_noop_when_no_url():
    def fail_run(*a, **kw):
        raise AssertionError("should not be called")
    orig = subprocess.run
    subprocess.run = fail_run
    try:
        ping_healthcheck("", "ok", HookContext())
    finally:
        subprocess.run = orig


def test_ping_healthcheck_ok_uses_base_url(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, check, timeout):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ping_healthcheck("https://hc.example.com/ping/abc", "ok", HookContext(hostname="h", stack="s"))

    assert captured["cmd"][-1] == "https://hc.example.com/ping/abc"


def test_ping_healthcheck_fail_appends_fail_suffix(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, check, timeout):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ping_healthcheck("https://hc.example.com/ping/abc/", "fail", HookContext(error="boom"))

    assert captured["cmd"][-1] == "https://hc.example.com/ping/abc/fail"
    assert "error: boom" in captured["cmd"][-2]


def test_ping_healthcheck_survives_timeout(monkeypatch):
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="curl", timeout=15)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ping_healthcheck("https://hc.example.com/ping/abc", "ok", HookContext())  # must not raise
