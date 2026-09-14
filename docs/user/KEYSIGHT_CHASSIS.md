# Keysight / Ixia chassis

## Capabilities

- Chassis inventory under `/keysight/*`
- Sensors, licenses, KCOS operations
- Discover / deploy / upgrade job APIs
- BMC associations, node inventory, BMC board
- Reservations (see [RESERVATIONS.md](RESERVATIONS.md))

## Customer SKU limits

UHD hardware, bfshell, and ucli are **not included**. Chassis inventory covers IxOS and KCOS appliances only.

## CLI / fleet

- CLI: `chassis list`
- Fleet: `/api/fleet/chassis/<id>/health.json`, `ports.json`, `recover`, team-tags
