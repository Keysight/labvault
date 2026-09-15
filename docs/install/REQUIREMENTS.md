# Requirements

## Supported operating systems

| OS | Package manager | Notes |
|----|-----------------|-------|
| Rocky Linux 9 / RHEL 9 | `dnf` | Preferred for bare metal. Stock `python3` is 3.9 — oneshot installs **python3.11** (Django 5.2 needs 3.10+) |
| Ubuntu 22.04 / 24.04 | `apt` | Fully supported |
| Other EL9 | `dnf` | Untested but usually fine |

## Hardware

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| vCPU | 2 | 4 |
| RAM | 4 GiB | 8 GiB |
| Disk | 40 GiB | 80+ GiB (telemetry growth) |

## Software matrix

| Component | Compose path | Systemd path |
|-----------|--------------|--------------|
| Docker Engine 24+ + Compose v2 plugin | **Required** | Optional |
| Python 3.10+ (3.11 preferred; Django 5.2 LTS) | In container image | Host + `.venv` |
| PostgreSQL 14/15 | Bundled (`db`, `metrics-db`) | Required for production (SQLite OK for labs). Rocky 9 default `postgresql-server` is **13** — oneshot enables `postgresql:15` |
| nginx | Optional front | Optional TLS front |
| OpenLDAP client libs (build) | In image (`libldap2-dev`) | **Required on host before pip** |

## Native packages (do not skip)

`django-auth-ldap` pulls `python-ldap`, which **compiles** unless you use a wheelhouse:

```bash
# Rocky / RHEL 9
sudo dnf install -y python3.11 python3.11-devel python3.11-pip \
  gcc openldap-devel openssl-devel cyrus-sasl-devel libpq-devel

# Ubuntu / Debian
sudo apt-get install -y python3-dev build-essential libldap2-dev libsasl2-dev libpq-dev

# Or
sudo ./labvaultctl host-deps --install
```

Deploy-matrix failure mode without these: `Building wheel for python-ldap ... error`.

`build-wheelhouse.sh` is fail-closed: host-dep install and `python-ldap` wheel builds must succeed. Skip only with explicit `LABVAULT_SKIP_WHEEL_HOSTDEPS=1` or `LABVAULT_SKIP_LDAP_WHEEL=1`.

## Network ports

| Port | Role |
|------|------|
| **9443/tcp** | nginx TLS (customer default) |
| 8000/tcp | Gunicorn loopback upstream only |
| 80/tcp | optional redirect to `https://<host>:9443` |
| 80/tcp | HTTP→HTTPS redirect (optional) |
| 5432/tcp | Postgres (local or compose network) |

Legacy internal stacks sometimes used **18000** — this customer SKU oneshot does **not**. Nginx templates upstream `127.0.0.1:8000` and publish HTTPS on **9443**.

## Health & login paths

| Path | Notes |
|------|--------|
| `/health/live` | No trailing slash |
| `/health/ready` | Load-balancer probe |
| `/login/` | Not `/accounts/login/` |
| `/cli/` | Staff only (non-staff → 403) |

## Secret policy

`DJANGO_SECRET_KEY` must be ≥32 characters and must not resemble `change-me` / `demo` / `placeholder`. Enforced by `validate_external_config` at install and systemd `ExecStartPre`.
