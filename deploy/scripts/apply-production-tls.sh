#!/usr/bin/env bash
# Apply TLS via nginx in front of LabVault oneshot (gunicorn :8000).
# Prefer oneshot-systemd + install-labvault-nginx.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "Use: sudo $ROOT/deploy/install/oneshot-systemd.sh"
echo "Then: sudo LABVAULT_ROOT=${LABVAULT_ROOT:-/opt/labvault/current} \\"
echo "         $ROOT/deploy/scripts/install-labvault-nginx.sh --hostname <fqdn>"
echo "Upstream is 127.0.0.1:8000 (not :18000)."
exit 0
