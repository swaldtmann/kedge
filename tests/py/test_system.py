"""Unit tests for kedge.system.hostname — must behave like `hostname -f
2>/dev/null || hostname`, NOT like socket.getfqdn() (see module docstring
for why: a real A/B run against backup.sh surfaced a garbage IPv6 PTR
hostname from getfqdn() on this dev machine)."""

import subprocess

from kedge.system import hostname


def test_prefers_hostname_dash_f(monkeypatch):
    def fake_run(cmd, capture_output, text, check):
        if cmd == ["hostname", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="host.example.com\n")
        raise AssertionError("should not fall through to plain hostname")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert hostname() == "host.example.com"


def test_falls_back_to_plain_hostname_when_dash_f_fails(monkeypatch):
    def fake_run(cmd, capture_output, text, check):
        if cmd == ["hostname", "-f"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="")
        if cmd == ["hostname"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="myhost\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert hostname() == "myhost"


def test_falls_back_to_socket_gethostname_if_both_fail(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout=""))
    monkeypatch.setattr("socket.gethostname", lambda: "fallback-host")
    assert hostname() == "fallback-host"
