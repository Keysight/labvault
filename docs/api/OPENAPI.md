# Fleet APIs & OpenAPI

| Entry | Path |
|-------|------|
| Index | `/api/fleet/` |
| OpenAPI JSON | `/api/fleet/openapi.json` |
| Swagger UI | `/api/docs/` |

## Endpoint groups (representative)

| Persona | Examples |
|---------|----------|
| On-caller | `health.json`, `heartbeat.json`, `heartbeat/stream`, chassis health/recover |
| Test user | `ports/telemetry.json`, `ports/preflight.json` |
| EM / director | `sla.json`, `summary.json`, `metrics/transmission.json` |
| Lab admin | `inventory.json`, `reservations`, team-tags |
| Infra | `ownership.json`, `conflicts.json`, OCS crossconnect helpers |

Auth: [API_AUTH.md](API_AUTH.md). Workers idle ⇒ heartbeat/health reflect idle mode until live collection is enabled.

Examples use RFC 5737 documentation addresses (`192.0.2.0/24`), not a live lab.
