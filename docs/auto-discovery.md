# Auto-Discovery

docker-stack-backup discovers everything it needs from `docker-compose.yml` at runtime. No configuration files, no volume lists, no manual hooks. When you add or remove containers, the next backup automatically adjusts.

## How It Works

The backup script runs `docker compose config --format json` and parses the output with `jq` to discover:

### Named Volumes

All volumes defined in the `volumes:` top-level key of the compose file:

```yaml
volumes:
  pg_data:        # ← discovered
  valkey_data:    # ← discovered
  grafana_data:   # ← discovered
```

Docker Compose prefixes volume names with the project name (e.g., `myproject_pg_data`). The script resolves this automatically via `docker volume ls`.

### Bind Mounts

All host paths mounted into containers:

```yaml
services:
  nginx:
    volumes:
      - ./html:/usr/share/nginx/html:ro    # ← discovered (relative to stack dir)
      - /opt/certs:/certs:ro               # ← discovered (external, archived separately)
```

- Mounts **inside** the stack directory → included when backing up the stack dir
- Mounts **outside** the stack directory → archived separately as tar.gz

### Database Pre-Hooks

Services are matched by **image name** to determine if a pre-backup dump is needed:

| Image pattern | Hook | Command |
|---------------|------|---------|
| `*postgres*`, `*postgis*` | PostgreSQL | `pg_dumpall` (gzipped) |
| `*mariadb*`, `*mysql*` | MySQL | `mysqldump --all-databases` (gzipped) |
| `*valkey*`, `*redis*` | Valkey/Redis | `BGSAVE` (triggers save, data in volume) |
| `*mongo*` | MongoDB | `mongodump --archive --gzip` |

The script auto-detects the database user from container environment variables:
- PostgreSQL: reads `POSTGRES_USER` (default: `postgres`)
- MySQL: reads `MYSQL_ROOT_PASSWORD` or `MARIADB_ROOT_PASSWORD`

### Stack Files

Always backed up:
- `docker-compose.yml` / `compose.yml` (and `.yaml` variants)
- `docker-compose.override.yml` (if present)
- `.env`, `.env.local`, `.env.production` (if present)

### Metadata

Each backup includes a `meta.json` with:
- Timestamp (UTC)
- Hostname
- Original stack directory path
- Docker version
- List of running containers (name, image, state)
- Volume name mapping (compose name → Docker name)

## Preview with `discover`

Run `dsb-backup discover` to see what would be backed up without actually running a backup:

```
=== Stack: /opt/myapp ===

--- Services ---
  nginx  (nginx:1-alpine)
  postgres  (postgres:17-alpine) [pre-hook: postgres]
  valkey  (valkey/valkey:8-alpine) [pre-hook: valkey]

--- Named Volumes ---
  pg_data  -> myapp_pg_data
  valkey_data  -> myapp_valkey_data

--- Bind Mounts ---
  /opt/myapp/nginx-html  [exists]
  /opt/myapp/config  [exists]

--- Compose Files ---
  docker-compose.yml

--- Env Files ---
  .env
```

## Excluding Volumes

To skip specific volumes (e.g., large cache volumes that don't need backup):

```bash
export BACKUP_EXCLUDE_VOLUMES="prometheus_data node_modules_cache"
dsb-backup backup
```

## Adding New Services

When you add a new service to `docker-compose.yml`:

1. Its volumes are automatically included in the next backup
2. If it's a recognized database, pre-hooks run automatically
3. No configuration changes needed

Example: adding a MariaDB service:

```yaml
services:
  mariadb:
    image: mariadb:11
    environment:
      MARIADB_ROOT_PASSWORD: ${MARIADB_ROOT_PASSWORD}
    volumes:
      - mariadb_data:/var/lib/mysql

volumes:
  mariadb_data:
```

Next backup will automatically:
- Dump all databases via `mysqldump`
- Export `mariadb_data` volume
- Include both in the restic snapshot
