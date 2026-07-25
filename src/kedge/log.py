"""Console logging — port of backup.sh's _log/info/ok/warn/err (lines 97-106)."""

from __future__ import annotations

from datetime import datetime

import click


def _log(tag: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    click.echo(f"[{ts}] {tag}  {msg}")


def info(msg: str) -> None:
    _log("==>", msg)


def ok(msg: str) -> None:
    _log(" ok", msg)


def warn(msg: str) -> None:
    _log("wrn", msg)


def err(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    click.echo(f"[{ts}] ERR  {msg}", err=True)
