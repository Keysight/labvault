# Inventory

LabVault inventory spans devices, hardware/node records, BMC associations, and Keysight chassis.

## UI

- Dashboard summary
- Device list / detail
- Keysight chassis and node/BMC boards
- Import/export hooks for LabVault dataset and multibundle

## Fleet API

- `GET /api/fleet/inventory.json`
- `GET /api/fleet/inventory.csv`
- Related: `ownership.json`, `summary.json`

## CLI

`device list`, `chassis list`, `diag cheap` (empty inventory is valid → `empty_ok`).

## Empty lab

A fresh install with no devices is supported. Heartbeat/collector idle means fleet health reports idle — expected until live mode is enabled.
