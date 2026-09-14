#!/usr/bin/env bash
# Drive appliance CLI acceptance for one deployment target.
# TARGET=compose|systemd|airgap|proxmox-restore
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TARGET="${TARGET:?set TARGET}"
OUT_DIR="${OUT_DIR:-$ROOT/docs/release/acceptance}"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$OUT_DIR/${TARGET}-${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1
echo "TARGET=$TARGET stamp=$STAMP"

case "$TARGET" in
  compose)
    sudo LABVAULT_BOOTSTRAP_RANDOM=1 "$ROOT/deploy/install/oneshot-compose.sh"
    ADAPTER=compose bash "$ROOT/deploy/scripts/post_deploy_verify.sh"
    ;;
  systemd)
    sudo LABVAULT_BOOTSTRAP_RANDOM=1 "$ROOT/deploy/install/oneshot-systemd.sh"
    ADAPTER=systemd bash "$ROOT/deploy/scripts/post_deploy_verify.sh"
    ;;
  airgap)
    : "${WHEELHOUSE:?set WHEELHOUSE}"
    sudo LABVAULT_BOOTSTRAP_RANDOM=1 "$ROOT/deploy/install/oneshot-airgap.sh" "$WHEELHOUSE"
    ADAPTER=systemd bash "$ROOT/deploy/scripts/post_deploy_verify.sh"
    ;;
  proxmox-restore)
    : "${LABVAULT_RESTORE_DATASET:?set LABVAULT_RESTORE_DATASET}"
    sudo LABVAULT_BOOTSTRAP_RANDOM=1 LABVAULT_WORKER_MODE=live \
      LABVAULT_RESTORE_DATASET="$LABVAULT_RESTORE_DATASET" \
      "$ROOT/deploy/install/oneshot-compose.sh"
    ADAPTER=compose LABVAULT_WORKER_MODE=live bash "$ROOT/deploy/scripts/post_deploy_verify.sh"
    ;;
  *)
    echo "unknown TARGET=$TARGET"; exit 2 ;;
esac

# SSH smoke if credentials exist
STATE="${LABVAULT_STATE_DIR:-/var/lib/labvault}"
if [[ -f "$STATE/bootstrap-credentials" ]]; then
  # shellcheck disable=SC1090
  source <(sed -n 's/^username=/LABVAULT_CLI_USER=/p;s/^password=/LABVAULT_CLI_PASSWORD=/p' "$STATE/bootstrap-credentials")
  LABVAULT_CLI_SSH_HOST=127.0.0.1 LABVAULT_CLI_SSH_PORT="${LABVAULT_CLI_SSH_PORT:-2222}" \
    python3 "$ROOT/deploy/scripts/cli_ssh_smoke.py"
fi
echo "ACCEPTANCE_LOG=$LOG"
