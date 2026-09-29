# Diagnostics Center

The Diagnostics Center is a read-only health report and support-bundle exporter. It runs
a fixed set of checks, collects structured sections (databases, services, inventory,
integrations, workers, logs), and turns failures into remediation hints.

**It does not auto-recover anything.** No check restarts services, clears caches, or
changes device state. The `remediation` strings tell an operator what to do. The only
side effects of building a report are:

- a 30 s cache key (`lv-diag-<epoch>`) written by the cache ping,
- in-process HTTP GETs through Django's test `Client`,
- `manage.py check`, `ensure_topology_insights --check-only`, and
  `labvault_migration_health` run in-process,
- read-only driver probes: the first OCS device (`probe()` plus a crossconnect list when
  reachable), and up to 5 offline/auth-failed devices (`probe()`),
- `docker compose ps --format json` via subprocess, when a `docker` CLI is on `PATH`
  (8 s timeout; the error is recorded if the CLI is missing).

Because of the live probes and subprocesses, one report can take several seconds. Every
page view and every `live.json` refresh rebuilds the report from scratch.

## Endpoints

| Path | View | Auth | Output |
|---|---|---|---|
| `/diagnostics/` | `diagnostics_center` | `staff_member_required` | HTML page (`diagnostics_center.html`) |
| `/diagnostics/live.json` | `diagnostics_json_live` | staff | Full payload as JSON |
| `/diagnostics/export/` | `diagnostics_export_page` | staff | `labvault-diagnostics-<ts>.json` attachment |
| `/diagnostics/bundle/` | `diagnostics_bundle_export` | staff | `labvault-diagnostics-bundle-<ts>.tar.gz` |
| `/api/diagnostics.json` | `diagnostics_export_api` | `_api_auth_required` (session or Bearer token) **and** `is_staff`/`is_superuser`, else 403 | JSON; `?download=1` returns an attachment; `?bundle=1` returns the tar.gz |
| `/api/diagnostics/ingest/` | `diagnostics_log_ingest` | Header `X-LabVault-Diag-Secret` must equal `LABVAULT_DIAGNOSTICS_INGEST_SECRET` (endpoint disabled when unset) | Appends one JSONL line |

Query options (all read endpoints): `logs=0` omits log sections; `log_limit=` is clamped
to 50–1000 (default 500).

CLI: `python manage.py export_diagnostics [-o FILE] [--no-logs] [--log-limit N] [--bundle]`.

## Checks

`_run_all_checks()` returns rows of
`{name, category, ok, severity (ok|warn|fail), detail, remediation}`. `summary` counts
`passed` / `failed` / `warnings`. `overall_ok` (and top-level `ok`) is true when there are
no `fail` rows. Warnings do not flip it.

| Check | Category | Fails / warns when | Meaning |
|---|---|---|---|
| `django_system_check` | platform | `manage.py check` raised | Settings / app registry problem |
| `disk_headroom` | platform | ≥ 90 % used (warn), ≥ 95 % (fail) | Disk under `BASE_DIR` is nearly full; SQLite, cache, and logs will start failing |
| `migration_health` | platform | `labvault_migration_health` non-zero (warn) | Unapplied or inconsistent migrations. Take a backup, then migrate. |
| `database_default` | database | Cannot connect | Inventory DB unreachable (`DATABASE_URL`) |
| `database_np_timeseries` | database | Cannot connect | Time-series DB unreachable (`NP_TIMESERIES_DATABASE_URL`) |
| `postgres_connection_headroom` | database | ≥ 85 % of `max_connections` (warn), ≥ 95 % (fail) | Only on PostgreSQL. Likely connection leak or too many workers. |
| `cache_read_write` | services | Set/get round-trip failed | `LABVAULT_CACHE_DIR` not writable |
| `topology_insights` | services | `ensure_topology_insights --check-only` raised | `np_timeseries` tables missing |
| `fleet_heartbeat` | services | Any `halt_suspect` chassis, **or zero chassis tracked** (warn) | Heartbeat service down or chassis halted. Expect this warning on an install with no chassis. |
| `metrics_collector` | services | Query error or no samples at all (fail). Latest sample older than 900 s in `live` mode or 3600 s otherwise (warn; fail at twice that). | Collector not running, or writing nothing for the enabled topologies. In `idle` mode with no historical samples this check **fails**. |
| `device_refresh_thread`, `keysight_refresh_thread` | workers | Added only when `worker_status_payload()` marks the row `stale` (no heartbeat within 3 intervals) (warn) | Refresh loop not running |
| `aggregated_logs` | logs | `LABVAULT_DIAGNOSTICS_LOG_DIR` set but no entries (warn) | Nothing is shipping logs to the directory |
| `ocs_controller` | integrations | Added only when an OCS device exists; fails when the probe is not `ok` | OCS REST credentials or reachability |
| `devices_auth_failed`, `chassis_auth_failed` | inventory | Any rows with status `auth_failed` (warn) | Credentials need updating |
| `http_login_page`, `http_fleet_health`, `http_fleet_openapi`, `http_diagnostics_api` | api | Status differs from expected (`/login/` 200, `/api/fleet/health.json` 401, `/api/fleet/openapi.json` 200, `/api/diagnostics.json` 401) | Routing, auth, or middleware regression |

## Payload sections

`build_diagnostics_payload()` returns:

- `ok`, `generated_at`, `hostname`, `platform`, `python`, `summary`, `recommendations`
  (`"<check>: <remediation>"` for each failed check), `checks`.
- `sections.platform`: git revision, demo mode, `DEBUG`, `django_check`, disk/RSS,
  `path_sizes` (cache dir, `np_timeseries` file, media), `driver_registry` summary.
- `sections.databases`: per alias `reachable`, `vendor`, `engine`. PostgreSQL adds
  connection totals and the top 15 by application/state. SQLite adds the path and size.
- `sections.services`: collector stats (mode, node-type filter, allowlist, sample count,
  latest sample age, samples in the last hour), heartbeat (mode, counts, up to 50 stale
  chassis), `cache_ok`.
- `sections.integrations.ocs`: `configured`, device id/IP/hostname/status/username,
  `probe`, `crossconnect_count`.
- `sections.inventory`: device/chassis/topology/alert counts, and up to 25 auth-failed or
  offline rows each.
- `sections.api_smoke`, `topology_insights`, `host_logs`, `workers`, `processes`
  (`docker compose ps`), `driver_probes`, `migration_health`.
- `environment`: `LABVAULT_*`, `DJANGO_*`, `DATABASE_*`, `NP_*`, `GUNICORN_*` variables.
  Values whose **name** contains `password`, `secret`, `token`, `key`, `credential`,
  `auth`, or `private` are replaced by `***redacted***`.
- With logs: `recent_logs` (ring buffer) and `aggregated_logs` (host log store).
- Legacy flat keys (`database`, `fleet_heartbeat`, `inventory_summary`, `resources`,
  `labvault`) are kept for older consumers.

## Log sources

### Ring buffer (`connect/log_ring.py`)

`RingBufferHandler` is attached to the root logger in `settings.LOGGING`
(`diagnostics_ring`, level WARNING). It keeps the last 5000 WARNING+ records **per
process** in a `deque` guarded by a lock. Each record is
`{ts (UTC ISO), level, logger, message}`, and `message` uses the `verbose` formatter.
`recent_log_entries(limit, min_level)` returns the newest `limit` matching rows, oldest
first.

The buffer lives inside one gunicorn worker. A report only shows warnings from the worker
that served the request, and the buffer is lost on worker restart.

### Host log store (`connect/diagnostics_log_store.py`)

Optional. It is enabled when `LABVAULT_DIAGNOSTICS_LOG_DIR` points at an existing directory.

- `read_host_logs(limit, service=None, min_level='INFO')` reads `<dir>/<service>.jsonl` for
  services `web`, `collector`, `heartbeat`, `nginx`, `db`. It parses the last
  `max(2*limit, 200)` lines of each file, filters by level, merges and sorts by `ts`, and
  keeps `limit` rows. It also returns per-service `{path, exists, count, size_bytes}` and
  `agent-meta.json` when present.
- `host_log_bundle_files(limit)` adds raw tails as `logs/<svc>.jsonl` plus
  `logs/summary.json` to the bundle.

The customer SKU ships no log agent. Populate the directory with your own shipper, or POST
to the ingest endpoint:

```bash
curl -X POST https://<host>:9443/api/diagnostics/ingest/ \
  -H "X-LabVault-Diag-Secret: $LABVAULT_DIAGNOSTICS_INGEST_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"service": "collector", "level": "WARNING", "ts": "2026-01-01T00:00:00Z", "message": "..."}'
```

The endpoint returns 403 when the secret is unset or wrong, 400 for invalid JSON, and 503
when the log directory is not configured. Messages are truncated to 8000 characters. Only
the five service names above are read back.

## Bundle layout

`build_diagnostics_bundle_files()` / `build_diagnostics_tarball()`:

```text
README.txt                 triage steps
summary.json               ok, summary, recommendations, labvault
checks.json
sections/{databases,services,inventory,integrations,platform,api_smoke,host_logs,workers}.json
recent_logs.json           ring buffer (web process only)
environment.json           redacted env
full_report.json           entire payload
logs/<svc>.jsonl, logs/summary.json   (when the host log dir is configured)
```

Review a bundle before sending it outside your organisation. It contains hostnames,
management IPs, device usernames, and log messages.

## Per-module reference

- `connect/diagnostics.py`: `build_diagnostics_payload`, `build_diagnostics_bundle_files`,
  `build_diagnostics_tarball`. Collectors: `_db_check`, `_disk_mem`, `_path_sizes`,
  `_inventory_summary`, `_collector_stats`, `_heartbeat_detail`, `_api_smoke_tests`,
  `_django_system_check`, `_topology_insights_check`, `_ocs_status`,
  `_migration_health_check`, `_driver_probe_summary`, `_process_health`. Evaluation:
  `_run_all_checks`, `_add_check`, `_summary`, `_recommendations`, `_redact_env`,
  `_cache_ping`, `_git_revision`.
- `connect/diagnostics_views.py`: the six views above, plus `_parse_opts`.
- `connect/diagnostics_log_store.py`: `diagnostics_log_dir`, `read_host_logs`,
  `host_log_bundle_files`.
- `connect/log_ring.py`: `RingBufferHandler`, `recent_log_entries`.

## Known risks (as of this writing)

- `_redact_env` redacts by variable **name** only. `DATABASE_URL` and
  `NP_TIMESERIES_DATABASE_URL` match none of the secret substrings, so a URL with an
  embedded password is exported in clear text in `environment` / `environment.json`.
- `diagnostics_log_ingest` is not `csrf_exempt`, and `CsrfViewMiddleware` is enabled, so a
  non-browser log shipper without a CSRF cookie gets a CSRF 403 before the secret is
  checked.
- The ingest endpoint builds the file name from the client-supplied `service` without
  sanitising it. A caller that holds the secret can use `../` to write outside the log
  directory.
- The HTTP smoke tests use Django's test `Client` (host `testserver`). Unless `testserver`
  is in `DJANGO_ALLOWED_HOSTS` (the default is `localhost,127.0.0.1`), they get HTTP 400 and
  the `http_*` checks fail.
- Under gunicorn the in-process device refresh thread is disabled. If nothing else records
  device refresh heartbeats, `device_refresh_thread` shows as stale.
- `_parse_opts` calls `int(log_limit)` without handling errors, so a non-numeric value
  returns HTTP 500.
