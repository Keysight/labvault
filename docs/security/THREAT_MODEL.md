# Threat model (summary)

| Asset | Threat | Control |
|-------|--------|---------|
| Host | docker.sock escape | Not mounted |
| App | Unauthenticated mutate | Login, staff, CSRF, fleet Bearer |
| Services | Arbitrary stop | opsd allowlist |
| Secrets | Git leak | gitignore, 0600 files |
| Supply chain | Offline pip | Wheelhouse + check_public_source |
| Dumped SKUs | Accidental reintroduce | CI `check_public_source.py` |

Report issues per [VULNERABILITY_REPORTING.md](VULNERABILITY_REPORTING.md).
