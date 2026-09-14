# Third-party notices

This product includes open-source components listed in `requirements.txt` and
pinned in `requirements.lock`. Generate a CycloneDX SBOM at release time with
`bash tools/generate_sbom.sh` and attach it to the GitHub release.

Primary runtime dependencies (license as commonly published by the upstream
project; confirm against the resolved lockfile before public launch):

| Package | Typical license |
|---------|-----------------|
| Django | BSD-3-Clause |
| django-sslserver | MIT |
| jsonrpclib-pelix | Apache-2.0 |
| requests | Apache-2.0 |
| paramiko | LGPL-2.1 |
| psutil | BSD-3-Clause |
| PyYAML | MIT |
| python-dotenv | BSD-3-Clause |
| gunicorn | MIT |
| dj-database-url | BSD-3-Clause |
| psycopg2-binary | LGPL-3.0 |
| redfish | BSD-3-Clause |
| django-auth-ldap | BSD-2-Clause |
| asyncssh | EPL-2.0 |

This table is informational. The SBOM is authoritative for a given release.
