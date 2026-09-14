# Shared oneshot helpers. Source after die()/log() are defined.
# shellcheck shell=bash

default_tls_port() {
  echo "${LABVAULT_TLS_PORT:-9443}"
}

https_origin() {
  local host="$1"
  local port="${2:-$(default_tls_port)}"
  if [[ "$port" == "443" ]]; then
    echo "https://${host}"
  else
    echo "https://${host}:${port}"
  fi
}

default_tls_origins() {
  local host="$1"
  local ip="$2"
  local port
  port="$(default_tls_port)"
  local out
  out="$(https_origin 127.0.0.1 "$port"),$(https_origin localhost "$port")"
  [[ -n "$host" ]] && out="${out},$(https_origin "$host" "$port")"
  [[ -n "$ip" ]] && out="${out},$(https_origin "$ip" "$port")"
  echo "$out"
}

print_ready_banner() {
  local state="$1"
  local port="${2:-$(default_tls_port)}"
  local scheme="${3:-https}"
  [[ -f "$state/bootstrap-credentials" ]] || die "missing credential file $state/bootstrap-credentials"
  [[ -f "$state/fleet-token" ]] || die "missing $state/fleet-token"
  echo "READY"
  echo "url=${scheme}://127.0.0.1:${port}/login/"
  echo "health=${scheme}://127.0.0.1:${port}/health/ready"
  echo "cli=${scheme}://127.0.0.1:${port}/cli/  (staff login required)"
  echo "credential_file=$state/bootstrap-credentials"
  echo "read_login=sudo cat $state/bootstrap-credentials"
  echo "read_token=sudo cat $state/fleet-token"
  if [[ "$scheme" == "https" ]]; then
    echo "tls_note=First-run uses a generated cert. Browsers warn until you install LABVAULT_TLS_CERT. curl needs -k."
  fi
  echo "note=Read the credential file. Demo logins are not the default; they require LABVAULT_DEMO_DEFAULTS=1."
}

maybe_install_tls() {
  if [[ "${LABVAULT_SKIP_TLS:-}" =~ ^(1|true|yes)$ ]]; then
    log "skipping TLS edge (LABVAULT_SKIP_TLS)"
    return 0
  fi
  local root="${1:-}"
  local install_root="${2:-}"
  local ensure=""
  local install_nginx=""
  for cand in "${root}/deploy/scripts/ensure-labvault-tls.sh" \
              "${install_root}/deploy/scripts/ensure-labvault-tls.sh"; do
    if [[ -n "$cand" && -x "$cand" ]]; then
      ensure="$cand"
      break
    fi
  done
  [[ -n "$ensure" ]] || return 0
  bash "$ensure"
  for cand in "${root}/deploy/scripts/install-labvault-nginx.sh" \
              "${install_root}/deploy/scripts/install-labvault-nginx.sh"; do
    if [[ -n "$cand" && -x "$cand" ]]; then
      install_nginx="$cand"
      break
    fi
  done
  if [[ -z "$install_nginx" ]]; then
    return 0
  fi
  if ! command -v nginx >/dev/null; then
    if command -v dnf >/dev/null; then
      dnf install -y nginx || true
    elif command -v apt-get >/dev/null; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y nginx || true
    fi
  fi
  if ! command -v nginx >/dev/null; then
    log "nginx not installed — compose TLS edge still applies; host :$(default_tls_port) skipped"
    return 0
  fi
  LABVAULT_ROOT="${LABVAULT_ROOT:-${install_root:-$root}}" \
    LABVAULT_PUBLIC_HOSTNAME="${LABVAULT_PUBLIC_HOSTNAME:-$(hostname -f 2>/dev/null || hostname)}" \
    bash "$install_nginx" || log "host nginx TLS install skipped (compose edge still listens on :$(default_tls_port))"
}

maybe_install_http80() {
  if [[ "${LABVAULT_SKIP_HTTP80:-}" =~ ^(1|true|yes)$ ]]; then
    log "skipping host nginx :80 (LABVAULT_SKIP_HTTP80)"
    return 0
  fi
  local script=""
  local root="${1:-}"
  local install_root="${2:-}"
  for cand in "${root}/deploy/scripts/install-labvault-http80.sh" \
              "${install_root}/deploy/scripts/install-labvault-http80.sh"; do
    if [[ -n "$cand" && -x "$cand" ]]; then
      script="$cand"
      break
    fi
  done
  if [[ -z "$script" ]]; then
    return 0
  fi
  LABVAULT_ROOT="${LABVAULT_ROOT:-${install_root:-$root}}" bash "$script"
}

copy_bootstrap_artifacts() {
  local src_dir="$1"
  local dest="$2"
  mkdir -p "$dest"
  if [[ -f "$src_dir/bootstrap-credentials" ]]; then
    cp -f "$src_dir/bootstrap-credentials" "$dest/bootstrap-credentials"
  fi
  if [[ -f "$src_dir/fleet-token" ]]; then
    cp -f "$src_dir/fleet-token" "$dest/fleet-token"
  fi
  chmod 600 "$dest/bootstrap-credentials" "$dest/fleet-token"
}
