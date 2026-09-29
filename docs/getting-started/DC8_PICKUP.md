# DC example backup — what to change before import

Use this when you want Keysight chassis, Arista spines, one OCS, one lab topology, and Lab Pulse **live** from a single file. This is **not** a `labvaultctl` appliance backup and **not** a full live-lab export.

| File | Role |
|------|------|
| [resources/examples/dc8-pickup-export.json](../../resources/examples/dc8-pickup-export.json) | Shareable `labvault-full-export` v1 (TEST-NET placeholders) |
| [resources/examples/empty-labvault-export.json](../../resources/examples/empty-labvault-export.json) | Empty schema only (no inventory) |

Do **not** commit or share a live-IP copy. Changing an address later in the UI does **not** rewrite topology `device_ip` — edit the JSON first, then import.

## What ships in the file

| Section | Count | Placeholder |
|---------|------:|-------------|
| Arista spines (`devices`) | 4 | `192.0.2.20`–`.23` · `admin` / `changeme` |
| OCS S-320 (`devices`) | 1 | `192.0.2.10` · `user` / `changeme` |
| AresONE chassis (`keysight_chassis` **and** `devices`) | 8 | `192.0.2.31`–`.38` · `admin` / `admin` |
| Lab topology | 1 | name `DC Lab Topology` — same IPs on each node |
| Reservations, audit, changelog, other vendors | 0 | leave empty |

RFC 5737 `192.0.2.0/24` is documentation-only. Nothing in this file will reach a real lab until you replace those addresses.

Two server nodes (`server01`, `server02`) have **no** management IP. Leave them empty or delete those nodes; they are canvas placeholders.

## Fields you must edit

Replace **every** occurrence of a placeholder IP, not only the inventory row. Import binds chassis and devices by management IP.

### 1. Devices (`devices[]`)

For each of the 4 spines, the OCS, and the 8 AresONE rows:

| Field | Change to |
|-------|-----------|
| `ip_address` | Management IPv4 (or hostname) you can reach from the LabVault host |
| `username` / `password` | Real login for that box |
| `transport` | `auto` is fine; set `https` / `http` / `ssh` if you know it |
| `api_port` | Leave `null` unless the API is off the vendor default |
| `preferred_ip_version` | `ipv4` unless you also set `mgmt_ipv6` |
| `mgmt_ipv6` | Real IPv6 only if you use it; otherwise leave `""` |
| `enable_password` | Enable secret if the switch needs it |
| `arista_password` / `sonic_password` | Only if dual-OS creds differ from `password` |

Do not invent a new row. Keep the 13 device objects and change the fields above.

### 2. Chassis (`keysight_chassis[]`)

For each of the 8 AresONE rows, set the **same** `ip_address` / `username` / `password` as the matching `devices[]` row. Also set `transport` is not on chassis — IxOS uses `username` / `password` and `ip_address`.

| Field | Change to |
|-------|-----------|
| `ip_address` | Same value as that chassis in `devices[]` |
| `username` / `password` | IxOS / AresONE login |
| `preferred_ip_version` | `ipv4` unless `mgmt_ipv6` is set |
| `chassis_type` | Leave `aresone` unless the box is a different Keysight type |

### 3. Topology nodes (`lab_topologies[0].nodes[]`)

Every chassis, switch, and OCS node has the IP in **four** places. They must all match the inventory address.

| Field | Where |
|-------|--------|
| `device_ip` | Top-level on the node |
| `mgmt_ipv4` | Top-level on the node |
| `mgmt_display` | Top-level on the node |
| `extra.device_ip` | Inside `extra` (same IPv4) |

Optional: `extra.mgmt_ipv4`, `extra.hostname`. Leave port lists, fabric maps, and `ocs_triplet_map` alone.

| `node_key` | Placeholder IP | Must match |
|------------|----------------|------------|
| `aresone01` … `aresone08` | `192.0.2.31` … `.38` | `keysight_chassis` + AresONE `devices` |
| `arista1` … `arista4` | `192.0.2.20` … `.23` | spine `devices` |
| `ocs` | `192.0.2.10` | OCS `devices` |
| `server01` / `server02` | empty | optional |

Search-and-replace each placeholder IP with the real one (13 replacements). That keeps inventory and topology aligned.

### 4. Leave alone

- `"format": "labvault-full-export"` and `"version": 1`
- Empty arrays (`keysight_reservations`, `audit_logs`, …)
- the empty compatibility object in the example (leave that key as shipped)
- Node `ports`, `extra.ports`, `extra.port_details`, fabric / OCS maps
- Topology name unless you want a different label in the UI
- Hostnames are labels only; changing them is optional

## How to import

### Already installed

1. Copy the edited JSON onto the LabVault host (not into git). The `labvault` service user must be able to **read** it (`chmod 644` or `chown labvault`). A `0600` root-owned copy will fail the CLI restore.
2. Sign in as staff — credentials are in [FIRST_LOGIN](FIRST_LOGIN.md).
3. **DATA → Import → Import full LabVault dataset** and choose the file.
4. Wait for the success banner (`pulse=live`).

CLI equivalent (after a systemd/airgap oneshot). Load `/etc/labvault/labvault.env` as **root**, then run the commands as `labvault` so Django sees Postgres/cache settings:

```bash
set -a
source /etc/labvault/labvault.env
set +a
sudo chmod 644 /path/to/dc8-pickup-export.json
sudo -E -u labvault env DJANGO_SETTINGS_MODULE=connect.settings \
  /opt/labvault/current/.venv/bin/python /opt/labvault/current/manage.py \
  bootstrap_labvault --live --restore /path/to/dc8-pickup-export.json
sudo -E -u labvault env DJANGO_SETTINGS_MODULE=connect.settings \
  /opt/labvault/current/.venv/bin/python /opt/labvault/current/manage.py \
  run_fleet_heartbeat --once
sudo -E -u labvault env DJANGO_SETTINGS_MODULE=connect.settings \
  /opt/labvault/current/.venv/bin/python /opt/labvault/current/manage.py \
  run_metric_collector --once
```

A leftover root-owned `/tmp/labvault-fleet-token` from oneshot is skipped (`warning skip_write`); restore still runs. The live token stays in `/var/lib/labvault/fleet-token`.

### First install (oneshot)

```bash
sudo LABVAULT_RESTORE_DATASET=/path/to/dc8-pickup-export.json \
  ./deploy/install/oneshot-compose.sh
```

Same env var works on `oneshot-systemd.sh` and `oneshot-airgap.sh`.

## What happens automatically

Import does **not** need a second “go live” click:

- Collector and heartbeat switch to **live** (even if `.env` still says `LABVAULT_WORKER_MODE=idle`).
- Chassis nodes bind to Keysight inventory by management IP (including when the same address is also a Device row).
- Topology metrics stay enabled so Lab Pulse can write samples on the next collector tick (~60s).

Then open:

| URL | Expect |
|-----|--------|
| `/keysight/` | 8 chassis, reachability from the LabVault host |
| `/lab-topology/` | `DC Lab Topology` |
| `/lab-topology/1/usage/insights/` | Pulse charts after the first live tick |
| `/cli/` → `chassis list` / `device list` / `fleet health` | Same inventory |

## If something stays idle

1. Confirm every `192.0.2.*` you intended to use is gone from the JSON.
2. Confirm each topology `device_ip` equals the chassis/device `ip_address`.
3. From the LabVault host, reach the management IPs (ICMP/API). Wrong VRF or ACL looks like “imported but offline”.
4. Chassis #auth_failed means the JSON password does not match IxOS — edit and re-import (update-or-create by IP).
5. Do not re-run `ensure_topology_insights` to “fix” an empty Pulse graph after a bad IP bind — fix the JSON and import again.

## What not to upload

- A `labvaultctl backup` tarball (Postgres/sqlite + secrets)
- A full `export_labvault_dataset` from a live appliance (every vendor, logs, reservations)
- Any file named `*-pickup-export-lab.json` (operator validation only; gitignored)

Appliance identity (SSH host keys, bootstrap credential files, fleet token files) is never part of this JSON.
