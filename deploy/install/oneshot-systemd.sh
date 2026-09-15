#!/usr/bin/env bash
# LabVault customer SKU — one-shot bare-metal / systemd install.
# Usage: sudo ./deploy/install/oneshot-systemd.sh [/opt/labvault/current]
set -euo pipefail

ROOT_SRC="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_ROOT="${1:-/opt/labvault/current}"
STATE="${LABVAULT_STATE_DIR:-/var/lib/labvault}"
ENV_FILE="${LABVAULT_ENV_FILE:-/etc/labvault/labvault.env}"

log() { printf '+ %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# shellcheck disable=SC1091
source "$ROOT_SRC/deploy/install/lib-ready.sh"

[[ "$(id -u)" -eq 0 ]] || die "run as root"

install_host_deps() {
  if command -v dnf >/dev/null; then
    log "Installing Rocky/RHEL build deps"
    # Django 5.2 needs PostgreSQL 14+. Rocky 9's default module is 13.
    dnf module reset -y postgresql >/dev/null 2>&1 || true
    dnf module enable -y postgresql:15 >/dev/null 2>&1 || \
      dnf module enable -y postgresql:16 >/dev/null 2>&1 || true
    dnf install -y python3.11 python3.11-devel python3.11-pip \
      python3 python3-pip python3-devel gcc git nginx openssl \
      openldap-devel openssl-devel cyrus-sasl-devel libpq-devel \
      postgresql-server postgresql-contrib || \
      dnf install -y python3.11 python3.11-devel python3.11-pip \
        python3 python3-pip python3-devel gcc git nginx openssl \
        openldap-devel openssl-devel cyrus-sasl-devel
  elif command -v apt-get >/dev/null; then
    log "Installing Debian/Ubuntu build deps"
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      python3 python3-venv python3-pip python3-dev build-essential git nginx openssl \
      libldap2-dev libsasl2-dev libssl-dev libpq-dev \
      postgresql postgresql-contrib
  else
    die "unsupported package manager — install python3, venv, openldap devel, postgres manually"
  fi
}

install_host_deps

id labvault >/dev/null 2>&1 || useradd --system --home /var/lib/labvault --shell /sbin/nologin labvault
getent group labvault-ops >/dev/null || groupadd --system labvault-ops
usermod -aG labvault-ops labvault
mkdir -p "$INSTALL_ROOT" "$STATE" /etc/labvault /run/labvault /var/lib/labvault/media "$STATE/django_cache"
chown -R labvault:labvault "$STATE" /var/lib/labvault
chown root:labvault-ops /run/labvault
chmod 775 /run/labvault

if [[ "$ROOT_SRC" != "$INSTALL_ROOT" ]]; then
  log "Syncing tree → $INSTALL_ROOT"
  mkdir -p "$INSTALL_ROOT"
  rsync -a --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
    --exclude 'db.sqlite3' --exclude 'np_timeseries.sqlite3' --exclude 'staticfiles' \
    "$ROOT_SRC"/ "$INSTALL_ROOT"/
fi
cd "$INSTALL_ROOT"

if [[ ! -d .venv ]]; then
  log "Creating venv"
  PY=python3
  if command -v python3.11 >/dev/null; then
    PY=python3.11
  elif command -v python3.12 >/dev/null; then
    PY=python3.12
  fi
  "$PY" -c 'import sys; assert sys.version_info >= (3, 10), sys.version'
  log "venv interpreter $($PY --version)"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt gunicorn

if [[ ! -f "$ENV_FILE" ]]; then
  log "Writing $ENV_FILE"
  SK="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  HOST="$(hostname -f 2>/dev/null || hostname)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  cat > "$ENV_FILE" <<EOF
DJANGO_SECRET_KEY=${SK}
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,${HOST},${IP}
DJANGO_DEBUG=false
DATABASE_URL=postgres://labvault:labvault@127.0.0.1:5432/labvault
NP_TIMESERIES_DATABASE_URL=postgres://labvault:labvault@127.0.0.1:5432/labvault_metrics
LABVAULT_CACHE_DIR=/var/lib/labvault/django_cache
LABVAULT_HEARTBEAT_LOCK=/var/lib/labvault/django_cache/fleet_heartbeat.lock
LABVAULT_HEARTBEAT_INTERVAL_SECONDS=120
LABVAULT_CSRF_TRUSTED_ORIGINS=$(default_tls_origins "$HOST" "$IP")
LABVAULT_USE_TLS=true
LABVAULT_TLS_PORT=$(default_tls_port)
LABVAULT_PUBLIC_ORIGIN=$(https_origin 127.0.0.1)
LABVAULT_TLS_DIR=/var/lib/labvault/tls
LABVAULT_TLS_CERT=/var/lib/labvault/tls/fullchain.pem
LABVAULT_TLS_KEY=/var/lib/labvault/tls/privkey.pem
LABVAULT_OPS_SOCK=/run/labvault/ops.sock
EOF
  chmod 600 "$ENV_FILE"
  chown root:labvault "$ENV_FILE"
fi

# Ensure Pulse defaults on older env files.
grep -q '^LABVAULT_CACHE_DIR=' "$ENV_FILE" || \
  echo "LABVAULT_CACHE_DIR=/var/lib/labvault/django_cache" >> "$ENV_FILE"
grep -q '^LABVAULT_HEARTBEAT_LOCK=' "$ENV_FILE" || \
  echo "LABVAULT_HEARTBEAT_LOCK=/var/lib/labvault/django_cache/fleet_heartbeat.lock" >> "$ENV_FILE"
grep -q '^LABVAULT_HEARTBEAT_INTERVAL_SECONDS=' "$ENV_FILE" || \
  echo "LABVAULT_HEARTBEAT_INTERVAL_SECONDS=120" >> "$ENV_FILE"
mkdir -p /var/lib/labvault/django_cache
chown -R labvault:labvault /var/lib/labvault/django_cache

# Restoring a live lab dataset turns Lab Pulse on with no extra UI steps.
if [[ -n "${LABVAULT_RESTORE_DATASET:-}" ]]; then
  [[ -f "$LABVAULT_RESTORE_DATASET" ]] || die "LABVAULT_RESTORE_DATASET not a file: $LABVAULT_RESTORE_DATASET"
  chmod a+r "$LABVAULT_RESTORE_DATASET" || true
  export LABVAULT_WORKER_MODE=live
  log "Restore dataset: $LABVAULT_RESTORE_DATASET"
fi
export LABVAULT_WORKER_MODE="${LABVAULT_WORKER_MODE:-idle}"
if grep -q '^LABVAULT_WORKER_MODE=' "$ENV_FILE"; then
  sed -i "s|^LABVAULT_WORKER_MODE=.*|LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}|" "$ENV_FILE"
else
  echo "LABVAULT_WORKER_MODE=${LABVAULT_WORKER_MODE}" >> "$ENV_FILE"
fi

# Ensure Postgres DBs exist when local postgresql is available.
# An empty /var/lib/pgsql/data directory is not a cluster (EL9 check-db-dir).
pg_cluster_ready() {
  local d
  for d in /var/lib/pgsql/data /var/lib/pgsql/15/data /var/lib/pgsql/14/data /var/lib/pgsql/13/data; do
    [[ -f "$d/PG_VERSION" ]] && return 0
  done
  return 1
}
if [[ -f /var/lib/pgsql/data/PG_VERSION ]] && [[ "$(tr -d '[:space:]' < /var/lib/pgsql/data/PG_VERSION)" -lt 14 ]]; then
  log "Replacing PostgreSQL $(tr -d '[:space:]' < /var/lib/pgsql/data/PG_VERSION) cluster (Django 5.2 needs 14+)"
  systemctl stop postgresql 2>/dev/null || true
  rm -rf /var/lib/pgsql/data
fi
if command -v postgresql-setup >/dev/null 2>&1 && ! pg_cluster_ready; then
  log "Initializing PostgreSQL cluster"
  postgresql-setup --initdb
fi
PG_UNIT=""
for u in postgresql postgresql-15 postgresql-14 postgresql-13; do
  if [[ -f "/usr/lib/systemd/system/${u}.service" || -f "/lib/systemd/system/${u}.service" ]]; then
    PG_UNIT="$u"
    break
  fi
done
if [[ -n "$PG_UNIT" ]]; then
  log "Starting $PG_UNIT"
  systemctl enable --now "$PG_UNIT" || die "failed to start $PG_UNIT — see journalctl -u $PG_UNIT"
else
  log "no postgresql unit found; oneshot env still points at 127.0.0.1:5432"
fi
if command -v sudo >/dev/null && id postgres >/dev/null 2>&1; then
  # Rocky/RHEL defaults to Ident for 127.0.0.1 — Django needs password auth.
  HBA="$(sudo -u postgres psql -Atc 'SHOW hba_file')"
  if [[ -n "$HBA" && -f "$HBA" ]]; then
    log "Configuring Postgres password auth for localhost ($HBA)"
    cp -a "$HBA" "${HBA}.labvault.bak"
    sed -i -E 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+)\S+|\1md5|' "$HBA"
    sed -i -E 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+::1/128[[:space:]]+)\S+|\1md5|' "$HBA"
    grep -qE '^host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32' "$HBA" || \
      echo 'host all all 127.0.0.1/32 md5' >> "$HBA"
    systemctl reload "${PG_UNIT:-postgresql}" 2>/dev/null || systemctl restart "${PG_UNIT:-postgresql}"
  fi
  sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='labvault'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER labvault WITH PASSWORD 'labvault';"
  sudo -u postgres psql -c "ALTER USER labvault WITH PASSWORD 'labvault';" >/dev/null
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='labvault'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE labvault OWNER labvault;"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='labvault_metrics'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE labvault_metrics OWNER labvault;"
fi

# Install units (rewrite WorkingDirectory if needed).
# opsd-compose is a compose-only stub and conflicts with Alias= on labvault-opsd.
for unit in deploy/systemd/*.service; do
  base="$(basename "$unit")"
  if [[ "$base" == "labvault-opsd-compose.service" ]]; then
    rm -f "/etc/systemd/system/${base}"
    continue
  fi
  sed "s|/opt/labvault/current|${INSTALL_ROOT}|g" "$unit" > "/etc/systemd/system/${base}"
  log "installed /etc/systemd/system/${base}"
done
systemctl daemon-reload

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
export DJANGO_SETTINGS_MODULE=connect.settings

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
# Export restore path for labvaultctl → bootstrap_labvault --restore
if [[ -n "${LABVAULT_RESTORE_DATASET:-}" ]]; then
  export LABVAULT_RESTORE_DATASET
fi
export LABVAULT_TLS_PORT="${LABVAULT_TLS_PORT:-$(default_tls_port)}"
bash "$ROOT_SRC/deploy/scripts/ensure-labvault-tls.sh"
maybe_install_tls "$ROOT_SRC" "$INSTALL_ROOT"
log "labvaultctl install --adapter systemd"
./labvaultctl --adapter systemd --state-dir "$STATE" install

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
