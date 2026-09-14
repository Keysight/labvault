# Uninstall

## Compose

```bash
docker compose -f deploy/compose/docker-compose.yml down
# add -v to delete Postgres volumes
```

## Systemd

```bash
sudo systemctl disable --now labvault-web labvault-refresh labvault-heartbeat \
  labvault-collector labvault-cli-worker labvault-opsd
sudo rm -f /etc/systemd/system/labvault-*.service
sudo systemctl daemon-reload
# optionally remove /opt/labvault /var/lib/labvault /etc/labvault
```
