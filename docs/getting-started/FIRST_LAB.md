# First lab walkthrough

1. **Inventory** — Add devices and/or discover Keysight chassis under `/keysight/`.
2. **Topology** — Create a lab topology at `/lab-topology/`; place nodes; save.
3. **Fabric** (optional) — Define port fabric; take an OCS snapshot if an optical switch is attached.
4. **Reservation** — Reserve chassis for a window so ownership/conflict APIs stay accurate.
5. **Fleet** — After rotating the token ([FIRST_LOGIN](FIRST_LOGIN.md)), call `/api/fleet/health.json` and `inventory.json` with `Authorization: Bearer …`. The install default is `labvault-default-api-token` (name `demo-api`) until you revoke it.
6. **CLI** — `device list`, `chassis list`, `topo list`, `diag cheap`.
7. **Live mode / Lab Pulse** — Empty installs stay idle on purpose. Restoring a dataset with `LABVAULT_RESTORE_DATASET` sets `LABVAULT_WORKER_MODE=live` and collector/heartbeat to live in one shot (no extra UI). Otherwise enable live mode via runtime settings when you are ready to poll hardware.
