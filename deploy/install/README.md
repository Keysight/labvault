# `deploy/install/`

Oneshot installers. Run as root from the source tree. Each ends by printing `READY`, the
login URL, and where the credential and fleet-token files are
(`/var/lib/labvault/bootstrap-credentials`, `/var/lib/labvault/fleet-token`).

| File | Purpose |
|---|---|
| `oneshot-compose.sh` | Docker Compose install: host `labvault-opsd` unit + Compose drop-in, `.env` from `.env.example` with a generated secret key, self-signed TLS, `docker compose up -d --build`, migrate + `bootstrap_labvault` in the `web` container, copy credential files out, `post_deploy_verify.sh` |
| `oneshot-systemd.sh [INSTALL_ROOT]` | Bare-metal / VM install: OS packages, PostgreSQL 14+ with DBs `labvault` and `labvault_metrics`, user `labvault` + group `labvault-ops`, rsync to `/opt/labvault/current`, `.venv`, `/etc/labvault/labvault.env`, units from `deploy/systemd/`, `labvaultctl install`, verify |
| `oneshot-airgap.sh WHEELHOUSE [INSTALL_ROOT]` | Same as systemd but `pip install --no-index` from a wheelhouse and no network package installs |
| `build-wheelhouse.sh [OUT]` | On a networked builder: download all wheels (plus a built `python-ldap` wheel) into `dist/wheelhouse` for the air-gapped host |
| `lib-ready.sh` | Sourced helpers: TLS port/origin defaults, TLS and `:80` edge installers, the final `READY` banner |

Environment switches read by the oneshots: `LABVAULT_WORKER_MODE` (`idle`/`live`),
`LABVAULT_RESTORE_DATASET` (restore a `labvault-full-export` JSON and go live),
`LABVAULT_BOOTSTRAP_RANDOM=1` (random first-login password instead of the demo default — see
[FIRST_LOGIN.md](../../docs/getting-started/FIRST_LOGIN.md)), `LABVAULT_TLS_PORT` (9443),
`LABVAULT_SKIP_TLS`, `LABVAULT_SKIP_HTTP80`, `LABVAULT_SKIP_VERIFY`.

The systemd and air-gapped oneshots set the local PostgreSQL role password and switch
localhost `pg_hba` rules to `md5` (a `.labvault.bak` copy is kept). On EL hosts
`oneshot-systemd.sh` removes an existing `/var/lib/pgsql/data` cluster older than
PostgreSQL 14. Read the scripts before running them on a host that already runs PostgreSQL.
