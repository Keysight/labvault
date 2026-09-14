#!/usr/bin/env bash
# TLS is the oneshot default (HTTPS :9443 → gunicorn :8000).
# Prefer oneshot-systemd / oneshot-compose; this script only prints the path.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "Use: sudo $ROOT/deploy/install/oneshot-compose.sh"
echo "  or: sudo $ROOT/deploy/install/oneshot-systemd.sh"
echo "Customer URL: https://<host>:9443/  (gunicorn stays on 127.0.0.1:8000)"
echo "Replace the generated cert with LABVAULT_TLS_CERT / LABVAULT_TLS_KEY."
exit 0
