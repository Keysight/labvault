# Monitoring

| Probe | Path | Use |
|-------|------|-----|
| Liveness | `/health/live` | Process up |
| Readiness | `/health/ready` | LB / k8s style ready |
| Fleet heartbeat | `/api/fleet/heartbeat.json` | Chassis freshness |
| Diagnostics | export / `live.json` | Support bundles |

Health endpoints intentionally omit inventory secrets. Alert on ready≠200 and on sustained halt_suspect counts when live.

## NP time-series database

Chassis / NP samples are stored in the **`np_timeseries`** database (`NP_TIMESERIES_DATABASE_URL`), not in the inventory OLTP DB. Compose oneshot uses the `metrics-db` Postgres service. See [install/CONFIGURATION.md](../install/CONFIGURATION.md) and [install/DOCKER.md](../install/DOCKER.md).

After deploy or upgrade:

```bash
# Compose
sudo docker compose -f deploy/compose/docker-compose.yml exec web \
  python manage.py migrate --database np_timeseries

# systemd / airgap
sudo -u labvault /opt/labvault/current/.venv/bin/python manage.py migrate --database np_timeseries
```

Purge samples older than 31 days (daily cron or timer):

```bash
python manage.py cleanup_np_timeseries
```

Verify: chassis detail → **NP Resource History** (first points after a few minutes of an online chassis), or `GET /keysight/api/chassis/<id>/np-timeseries/?range=24h`. Empty charts on a new idle install are expected until workers are live — [user/INSIGHTS.md](../user/INSIGHTS.md).
