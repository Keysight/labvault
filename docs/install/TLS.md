# TLS

App listens on `127.0.0.1:8000`. Terminate TLS at nginx (or a load balancer).

```bash
sudo LABVAULT_ROOT=/opt/labvault/current \
  ./deploy/scripts/install-labvault-nginx.sh \
  --hostname labvault.example \
  --cert /etc/labvault/tls/fullchain.pem \
  --key /etc/labvault/tls/privkey.pem
```

| File | Role |
|------|------|
| `deploy/nginx/labvault-https.conf.template` | Customer host nginx → **8000** |
| `deploy/nginx/labvault-443.conf` | Example 443 vhost |
| `deploy/nginx/compose-proxy.conf` | Optional Compose-in-container proxy (`web:8000`) |
| `deploy/compose/nginx-http.conf` | HTTP-only Compose edge (default port 8080) |

Then set `LABVAULT_USE_TLS=true` and HTTPS CSRF origins; restart `labvault-web`.
