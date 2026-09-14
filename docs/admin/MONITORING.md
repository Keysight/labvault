# Monitoring

| Probe | Path | Use |
|-------|------|-----|
| Liveness | `/health/live` | Process up |
| Readiness | `/health/ready` | LB / k8s style ready |
| Fleet heartbeat | `/api/fleet/heartbeat.json` | Chassis freshness |
| Diagnostics | export / `live.json` | Support bundles |

Health endpoints intentionally omit inventory secrets. Alert on ready≠200 and on sustained halt_suspect counts when live.
