# Lab graph views (Lab Pulse family)

Five graph-based dashboards alongside Lab Pulse, inspired by NOC/stream-monitoring patterns:
topology graphs with live stress encoding, streamgraphs for aggregate traffic layers, and
radar/heatmap/matrix views for fleet comparison.

## Research synthesis

- **Topology + metrics overlay** (DockGraph, StreamLens, KLogic): interactive node-link graphs where
  node size/color encodes utilization; best for spotting bridge hotspots and fabric choke points.
- **Streamgraphs / stacked flows** (D3 streamchart, Kafka pipeline explorers): show how aggregate
  traffic/CPU/memory composition shifts over time; event scatter overlays mark ownership changes.
- **Radar / small multiples** (D3 radar, Grafana multi-axis): compare devices across CPU, memory,
  ownership, and traffic dimensions in one glance.
- **Heat matrices with brush** (NetPath, observability heatmaps): device × time cells for pattern
  detection and range selection.
- **Radial gauges** (polar area, gauge dashboards): per-resource-class arcs for point-in-time fleet pulse.

## The five views

| View | URL suffix | Paradigm | Primary data |
|------|------------|----------|--------------|
| Pulse Radial | `/usage/pulse-radial/` | Radial arcs + gauge cards | insights summary + devices |
| Stream Wave | `/usage/stream-wave/` | Streamgraph + event dots | timeline metric_buckets + events |
| Radar Health | `/usage/radar-health/` | Spider/radar per device | insights devices + time_series |
| Matrix Heat | `/usage/matrix-heat/` | Device × time heatmap | timeline metric_buckets |
| Topology Pulse | `/usage/topology-pulse/` | Force graph stress overlay | insights stress_graph + fabric |

## Data mode toggle

Shared across Lab Pulse, Usage (timeline), and all five views:

- **Instantaneous** — latest bucket value or summary metrics (point-in-time).
- **Time-series** — raw server buckets unchanged.
- **Time-series average** — client-side re-bucketing (merge adjacent buckets, mean per group).

Implemented in `usage-data-mode.js` + `usage-graph-data.js`; persisted in `localStorage` key
`lv_usage_data_mode`.
