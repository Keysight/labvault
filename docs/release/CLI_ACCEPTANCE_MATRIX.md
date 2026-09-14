# Appliance CLI acceptance matrix

| Deployment | Evidence required | Status |
|---|---|---|
| Clean Customer Compose | oneshot; generated login; web+SSH; worker restarts; restart-all; recreate fingerprint | harness ready — live run required on target |
| Native online systemd | oneshot; units active; broker mutations; web+SSH; reboot persistence | harness ready — live run required on target |
| Air-gapped systemd | wheel+RPM bundle; network disabled; lifecycle suite; reboot persistence | harness ready — live run required on target |
| Proxmox Compose + live restore | backup; restore excludes identity; live workers; web+SSH; heartbeat/collector recovery | harness ready — live run required on target |

## Automated evidence captured in-tree

- Ops broker unit tests: `python3 -m unittest connect.tests.test_opsd_broker`
- CLI parser/runner/catalog tests: `connect/tests/test_labvault_cli_appliance.py`
- SSH auth CIDR tests: `connect/tests/test_cli_ssh_auth.py`
- SSH smoke client: `deploy/scripts/cli_ssh_smoke.py`
- Post-deploy verify includes `cli-ssh` service + opsd socket + port 2222

## Notes

- Internal lab VMs are **not** targets for this customer SKU matrix.
- Dataset restore must not copy `/var/lib/labvault/cli-ssh`, `bootstrap-credentials`, or auth-throttle state.

## Honesty note

In-repo unit/broker tests pass (`connect.tests.test_opsd_broker`). Full four-row live acceptance must be executed with:

```bash
TARGET=compose ./deploy/scripts/cli_acceptance_run.sh
TARGET=systemd ./deploy/scripts/cli_acceptance_run.sh
TARGET=airgap WHEELHOUSE=/path/to/wheelhouse ./deploy/scripts/cli_acceptance_run.sh
TARGET=proxmox-restore LABVAULT_RESTORE_DATASET=/path/export.json ./deploy/scripts/cli_acceptance_run.sh
```

Do not mark a release READY until each row has a dated log under `docs/release/acceptance/`.
