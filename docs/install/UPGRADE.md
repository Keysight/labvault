# Upgrade

```bash
./labvaultctl --adapter systemd backup
# Place new release at install root / update symlink
pip install -r requirements.txt   # or rebuild compose images
./labvaultctl --adapter systemd install
# or: systemctl restart labvault-web labvault-opsd …
```

Compose:

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
# entrypoint migrates automatically
```
