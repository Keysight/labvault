# Soak + restore report (fill on staging)

## Environment

- Adapter: compose / systemd
- Host OS:
- Start:
- End:

## 24h empty-lab soak

- [ ] No 5xx on `/health/live` or `/health/ready`
- [ ] No unit restart loops
- [ ] Workers remain `idle` with zero device traffic
- [ ] Logs bounded

Notes:

## Restore drill

```bash
./labvaultctl backup
./labvaultctl restore --drill <backup-dir>
```

- [ ] App + metrics dumps restored
- [ ] `/health/ready` 200
- [ ] Login with credential file

Backup path:
Result:
