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

## Why fleet is instant and `/keysight/` cards are not

Fleet heartbeat is ICMP + a light API probe. That is enough to show **online / halt**.

The chassis **card/port grid** is a heavier IxOS call (`GET /ports`, then `get_cards` + SSH topology). A pickup JSON does **not** include that snapshot. Under gunicorn the old in-process poller is off on purpose; `labvault-refresh` now fills the cache, and heartbeat writes `/ports` into the same cache (up to 8 chassis per tick). The detail page renders that fleet port list immediately and finishes the full card map in the background.

“No cards detected / still initializing” after a live import usually means the port cache is empty (auth failed, or heartbeat has not polled `/ports` yet) — not that the box is still booting.
