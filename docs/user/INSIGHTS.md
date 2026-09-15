# Insights & usage graphs

Usage insights attach to topologies and devices. On a fresh idle install they stay empty until collection is enabled (`LABVAULT_WORKER_MODE=live` or a restore that turns Lab Pulse on).

| CLI | `insights status` |
| Workers | Collector must not stay in idle for live series |

## Views

| View | URL | What it shows |
|------|-----|----------------|
| Lab Pulse | `/usage/` | Topology + device pulse |
| Pulse Radial | `/usage/pulse-radial/` | Radial arcs and gauge cards |
| Stream Wave | `/usage/stream-wave/` | Streamgraph plus event dots |
| Radar Health | `/usage/radar-health/` | Per-device spider / radar |
| Matrix Heat | `/usage/matrix-heat/` | Device × time heatmap |
| Topology Pulse | `/usage/topology-pulse/` | Force graph with stress overlay |

Shared data-mode toggle (Lab Pulse, timeline, and the views above):

- **Instantaneous** — latest bucket or summary
- **Time-series** — raw server buckets
- **Time-series average** — client-side re-bucket (mean of adjacent buckets)

The browser stores the choice in `localStorage` key `lv_usage_data_mode`.

Chassis NP history charts (after the collector has run) also appear on the chassis detail page. The series API is `GET /keysight/api/chassis/<id>/np-timeseries/?range=24h`. Metrics live in the `np_timeseries` database — see [admin/MONITORING.md](../admin/MONITORING.md).
