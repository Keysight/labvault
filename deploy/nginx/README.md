# `deploy/nginx/`

Host nginx configs for the TLS edge in front of gunicorn (`127.0.0.1:8000`). The Compose
stack uses its own configs in [`../compose/`](../compose/README.md). Guide:
[docs/install/TLS.md](../../docs/install/TLS.md).

| File | Used by | Purpose |
|---|---|---|
| `labvault-https.conf.template` | `deploy/scripts/install-labvault-nginx.sh` → `/etc/nginx/conf.d/labvault-9443.conf` | Default edge: `:80` redirect and TLS on `@@LABVAULT_TLS_PORT@@` (9443), static/media aliases, proxy to `127.0.0.1:8000` |
| `labvault-edge.conf` | `deploy/scripts/install-labvault-http80.sh` | Bare IP/hostname edge on `:80` and `:443` → `127.0.0.1:8000` |
| `labvault-port80-to-8000.conf` | `install-labvault-http80.sh` fallback | `:80` redirect to `https://<host>:9443` only |
| `labvault-443.conf` | manual | Static example for TLS on 443 |
| `compose-proxy.conf` | manual | Full `nginx.conf` example for a Compose-style `:80`/`:443` proxy |
| `labvaultvm-upstream-18000.conf.template` | manual | Legacy template for a gunicorn upstream on `127.0.0.1:18000` with a `:8000` redirect listener; not used by the oneshots and conflicts with the default `:8000` gunicorn bind |

Placeholders `@@LABVAULT_SERVER_NAME@@`, `@@LABVAULT_TLS_PORT@@`, `@@LABVAULT_SSL_CERT@@`,
`@@LABVAULT_SSL_KEY@@` are filled by the install scripts.
