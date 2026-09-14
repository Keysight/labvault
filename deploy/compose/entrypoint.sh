#!/bin/sh
# Root: make named volumes writable, then drop to uid 10001.
# Non-root: continue (image USER or already dropped).
set -eu
mkdir -p /var/lib/labvault/uhd-locks /var/lib/labvault/media /app/var/django_cache
# Root-owned /tmp locks from `docker compose exec` (uid 0) break the labvault
# heartbeat worker — remove so the worker can recreate them.
rm -f /tmp/labvault_fleet_heartbeat.lock /tmp/labvault_heartbeat.lock 2>/dev/null || true
if [ "$(id -u)" = "0" ]; then
  chown -R labvault:labvault /var/lib/labvault /app/var 2>/dev/null || true
  chmod -R u+rwX,g+rwX /app/var/django_cache 2>/dev/null || true
fi
python manage.py migrate --noinput
python manage.py migrate --database np_timeseries --noinput
python manage.py collectstatic --noinput
if [ "$(id -u)" = "0" ]; then
  chown -R labvault:labvault /var/lib/labvault /app/var 2>/dev/null || true
fi
if [ "$(id -u)" = "0" ] && command -v setpriv >/dev/null 2>&1; then
  # Keep Docker group_add (e.g. host labvault-ops GID) so /run/labvault/ops.sock
  # remains accessible after dropping root. --init-groups would wipe those.
  exec setpriv --reuid=labvault --regid=labvault --keep-groups -- "$@"
fi
exec "$@"
