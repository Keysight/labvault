# `opsd/`

LabVault operations broker: a small root process that owns `/run/labvault/ops.sock` and
performs allowlisted `status` / `start` / `stop` / `restart` actions on LabVault services
through `systemctl` or `docker compose`. No Docker socket is mounted into any container, and
no Django code runs here.

Protocol, capability matrix, and adapters:
[docs/development/subsystems/operations.md](../docs/development/subsystems/operations.md#opsd--operations-broker).
Operator view: [docs/cli/SERVICE_CONTROL.md](../docs/cli/SERVICE_CONTROL.md).

| File | Purpose |
|---|---|
| `opsd.py` | Socket server (`--sock`), request validation (`handle()`), systemd and Compose adapters, Compose tree ownership checks |
| `service_catalog.py` | Canonical logical service names, unit/service mapping, per-action capability flags, `restart all` order, name aliases. Stdlib only; re-exported to Django by `connect/labvault_cli/service_catalog.py` |

Run by `deploy/systemd/labvault-opsd.service` (all deploy modes; the Compose oneshot adds a
drop-in setting `LABVAULT_OPS_ADAPTER=compose`). Tests: `connect/tests/test_opsd_broker.py`.

```bash
# Local smoke without root (uses a temp socket; systemctl calls will just fail)
python3 opsd/opsd.py --sock /tmp/ops.sock &
printf '{"action":"list"}\n' | python3 -c 'import socket,sys;s=socket.socket(socket.AF_UNIX);s.connect("/tmp/ops.sock");s.sendall(sys.stdin.buffer.read());print(s.recv(65536).decode())'
```
