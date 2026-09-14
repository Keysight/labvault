# Capacity planning

| Footprint | Guidance |
|-----------|----------|
| Small lab | 2–4 vCPU / 4–8 GiB |
| Busy polling | 4+ vCPU / 8+ GiB; watch collector CPU |
| Disk | Postgres + media grow with retention — monitor volumes |
| Gunicorn workers | Default 3; raise carefully |

Separate metrics DB (`np_timeseries`) so inventory OLTP is not blocked by telemetry writes.
