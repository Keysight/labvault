# Automation guidance

| Goal | Mechanism |
|------|-----------|
| Unattended read/write fleet ops | Bearer `/api/fleet/*` |
| Interactive operator tasks | `/cli/` |
| Service lifecycle | opsd allowlist or `systemctl` on host |
| Install / backup | `labvaultctl` / oneshot scripts |

Do not scrape HTML. Do not mount docker.sock into LabVault containers.
