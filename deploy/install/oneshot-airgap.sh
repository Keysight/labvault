#!/usr/bin/env bash
# LabVault customer SKU — air-gapped oneshot from source tarball + wheelhouse.
# Usage (on air-gapped host as root):
#   tar xzf labvault-public.tgz -C /opt && cd /opt/labvault-public
#   ./deploy/install/oneshot-airgap.sh /path/to/wheelhouse [/opt/labvault/current]
set -euo pipefail

ROOT_SRC="$(cd "$(dirname "$0")/../.." && pwd)"
WHEELHOUSE="${1:-}"
INSTALL_ROOT="${2:-/opt/labvault/current}"
STATE="${LABVAULT_STATE_DIR:-/var/lib/labvault}"
ENV_FILE="${LABVAULT_ENV_FILE:-/etc/labvault/labvault.env}"

log() { printf '+ %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# shellcheck disable=SC1091
source "$ROOT_SRC/deploy/install/lib-ready.sh"

[[ "$(id -u)" -eq 0 ]] || die "run as root"
[[ -n "$WHEELHOUSE" && -d "$WHEELHOUSE" ]] || die "usage: $0 /path/to/wheelhouse [install_root]"

# Offline OS packages must already be present (python3.11+, openldap-devel, gcc, postgresql…).
PY=python3
if command -v python3.11 >/dev/null; then
  PY=python3.11
elif command -v python3.12 >/dev/null; then
  PY=python3.12
fi
command -v "$PY" >/dev/null || die "python3.11+ missing — install from local RPM/DEB mirror first"
"$PY" -c "import sys; assert sys.version_info >= (3, 10), sys.version"
"$PY" -c "import ensurepip,venv" 2>/dev/null || die "$PY venv support required"

id labvault >/dev/null 2>&1 || useradd --system --home /var/lib/labvault --shell /sbin/nologin labvault
getent group labvault-ops >/dev/null || groupadd --system labvault-ops
usermod -aG labvault-ops labvault
mkdir -p "$INSTALL_ROOT" "$STATE" /etc/labvault /run/labvault /var/lib/labvault/media
chown -R labvault:labvault "$STATE" /var/lib/labvault
chown root:labvault-ops /run/labvault
chmod 775 /run/labvault

if [[ "$ROOT_SRC" != "$INSTALL_ROOT" ]]; then
  rsync -a --delete --exclude '.venv' --exclude '.git' --exclude '.env' "$ROOT_SRC"/ "$INSTALL_ROOT"/
fi
# Never keep a Compose example .env under the install root (poisonous DATABASE_URL hosts).
rm -f "$INSTALL_ROOT/.env"
cd "$INSTALL_ROOT"

"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
# Prefer the prebuilt linux wheel over the python-ldap sdist when both exist.
rm -f "$WHEELHOUSE"/python_ldap-*.tar.gz
# Bootstrap build tooling from the wheelhouse before app deps (needed if any sdist remains).
pip install --no-index --find-links="$WHEELHOUSE" pip setuptools wheel
pip install --no-index --find-links="$WHEELHOUSE" -r requirements.txt gunicorn

# Prefer local Postgres when the offline OS packages already provide it.
use_pg=0
if id postgres >/dev/null 2>&1 && command -v psql >/dev/null; then
  if command -v postgresql-setup >/dev/null 2>&1; then
    if [[ ! -f /var/lib/pgsql/data/PG_VERSION && ! -f /var/lib/pgsql/15/data/PG_VERSION && ! -f /var/lib/pgsql/14/data/PG_VERSION && ! -f /var/lib/pgsql/13/data/PG_VERSION ]]; then
      postgresql-setup --initdb
    fi
  fi
  systemctl enable --now postgresql 2>/dev/null || \
    systemctl enable --now postgresql-15 2>/dev/null || \
    systemctl enable --now postgresql-13 || \
    die "failed to start postgresql — run postgresql-setup --initdb"
  HBA="$(sudo -u postgres psql -Atc 'SHOW hba_file')"
  if [[ -n "$HBA" && -f "$HBA" ]]; then
    log "Configuring Postgres password auth for localhost ($HBA)"
    cp -a "$HBA" "${HBA}.labvault.bak"
    sed -i -E 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+)\S+|\1md5|' "$HBA"
    sed -i -E 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+::1/128[[:space:]]+)\S+|\1md5|' "$HBA"
    grep -qE '^host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32' "$HBA" || \
      echo 'host all all 127.0.0.1/32 md5' >> "$HBA"
    systemctl reload postgresql 2>/dev/null || systemctl restart postgresql
  fi
  sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='labvault'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER labvault WITH PASSWORD 'labvault';"
  sudo -u postgres psql -c "ALTER USER labvault WITH PASSWORD 'labvault';" >/dev/null
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='labvault'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE labvault OWNER labvault;"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='labvault_metrics'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE labvault_metrics OWNER labvault;"
  use_pg=1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  SK="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  HOST="$(hostname -f 2>/dev/null || hostname)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ "$use_pg" -eq 1 ]]; then
    DB_URL='postgres://labvault:labvault@127.0.0.1:5432/labvault'
    METRICS_URL='postgres://labvault:labvault@127.0.0.1:5432/labvault_metrics'
  else
    DB_URL='sqlite:////var/lib/labvault/db.sqlite3'
    METRICS_URL='sqlite:////var/lib/labvault/np_timeseries.sqlite3'
  fi
  cat > "$ENV_FILE" <<EOF
DJANGO_SECRET_KEY=${SK}
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,${HOST},${IP}
DJANGO_DEBUG=false
DATABASE_URL=${DB_URL}
NP_TIMESERIES_DATABASE_URL=${METRICS_URL}
LABVAULT_CACHE_DIR=/var/lib/labvault/django_cache
LABVAULT_CSRF_TRUSTED_ORIGINS=$(default_tls_origins "$HOST" "$IP")
LABVAULT_USE_TLS=true
LABVAULT_TLS_PORT=$(default_tls_port)
LABVAULT_PUBLIC_ORIGIN=$(https_origin 127.0.0.1)
LABVAULT_TLS_DIR=/var/lib/labvault/tls
LABVAULT_TLS_CERT=/var/lib/labvault/tls/fullchain.pem
LABVAULT_TLS_KEY=/var/lib/labvault/tls/privkey.pem
LABVAULT_OPS_SOCK=/run/labvault/ops.sock
LABVAULT_OPS_ADAPTER=systemd
EOF
  chmod 600 "$ENV_FILE"
  chown root:labvault "$ENV_FILE"
fi
grep -q '^LABVAULT_PUBLIC_ORIGIN=' "$ENV_FILE" || echo "LABVAULT_PUBLIC_ORIGIN=$(https_origin 127.0.0.1)" >> "$ENV_FILE"
grep -q '^LABVAULT_USE_TLS=' "$ENV_FILE" || echo "LABVAULT_USE_TLS=true" >> "$ENV_FILE"

for unit in deploy/systemd/*.service; do
  sed "s|/opt/labvault/current|${INSTALL_ROOT}|g" "$unit" > "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload

# Restoring a live lab dataset turns Lab Pulse on with no extra UI steps.
if [[ -n "${LABVAULT_RESTORE_DATASET:-}" ]]; then
  [[ -f "$LABVAULT_RESTORE_DATASET" ]] || die "LABVAULT_RESTORE_DATASET not a file: $LABVAULT_RESTORE_DATASET"
  chmod a+r "$LABVAULT_RESTORE_DATASET" || true
  export LABVAULT_WORKER_MODE=live
  log "Restore dataset: $LABVAULT_RESTORE_DATASET"
fi
export LABVAULT_WORKER_MODE="${LABVAULT_WORKER_MODE:-idle}"
if grep -q '^LABVAULT_WORKER_MODE=' "$ENV_FILE" 2>/dev/null; then
  sed -i "s|^LABVAULT_WORKER_MODE=.*|LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}|" "$ENV_FILE"
else
  echo "LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}" >> "$ENV_FILE"
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
# Keep restore path visible to labvaultctl after sourcing env
if [[ -n "${LABVAULT_RESTORE_DATASET:-}" ]]; then
  export LABVAULT_RESTORE_DATASET
fi
chown -R labvault:labvault "$INSTALL_ROOT"
if [[ "${LABVAULT_BOOTSTRAP_RANDOM:-}" =~ ^(1|true|yes)$ ]]; then
  export LABVAULT_DEMO_DEFAULTS="${LABVAULT_DEMO_DEFAULTS:-0}"
else
  export LABVAULT_DEMO_DEFAULTS="${LABVAULT_DEMO_DEFAULTS:-1}"
fi
if grep -q '^LABVAULT_DEMO_DEFAULTS=' "$ENV_FILE"; then
  sed -i "s|^LABVAULT_DEMO_DEFAULTS=.*|LABVAULT_DEMO_DEFAULTS=${LABVAULT_DEMO_DEFAULTS}|" "$ENV_FILE"
else
  echo "LABVAULT_DEMO_DEFAULTS=${LABVAULT_DEMO_DEFAULTS}" >> "$ENV_FILE"
fi
export LABVAULT_TLS_PORT="${LABVAULT_TLS_PORT:-$(default_tls_port)}"
bash "$ROOT_SRC/deploy/scripts/ensure-labvault-tls.sh"
maybe_install_tls "$ROOT_SRC" "$INSTALL_ROOT"
./labvaultctl --adapter systemd --state-dir "$STATE" install
getent group labvault-ops >/dev/null || groupadd --system labvault-ops
usermod -aG labvault-ops labvault
mkdir -p /var/lib/labvault/cli-ssh && chown labvault:labvault /var/lib/labvault/cli-ssh && chmod 700 /var/lib/labvault/cli-ssh
systemctl enable --now labvault-opsd labvault-cli-ssh labvault-web labvault-refresh \
  labvault-heartbeat labvault-collector labvault-cli-worker
if [[ "${LABVAULT_WORKER_MODE}" == "live" ]]; then
  systemctl restart labvault-heartbeat labvault-collector
  log "Priming first live heartbeat + collector tick"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  sudo -E -u labvault env DJANGO_SETTINGS_MODULE=connect.settings \
    "$INSTALL_ROOT/.venv/bin/python" "$INSTALL_ROOT/manage.py" run_fleet_heartbeat --once
  sudo -E -u labvault env DJANGO_SETTINGS_MODULE=connect.settings \
    "$INSTALL_ROOT/.venv/bin/python" "$INSTALL_ROOT/manage.py" run_metric_collector --once
fi

if [[ ! "${LABVAULT_SKIP_VERIFY:-}" =~ ^(1|true|yes)$ ]]; then
  log "Running post_deploy_verify"
  [[ -f "$STATE/fleet-token" ]] || die "fleet token missing after bootstrap"
  TOKEN="$(awk -F= '/^token=/{print $2}' "$STATE/fleet-token" | tr -d '[:space:]')"
  [[ -n "$TOKEN" ]] || die "fleet token empty"
  BASE_URL="https://127.0.0.1:${LABVAULT_TLS_PORT:-$(default_tls_port)}" TOKEN="$TOKEN" ADAPTER=systemd \
    LABVAULT_STATE_DIR="$STATE" \
    bash "$INSTALL_ROOT/deploy/scripts/post_deploy_verify.sh" || \
    die "post_deploy_verify failed — see docs/admin/SERVICES.md"
fi

maybe_install_http80 "$ROOT_SRC" "$INSTALL_ROOT"
print_ready_banner "$STATE" "${LABVAULT_TLS_PORT:-$(default_tls_port)}" https
echo "ssh_cli=ssh -p ${LABVAULT_CLI_SSH_PORT:-2222} <staff-user>@127.0.0.1"
echo "pulse=LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}"
echo "restore=LABVAULT_RESTORE_DATASET=/path/labvault_export.json $0 <wheelhouse> [install_root]"
