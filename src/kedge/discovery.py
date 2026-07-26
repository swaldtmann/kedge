"""Auto-discovery — port of backup.sh lines 208-367.

compose_config() shells out to `<compose_cmd> config --format json` exactly
like the shell version; everything downstream parses the resulting dict
instead of piping through jq.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from kedge.engines import engine_for_image

ENV_FILE_CANDIDATES = (".env", ".env.local", ".env.production")
COMPOSE_FILE_LISTING = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
)

# backup.sh:264-303 — known crash-consistent images, safe for hot backup
# without a pre-hook.
_HOT_SAFE_SUBSTRINGS: tuple[str, ...] = (
    "prometheus", "grafana", "loki", "alertmanager", "victoriametrics",
    "traefik", "nginx", "caddy", "haproxy",
    "authelia", "lldap", "keycloak", "dex",
    "crowdsec",
    "readeck", "wallabag", "linkding",
    "mosquitto", "emqx", "vernemq",
    "rabbitmq", "nats",
    "xwiki", "bookstack", "wiki.js",
    "mailcow", "dovecot", "mailserver", "stalwart",
    "vaultwarden", "bitwarden",
    "listmonk", "n8n", "gitea", "forgejo", "miniflux", "freshrss",
)


def compose_config(stack_dir, compose_cmd: list[str]) -> dict:
    """Run `<compose_cmd> config --format json` in stack_dir, parse JSON.

    Mirrors the shell's `2>/dev/null || true`: any failure (bad compose
    file, command error) yields an empty config rather than raising —
    discover/backup then simply report nothing found.
    """
    try:
        result = subprocess.run(
            [*compose_cmd, "config", "--format", "json"],
            cwd=stack_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        import json

        return json.loads(result.stdout)
    except ValueError:
        return {}


def discover_volumes(config: dict) -> list[str]:
    """jq `.volumes // {} | keys[]` — keys[] sorts alphabetically."""
    return sorted((config.get("volumes") or {}).keys())


def discover_bind_mounts(config: dict) -> list[str]:
    """jq `[...] | unique | .[]` — unique sorts + dedups."""
    sources = set()
    for service in (config.get("services") or {}).values():
        for vol in service.get("volumes") or []:
            if isinstance(vol, dict) and vol.get("type") == "bind":
                source = vol.get("source")
                if source:
                    sources.add(source)
    return sorted(sources)


def discover_services(config: dict) -> list[tuple[str, str]]:
    """jq `.services | to_entries[] | "key\\timage"` — insertion order."""
    return [
        (name, service.get("image") or "build")
        for name, service in (config.get("services") or {}).items()
    ]


def detect_db_type(image: str) -> str:
    """backup.sh:248-259 — match against the image name, tag stripped.
    KEDGE-W-004: delegates to the DB engine registry (kedge.engines) instead
    of a locally-kept pattern list."""
    engine = engine_for_image(image)
    return engine.name if engine else ""


def is_hot_safe_image(image: str) -> bool:
    """backup.sh:264-303 — matched against the full image string (with tag),
    same as the shell case statement."""
    return any(s in image for s in _HOT_SAFE_SUBSTRINGS)


def check_hot_safety(config: dict) -> tuple[bool, list[str]]:
    """backup.sh:305-330. Returns (all_safe, warnings)."""
    warnings: list[str] = []
    unsafe = 0
    for svc, image in discover_services(config):
        if not svc:
            continue
        if detect_db_type(image):
            continue
        if is_hot_safe_image(image):
            continue
        if image == "build":
            warnings.append(f"Service '{svc}' uses a build image — verify hot-backup safety manually")
            unsafe += 1
            continue
        warnings.append(f"Service '{svc}' ({image}) has no pre-hook and is not known to be crash-consistent")
        unsafe += 1
    return unsafe == 0, warnings


def is_excluded_volume(vol: str, excludes: list[str]) -> bool:
    return vol in excludes


def is_excluded_mount(mount: str, excludes: list[str]) -> bool:
    return any(mount == excl or mount.startswith(excl + "/") for excl in excludes)


def resolve_volume_name(pattern: str) -> str:
    """backup.sh:333-336 — `docker volume ls` filtered by a `(^|[-_])pattern$` regex."""
    result = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    regex = re.compile(rf"(^|[-_]){re.escape(pattern)}$")
    for line in result.stdout.splitlines():
        if regex.search(line):
            return line
    return ""


def resolve_volume_path(vol_name: str) -> str:
    """backup.sh:339-342."""
    result = subprocess.run(
        ["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", vol_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def build_discover_report(stack_dir: Path, compose_config_dict: dict, exclude_volumes: list[str],
                           exclude_mounts: list[str], system_paths: list[str],
                           system_paths_exclude: list[str]) -> dict:
    """Assemble the cmd_discover report (backup.sh:758-853) as a plain dict —
    same data for both --json and human-readable rendering."""
    services = []
    for svc, image in discover_services(compose_config_dict):
        db_type = detect_db_type(image)
        if db_type:
            status = f"pre-hook: {db_type}"
        elif is_hot_safe_image(image):
            status = "hot-safe"
        elif image == "build":
            status = "build — verify manually"
        else:
            status = None
        services.append({"service": svc, "image": image, "status": status})

    all_safe, safety_warnings = check_hot_safety(compose_config_dict)

    volumes = []
    for vol in discover_volumes(compose_config_dict):
        real = resolve_volume_name(vol)
        entry = {
            "name": vol,
            "excluded": is_excluded_volume(vol, exclude_volumes),
            "resolved": real or None,
            "path": None,
            "mode": None,
        }
        if real:
            path = resolve_volume_path(real)
            if path and Path(path).is_dir():
                entry["path"] = path
                entry["mode"] = "direct"
            else:
                entry["mode"] = "tar fallback"
        volumes.append(entry)

    bind_mounts = [
        {
            "path": mount,
            "exists": Path(mount).exists(),
            "excluded": is_excluded_mount(mount, exclude_mounts),
        }
        for mount in discover_bind_mounts(compose_config_dict)
    ]

    system_paths_report = [
        {"path": sp, "exists": Path(sp).exists()} for sp in system_paths
    ]

    compose_files = [f for f in COMPOSE_FILE_LISTING if (stack_dir / f).is_file()]
    env_files = [f for f in ENV_FILE_CANDIDATES if (stack_dir / f).is_file()]

    return {
        "stack_dir": str(stack_dir),
        "services": services,
        "hot_backup_safety": {"all_safe": all_safe, "warnings": safety_warnings},
        "volumes": volumes,
        "bind_mounts": bind_mounts,
        "system_paths": system_paths_report,
        "system_paths_exclude": system_paths_exclude,
        "compose_files": compose_files,
        "env_files": env_files,
    }


def format_discover_report(report: dict) -> str:
    """Human-readable rendering, section layout matches backup.sh:762-852."""
    lines = ["", f"=== Stack: {report['stack_dir']} ===", "", "--- Services ---"]

    for svc in report["services"]:
        suffix = f" [{svc['status']}]" if svc["status"] else ""
        lines.append(f"  {svc['service']}  ({svc['image']}){suffix}")

    lines += ["", "--- Hot Backup Safety ---"]
    safety = report["hot_backup_safety"]
    if safety["all_safe"]:
        lines.append("  All services have pre-hooks or are known crash-consistent")
        lines.append("  BACKUP_STOP_STACK=false is safe for this stack")
    else:
        for warning in safety["warnings"]:
            lines.append(f"  wrn  {warning}")
        lines.append("  Some services may not be safe for hot backup (see warnings above)")
        lines.append("  Review before setting BACKUP_STOP_STACK=false")

    lines += ["", "--- Named Volumes ---"]
    for vol in report["volumes"]:
        excl = " [EXCLUDED]" if vol["excluded"] else ""
        if not vol["resolved"]:
            info = "NOT FOUND"
        elif vol["mode"] == "direct":
            info = f"{vol['resolved']} ({vol['path']}) [direct]"
        else:
            info = f"{vol['resolved']} [tar fallback]"
        lines.append(f"  {vol['name']}  -> {info}{excl}")

    lines += ["", "--- Bind Mounts ---"]
    for mount in report["bind_mounts"]:
        exists = "exists" if mount["exists"] else "NOT FOUND"
        excl = " [EXCLUDED]" if mount["excluded"] else ""
        lines.append(f"  {mount['path']}  [{exists}]{excl}")

    lines += ["", "--- System Paths ---"]
    if not report["system_paths"]:
        lines.append("  (none configured)")
    else:
        for sp in report["system_paths"]:
            exists = "exists" if sp["exists"] else "NOT FOUND"
            lines.append(f"  {sp['path']}  [{exists}]")
        if report["system_paths_exclude"]:
            lines.append(f"  excludes: {' '.join(report['system_paths_exclude'])}")

    lines += ["", "--- Compose Files ---"]
    lines += [f"  {f}" for f in report["compose_files"]]

    lines += ["", "--- Env Files ---"]
    lines += [f"  {f}" for f in report["env_files"]]
    lines.append("")

    return "\n".join(lines)
