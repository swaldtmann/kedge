"""Host-level helpers — port of backup.sh's `hostname -f 2>/dev/null || hostname`
(used for BACKUP_HOSTNAME / meta.json / restic --host throughout the shell
version). `socket.getfqdn()` is NOT a substitute — it does its own reverse-DNS
based canonicalization and can return garbage (e.g. an IPv6 loopback PTR
record) on hosts where `hostname -f` returns a clean name. Found via the
KEDGE-W-001 shell/python A-B comparison, not a theoretical concern.
"""

from __future__ import annotations

import socket
import subprocess


def hostname() -> str:
    for cmd in (["hostname", "-f"], ["hostname"]):
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return socket.gethostname()
