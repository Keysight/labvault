# Topology views — manual regression checklist

Run against a staging LabVault with a customer topology. Feature flags default **off** unless noted.

## Baseline (flags off)

- [ ] `/topology/` loads; Rescan LLDP works
- [ ] `/lab-topology/` list; open a topology
- [ ] Designer: nodes render; Save Layout persists x/y
- [ ] Add Hardware: `POST .../nodes/` adds chassis
- [ ] Wire mode: `POST .../links/` persists link
- [ ] Fabric Map: Refresh Live; LLDP / Planned toggles; no console errors
- [ ] Port Fabric: ports render; wire two ports; reload shows link
- [ ] Export `.labtopo.json` matches DB

## Graph API (additive — always available)

- [ ] `GET /lab-topology/<id>/graph.json?live=0&lldp=1` → `schema_version: 1`
- [ ] `GET /lab-topology/<id>/graph/compare.json` → `ok: true`, link counts within tolerance
- [ ] OCS unreachable: `meta.sources.ocs_live.ok` false, HTTP 200 (no 500)
- [ ] `POST /lab-topology/<id>/graph/refresh/` → `{ok: true}`

## Side-by-side (set `LABVAULT_GRAPH_FABRIC=1`, restart gunicorn)

- [ ] `fabric.json` shape unchanged (`nodes`, `links`, `ocs_summary`, `fetched_at`)
- [ ] Compare endpoint: `legacy_link_count` ≈ `graph_link_count` (±5%)
- [ ] Fabric Map UI unchanged visually with flag on

## Phase 2+ (when enabled)

- [ ] Inspector panel; status bar; link health colors
- [ ] Designer live overlay toggle
