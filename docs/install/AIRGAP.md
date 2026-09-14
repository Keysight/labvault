# Air-gapped implementation

## Builder (networked, same OS/arch as target)

```bash
./deploy/install/build-wheelhouse.sh ./dist/wheelhouse
tar czf labvault-public-src.tgz --exclude .venv --exclude .git --exclude dist .
# Copy tarball + dist/wheelhouse/ offline
```

`PLATFORM.txt` records Python/machine — mismatch = broken native wheels.

## Target (air-gapped)

1. Install OS packages from local mirror: python3, gcc, openldap-devel (or Debian equivalents), postgresql as needed.
2. Extract source.
3. Run:

```bash
sudo LABVAULT_RESTORE_DATASET=/path/labvault_export.json \
  ./deploy/install/oneshot-airgap.sh /media/wheelhouse /opt/labvault/current
```

Default airgap env may use SQLite under `/var/lib/labvault/` so first boot works without Postgres. **Edit `/etc/labvault/labvault.env` to Postgres and re-migrate before production.**

Restore uses a `labvault-full-export` v1 JSON dataset (same as Compose/systemd), not a volume dump.
## Acceptance

```bash
curl -kfsS https://127.0.0.1:9443/health/ready
./labvaultctl --adapter systemd backup
./labvaultctl --adapter systemd restore --drill "$(ls -d /var/lib/labvault/backups/labvault-* | tail -1)"
```
