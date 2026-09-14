# Shared oneshot helpers. Source after die()/log() are defined.
# shellcheck shell=bash

print_ready_banner() {
  local state="$1"
  local port="${2:-8000}"
  [[ -f "$state/bootstrap-credentials" ]] || die "missing credential file $state/bootstrap-credentials"
  [[ -f "$state/fleet-token" ]] || die "missing $state/fleet-token"
  echo "READY"
  echo "url=http://127.0.0.1:${port}/login/"
  echo "health=http://127.0.0.1:${port}/health/ready"
  echo "cli=http://127.0.0.1:${port}/cli/  (staff login required)"
  echo "credential_file=$state/bootstrap-credentials"
  echo "read_login=sudo cat $state/bootstrap-credentials"
  echo "read_token=sudo cat $state/fleet-token"
  echo "note=Read the credential file. Demo logins are not the default; they require LABVAULT_DEMO_DEFAULTS=1."
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
