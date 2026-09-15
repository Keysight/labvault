# TLS

HTTPS on **:9443** is the customer default. That port is unused elsewhere in this SKU (not 443, 8000, 18000, or 18001).

Gunicorn stays on **127.0.0.1:8000**. Nginx terminates TLS and proxies to it.

```bash
# Generated on first oneshot if you have not installed operator certs:
#   /var/lib/labvault/tls/fullchain.pem
#   /var/lib/labvault/tls/privkey.pem

sudo LABVAULT_ROOT=/opt/labvault/current \
  ./deploy/scripts/ensure-labvault-tls.sh
sudo LABVAULT_ROOT=/opt/labvault/current \
  ./deploy/scripts/install-labvault-nginx.sh \
  --hostname labvault.example \
  --cert /var/lib/labvault/tls/fullchain.pem \
  --key /var/lib/labvault/tls/privkey.pem
```

| File | Role |
|------|------|
| `deploy/scripts/ensure-labvault-tls.sh` | Create lab self-signed cert if missing |
| `deploy/compose/nginx-tls.conf` | Compose nginx → `web:8000`, listen **9443** |
| `deploy/nginx/labvault-https.conf.template` | Host nginx → **8000**, listen `LABVAULT_TLS_PORT` |
| `deploy/compose/nginx-http.conf` | HTTP-only Compose edge (opt-out / debug) |

Defaults:

| Variable | Default |
|----------|---------|
| `LABVAULT_USE_TLS` | `true` |
| `LABVAULT_TLS_PORT` | `9443` |
| `LABVAULT_PUBLIC_ORIGIN` | `https://127.0.0.1:9443` |

On Rocky/RHEL with SELinux Enforcing, host nginx needs **9443/tcp** labeled `http_port_t` and `httpd_can_network_connect` so it can bind TLS and proxy to loopback `:8000`. `install-labvault-nginx.sh` does both.

Set `LABVAULT_USE_TLS=false` only for local HTTP debugging. Replace the generated cert on shared hosts. `curl` against the lab cert needs `-k`:

```bash
curl -kfsS https://127.0.0.1:9443/health/ready
```
