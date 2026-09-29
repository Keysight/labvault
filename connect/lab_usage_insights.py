"""Aggregate lab topology metrics into insight payloads for the Lab Pulse view.

Design goals (research-backed visibility patterns for hardware labs):
- **Fleet pulse**: headline KPIs (ownership, link, CPU/mem stress, traffic).
- **Time series**: bucketed trends so Pulse shows *when* stress happened, not just peaks.
- **Stress graph**: relationship-centric view (devices + hot ports + fabric links) for
  bottleneck and dependency tracing — graph-thinking vs flat KPI tables.
- **Bottlenecks**: scored findings with reservation-oriented remediation hints.
- **Heat matrix**: compare devices on one screen (ops war-room pattern).
- **PCPU board**: show real AresONE packet-CPU load (not chassis proxy).
- **Rankings**: Pareto top consumers — where capacity actually goes.
- **Waste radar**: owned/link-up but idle — the "eye opener" for lab managers.
- **Activity**: reservation/link event density over the window.

Payloads are built off the request path: the collector calls
``refresh_stale_insights_snapshots`` and ``write_insights_snapshot`` writes
``<cache>/insights-cache/<topo>-<preset>.json`` for the 4h and 24h presets, which
``lab_usage_insights_views`` serves.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.utils import timezone

from connect.models import LabTopologyNode
from connect.lab_metrics import get_metric_buckets
from connect.pcpu_versions import (
    attach_versions_to_device,
    attach_versions_to_pcpu_group,
    load_chassis_apps_by_parent,
    load_pcpu_apps_index,
)
from connect.topology_resource_catalog import (
    PROFILE_ARISTA_SWITCH,
    PROFILE_KEYSIGHT_CHASSIS,
    PROFILE_KEYSIGHT_PORT,
    apply_observed_keys_to_catalog,
    build_enriched_resource_catalog,
    catalog_resource_keys,
    load_port_owner_index,
    port_pcpu_mgmt_from_catalog,
    resolve_pcpu_mgmt_ip,
    restrict_metric_buckets,
)

WINDOW_PRESETS = {
    '4h': timedelta(hours=4),
    '24h': timedelta(hours=24),
    '7d': timedelta(days=7),
}

logger = logging.getLogger(__name__)
INSIGHTS_SNAPSHOT_PRESETS = ('4h', '24h')


def insights_snapshot_dir() -> Path:
    """``$LABVAULT_CACHE_DIR/insights-cache`` or ``BASE_DIR/var/insights-cache``."""
    cache_dir = (os.environ.get('LABVAULT_CACHE_DIR') or '').strip()
    if cache_dir:
        return Path(cache_dir) / 'insights-cache'
    return Path(settings.BASE_DIR) / 'var' / 'insights-cache'


def insights_snapshot_path(topo_id: int, preset: str) -> Path:
    """Snapshot file path for a topology and window preset (preset is sanitized)."""
    safe = re.sub(r'[^a-zA-Z0-9_-]+', '', preset or '24h') or '24h'
    return insights_snapshot_dir() / f'{int(topo_id)}-{safe}.json'


def read_insights_snapshot(topo_id: int, preset: str) -> Optional[Dict[str, Any]]:
    """Load a compacted Pulse snapshot, or None when missing/unreadable."""
    path = insights_snapshot_path(topo_id, preset)
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return compact_pulse_payload(data) if isinstance(data, dict) else None


def write_insights_snapshot(topo_id: int, preset: str = '24h') -> Dict[str, Any]:
    """Build Pulse JSON off the request path (collector / oneshot)."""
    now = timezone.now()
    delta = WINDOW_PRESETS.get(preset, WINDOW_PRESETS['24h'])
    payload = compact_pulse_payload(build_usage_insights(topo_id, now - delta, now))
    dest = insights_snapshot_path(topo_id, preset)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, default=str, separators=(',', ':')), encoding='utf-8')
    tmp.replace(dest)
    return payload


def refresh_insights_snapshots(topo_id: int) -> None:
    """Rewrite every preset in ``INSIGHTS_SNAPSHOT_PRESETS``; failures are logged, not raised."""
    for preset in INSIGHTS_SNAPSHOT_PRESETS:
        try:
            write_insights_snapshot(topo_id, preset)
        except Exception:
            logger.exception('insights snapshot failed topo=%s preset=%s', topo_id, preset)


def refresh_stale_insights_snapshots(
    topology_id: Optional[int] = None,
    *,
    max_age_sec: int = 60,
) -> None:
    """Rebuild Pulse JSON off the request path when the 24h snapshot is stale.

    Called from the collector (live or idle) so gunicorn never walks the
    timeseries store. Idle mode still refreshes from existing samples.
    """
    from connect.models import LabTopology

    qs = LabTopology.objects.filter(metrics_collection_enabled=True)
    if topology_id:
        qs = qs.filter(pk=topology_id)
    now = time.time()
    for topo in qs:
        snap = insights_snapshot_path(topo.pk, '24h')
        try:
            fresh = snap.exists() and (now - snap.stat().st_mtime) < max_age_sec
        except OSError:
            fresh = False
        if not fresh:
            refresh_insights_snapshots(topo.pk)

INSIGHT_METRICS = ['cpu_pct', 'mem_pct', 'bps_in', 'bps_out', 'port_ownership', 'link_speed_gbps']
# Ignore counter-reset spikes above a single 400G port.
_MAX_SANE_BPS_DISPLAY = 400e9
# Lab Pulse instant view: prefer rates below 1 Mbps (counter glitches are often Gbps+).
_PULSE_PLAUSIBLE_BPS = 1e6
_ETHERNET_IFACE_LABEL_RE = re.compile(r'^Ethernet\d', re.I)


def _is_fabric_mapped_switch_label(label: str) -> bool:
    """Designer/fabric labels differ from raw EOS interface names (Ethernet6/1)."""
    lab = (label or '').strip()
    if not lab:
        return False
    return not bool(_ETHERNET_IFACE_LABEL_RE.match(lab))
HOT_CPU = 75.0
HOT_MEM = 80.0
IDLE_BPS = 1000.0  # 1 kbps aggregate — below this counts as idle traffic
# Packet-CPU (NP) cores run a poll-mode busy-wait loop that pegs CPU near 100%
# regardless of traffic. So a PCPU reporting very high CPU but carrying less than
# this much traffic is *idle polling*, not under real test load — we must not
# flag it as hot/saturated. Real test traffic is orders of magnitude above this.
POLL_CPU_FLOOR = 90.0
POLL_IDLE_BPS = 1_000_000.0  # 1 Mbps — below this a hot NP is just busy-polling
# Instant mode keeps ports from the latest collector poll on that PCPU, not a
# wall-clock window. Chassis polls can take >20 minutes; ghosts from an older
# port-mode still have samples in the 4h/24h window.
GENERATION_SLACK_MINUTES = 3
_LIVE_PORT_METRICS = ('cpu_pct', 'mem_pct', 'bps_in', 'bps_out', 'link_speed_gbps')


def _latest_sample_iso(
    mb: Dict[str, List[List[Any]]],
    *,
    metrics: Tuple[str, ...] = _LIVE_PORT_METRICS,
) -> Optional[str]:
    best = None
    for name in metrics:
        series = (mb or {}).get(name)
        if not isinstance(series, list) or not series:
            continue
        pt = series[-1]
        if not isinstance(pt, (list, tuple)) or not pt:
            continue
        ts = pt[0]
        if ts is None:
            continue
        s = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
        if best is None or s > best:
            best = s
    return best


def _parse_sample_dt(ts_iso: Optional[str]):
    if not ts_iso:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_iso).replace('Z', '+00:00'))
    except ValueError:
        return None
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.utc)
    return ts


def mark_pcpu_generation_freshness(pcpu_groups: Dict[str, Dict[str, Any]]) -> None:
    """Mark ports from each chassis' most recent collector poll as fresh.

    Compare against the parent chassis, not the PCPU group. Leftover ports
    that inferred their own 10.0.1.x still lose to the live poll on that box.
    """
    slack = timedelta(minutes=GENERATION_SLACK_MINUTES)
    generation_by_parent: Dict[str, Any] = {}
    for grp in pcpu_groups.values():
        parent = grp.get('parent')
        for port in grp.get('ports') or []:
            ts = _parse_sample_dt(port.get('last_sample_at'))
            if ts is None:
                continue
            prev = generation_by_parent.get(parent)
            if prev is None or ts > prev:
                generation_by_parent[parent] = ts
    for grp in pcpu_groups.values():
        generation = generation_by_parent.get(grp.get('parent'))
        for port in grp.get('ports') or []:
            ts = _parse_sample_dt(port.get('last_sample_at'))
            port['fresh'] = bool(generation and ts and (generation - ts) <= slack)


def prune_stale_pcpu_ports(pcpu_groups: Dict[str, Dict[str, Any]]) -> None:
    """Drop designer / old-mode leftovers once a chassis has a live poll."""
    mark_pcpu_generation_freshness(pcpu_groups)
    parent_has_live = {
        grp.get('parent')
        for grp in pcpu_groups.values()
        if any(p.get('fresh') for p in (grp.get('ports') or []))
    }
    for pk, grp in list(pcpu_groups.items()):
        ports = list(grp.get('ports') or [])
        if grp.get('parent') in parent_has_live:
            ports = [p for p in ports if p.get('fresh')]
            grp['ports'] = ports
        if not ports:
            del pcpu_groups[pk]
            continue
        grp['port_count'] = len(ports)
        grp['owned_count'] = sum(1 for p in ports if p.get('owned'))
        grp['traffic_count'] = sum(1 for p in ports if p.get('has_traffic'))


def _series_stats(series: List[List[Any]], *, metric: str = '') -> Dict[str, Optional[float]]:
    if not series:
        return {'latest': None, 'avg': None, 'max': None}
    vals = [float(p[1]) for p in series if len(p) >= 2 and p[1] is not None]
    if not vals:
        return {'latest': None, 'avg': None, 'max': None}
    sane = vals
    if metric in ('bps_in', 'bps_out'):
        plausible = [v for v in vals if 0 < v <= _PULSE_PLAUSIBLE_BPS]
        sane = plausible or [v for v in vals if 0 <= v <= _MAX_SANE_BPS_DISPLAY]
        if not sane:
            sane = vals
    latest = sane[-1]
    if metric in ('bps_in', 'bps_out'):
        if latest == 0 or latest > _PULSE_PLAUSIBLE_BPS:
            fallback = None
            for v in reversed(vals):
                if 0 < v <= _PULSE_PLAUSIBLE_BPS:
                    fallback = v
                    break
            if fallback is None and latest == 0:
                for v in reversed(vals):
                    if v > 0:
                        fallback = v
                        break
            if fallback is not None:
                latest = fallback
            elif latest > _PULSE_PLAUSIBLE_BPS:
                latest = 0.0
    return {
        'latest': round(latest, 2),
        'avg': round(sum(sane) / len(sane), 2),
        'max': round(max(sane), 2),
    }


def _bucket_for_window(hours: float) -> str:
    if hours > 48:
        return '1h'
    # 5m through 48h — 15m buckets max-out counter glitches and hide real PCPU rates.
    return '5m'


def _parse_port_parent(resource_key: str) -> Optional[str]:
    if '__' not in resource_key:
        return None
    return resource_key.split('__', 1)[0]


def _resolve_port_iface_ip(
    rkey: str,
    meta: Dict[str, Any],
    catalog: Dict[str, Dict[str, Any]],
    port_mgmt: Dict[str, str],
) -> str:
    """Best display IP for a port: PCPU host when known, else parent device mgmt."""
    label = meta.get('label') or rkey.split('__', 1)[-1]
    parent = (meta.get('parent') or _parse_port_parent(rkey) or '').strip()
    parent_meta = catalog.get(parent, {}) or {}
    pcpu = resolve_pcpu_mgmt_ip(
        label,
        chassis_type=parent_meta.get('chassis_type') or '',
        cached=port_mgmt.get(rkey) or '',
        meta_ip=meta.get('pcpu_ip') or '',
    )
    if pcpu:
        return pcpu
    chassis = (meta.get('chassis_ip') or '').strip()
    if not chassis and parent:
        pm = catalog.get(parent, {}) or {}
        chassis = (pm.get('mgmt_ip') or pm.get('chassis_ip') or '').strip()
    return chassis


def _downsample_series(series: List[List[Any]], max_points: int = 96) -> List[List[Any]]:
    if len(series) <= max_points:
        return series
    step = len(series) / max_points
    out: List[List[Any]] = []
    i = 0.0
    while int(i) < len(series):
        out.append(series[int(i)])
        i += step
    if out and out[-1] != series[-1]:
        out.append(series[-1])
    return out


def _fleet_time_series(
    buckets: Dict[str, Dict[str, List[List[Any]]]],
    catalog: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate chassis CPU/mem and port traffic into fleet-level bucketed series."""
    node_keys = [k for k, v in catalog.items() if v.get('profile') != PROFILE_KEYSIGHT_PORT and '__' not in k]
    port_keys = [k for k, v in catalog.items() if v.get('profile') == PROFILE_KEYSIGHT_PORT or '__' in k]
    chassis_keys = [
        k for k in node_keys
        if catalog.get(k, {}).get('profile') == PROFILE_KEYSIGHT_CHASSIS
    ]

    ts_acc: Dict[Any, Dict[str, List[float]]] = defaultdict(lambda: {
        'cpu': [], 'mem': [], 'bps_in': [], 'bps_out': [], 'owned': [],
    })

    for rkey in chassis_keys:
        mb = buckets.get(rkey, {})
        for ts, v in mb.get('cpu_pct', []):
            if v is not None:
                ts_acc[ts]['cpu'].append(float(v))
        for ts, v in mb.get('mem_pct', []):
            if v is not None:
                ts_acc[ts]['mem'].append(float(v))

    for rkey in port_keys:
        mb = buckets.get(rkey, {})
        for ts, v in mb.get('bps_in', []):
            if v is not None:
                ts_acc[ts]['bps_in'].append(float(v))
        for ts, v in mb.get('bps_out', []):
            if v is not None:
                ts_acc[ts]['bps_out'].append(float(v))
        for ts, v in mb.get('port_ownership', []):
            if v is not None and float(v) >= 0.5:
                ts_acc[ts]['owned'].append(1.0)

    port_total = max(1, len(port_keys))

    def _avg(vals: List[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 2) if vals else None

    cpu_series: List[List[Any]] = []
    mem_series: List[List[Any]] = []
    bps_series: List[List[Any]] = []
    owned_series: List[List[Any]] = []

    for ts in sorted(ts_acc.keys()):
        row = ts_acc[ts]
        cpu_series.append([ts, _avg(row['cpu'])])
        mem_series.append([ts, _avg(row['mem'])])
        bps = sum(row['bps_in']) + sum(row['bps_out'])
        bps_series.append([ts, round(bps, 0) if bps else 0])
        owned_pct = round(100.0 * len(row['owned']) / port_total, 1) if row['owned'] else 0.0
        owned_series.append([ts, owned_pct])

    return {
        'cpu_pct': _downsample_series(cpu_series),
        'mem_pct': _downsample_series(mem_series),
        'bps_total': _downsample_series(bps_series),
        'ports_owned_pct': _downsample_series(owned_series),
    }


def _device_time_series(
    buckets: Dict[str, Dict[str, List[List[Any]]]],
    devices: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out = []
    for dev in devices:
        mb = buckets.get(dev['resource_key'], {})
        cpu = _downsample_series(mb.get('cpu_pct', []), 48)
        mem = _downsample_series(mb.get('mem_pct', []), 48)
        if not cpu and not mem:
            continue
        out.append({
            'resource_key': dev['resource_key'],
            'label': dev['label'],
            'cpu_pct': cpu,
            'mem_pct': mem,
        })
    return out


def _port_stress_score(cpu_max: Optional[float], mem_max: Optional[float], bps_peak: float, owned: bool) -> float:
    cpu = cpu_max or 0
    mem = mem_max or 0
    bps_norm = min(100.0, math.log10(max(bps_peak, 1)) * 12)
    owned_boost = 15.0 if owned else 0.0
    return round(min(100.0, max(cpu, mem, bps_norm) + owned_boost), 1)


def _load_fabric_light(topo_id: int) -> Dict[str, Any]:
    """Topology links for stress graph — no live probe, LLDP + planned only."""
    try:
        from connect.port_usage_graph import LabPortUsageGraphBuilder

        return LabPortUsageGraphBuilder(topo_id).build(
            live=False,
            lldp=True,
            ocs=False,
            planned=True,
            reservations=False,
        )
    except Exception:
        return {'devices': [], 'port_links': [], 'port_nodes': []}


def _build_stress_graph(
    topo_id: int,
    devices: List[Dict[str, Any]],
    rankings: Dict[str, List[Dict[str, Any]]],
    port_stress: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Nodes + weighted edges for relationship-centric bottleneck exploration."""
    fabric = _load_fabric_light(topo_id)
    dev_by_rkey = {d['resource_key']: d for d in devices}
    dev_by_id = {d.get('node_id'): d for d in devices if d.get('node_id') is not None}

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    node_ids: set = set()

    for dev in devices:
        m = dev.get('metrics') or {}
        cpu = (m.get('cpu_pct') or {}).get('max') or 0
        mem = (m.get('mem_pct') or {}).get('max') or 0
        ports = dev.get('ports') or {}
        total = max(1, ports.get('total') or 1)
        hot_ratio = (ports.get('hot_cpu') or 0) / total
        stress = round(min(100.0, max(cpu, mem, hot_ratio * 100)), 1)
        nid = dev['resource_key']
        node_ids.add(nid)
        nodes.append({
            'id': nid,
            'type': 'device',
            'label': dev['label'],
            'mgmt_ip': dev.get('mgmt_ip') or '',
            'stress': stress,
            'profile': dev.get('profile'),
            'ports_hot': ports.get('hot_cpu', 0),
            'ports_owned': ports.get('owned', 0),
        })

    hot_ports = sorted(
        port_stress.values(),
        key=lambda p: p.get('stress') or 0,
        reverse=True,
    )[:18]
    for p in hot_ports:
        if (p.get('stress') or 0) < 25:
            continue
        pid = p['resource_key']
        node_ids.add(pid)
        nodes.append({
            'id': pid,
            'type': 'port',
            'label': p['label'],
            'chassis_ip': p.get('chassis_ip') or '',
            'owner': p.get('owner') or 'Free',
            'owned': bool(p.get('owned')),
            'stress': p['stress'],
            'parent': p.get('parent'),
        })
        parent = p.get('parent')
        if parent and parent in dev_by_rkey:
            edges.append({
                'source': parent,
                'target': pid,
                'type': 'hosts',
                'weight': p['stress'],
            })

    port_to_dev: Dict[str, str] = {}
    for pn in fabric.get('port_nodes') or []:
        rkey = pn.get('resource_key') or pn.get('id')
        parent = pn.get('device_id') or pn.get('parent')
        if rkey and parent:
            dev = dev_by_id.get(parent) or dev_by_rkey.get(f'node_{parent}')
            if dev:
                port_to_dev[str(rkey)] = dev['resource_key']
                port_to_dev[str(pn.get('id'))] = dev['resource_key']

    seen_links: set = set()
    for link in fabric.get('port_links') or []:
        src = str(link.get('source') or '')
        tgt = str(link.get('target') or '')
        src_dev = port_to_dev.get(src) or (src if src in dev_by_rkey else None)
        tgt_dev = port_to_dev.get(tgt) or (tgt if tgt in dev_by_rkey else None)
        if not src_dev or not tgt_dev or src_dev == tgt_dev:
            continue
        pair = tuple(sorted((src_dev, tgt_dev)))
        if pair in seen_links:
            continue
        seen_links.add(pair)
        s1 = dev_by_rkey.get(src_dev, {}).get('metrics', {}).get('cpu_pct', {}).get('max') or 0
        s2 = dev_by_rkey.get(tgt_dev, {}).get('metrics', {}).get('cpu_pct', {}).get('max') or 0
        weight = round(max(s1, s2, 10), 1)
        edges.append({
            'source': src_dev,
            'target': tgt_dev,
            'type': 'fabric',
            'weight': weight,
        })

    bridge_ids = {n['id'] for n in nodes if n['type'] == 'device' and sum(
        1 for e in edges if e['type'] == 'fabric' and (e['source'] == n['id'] or e['target'] == n['id'])
    ) >= 2}
    for n in nodes:
        if n['id'] in bridge_ids and n['type'] == 'device':
            n['role'] = 'bridge'

    return {
        'nodes': nodes,
        'edges': edges,
        'meta': {
            'node_count': len(nodes),
            'edge_count': len(edges),
            'bridge_devices': len(bridge_ids),
            'graph_thinking': (
                'Relationship view: node size/color = stress; thick edges = hot fabric paths; '
                'bridge devices connect multiple peers — failures or saturation there ripple across LaaS runs.'
            ),
        },
    }


def _detect_bottlenecks(
    summary: Dict[str, Any],
    devices: List[Dict[str, Any]],
    pcpu_fleet: List[Dict[str, Any]],
    waste_signals: List[Dict[str, Any]],
    rankings: Dict[str, List[Dict[str, Any]]],
    port_stress: Dict[str, Dict[str, Any]],
    link_errors: Optional[List[Dict[str, Any]]] = None,
    switch_input_discards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Actionable findings for lab ops and future LaaS auto-remediation."""
    findings: List[Dict[str, Any]] = []
    sev_rank = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}

    for err in (link_errors or [])[:8]:
        total = err.get('total') or 0
        if total <= 0:
            continue
        ip = err.get('chassis_ip')
        label = f"{err.get('label')}{f' ({ip})' if ip else ''}"
        findings.append({
            'type': 'link_errors',
            'severity': 'high' if total >= 100 else 'medium',
            'resource_key': err.get('resource_key'),
            'chassis_ip': ip,
            'label': label,
            'score': round(total, 1),
            'detail': (
                f"{err.get('crc_errors', 0)} CRC · {err.get('alignment_errors', 0)} alignment · "
                f"{err.get('fragments', 0)} fragment errors in window"
            ),
            'laas_hint': 'Check transceiver/cable seating, speed/FEC mismatch, or signal integrity on this port.',
        })

    for disc in (switch_input_discards or [])[:12]:
        total = disc.get('total_drops') or 0
        if total <= 0:
            continue
        rate = disc.get('drop_rate_per_min') or 0
        ip = disc.get('chassis_ip')
        label = f"{disc.get('iface_label') or disc.get('label')}{f' ({ip})' if ip else ''}"
        findings.append({
            'type': 'switch_input_discard',
            'severity': 'critical' if rate >= 10 or total >= 500 else ('high' if rate >= 1 or total >= 50 else 'medium'),
            'resource_key': disc.get('resource_key'),
            'chassis_ip': ip,
            'label': label,
            'score': round(total, 1),
            'detail': (
                f"{total} input discards in window · peak {disc.get('peak_interval_drops', 0)}/interval · "
                f"~{rate}/min · first at {disc.get('first_drop_at') or '—'}"
            ),
            'laas_hint': (
                'RoCE fabric signal: rising input discards on DUT/spine ports often correlate with '
                'retransmits — check buffer/QoS, PFC/ECN, and congestion vs traffic-generator loss.'
            ),
        })

    for pcpu in pcpu_fleet:
        cpu = pcpu.get('cpu_pct') or 0
        mem = pcpu.get('mem_pct') or 0
        # Poll-mode NPs sit near 100% CPU while idle — only a memory-pressured or
        # traffic-loaded PCPU is a real bottleneck.
        if pcpu.get('poll_mode') and mem < HOT_MEM:
            continue
        if cpu < HOT_CPU and mem < HOT_MEM:
            continue
        mgmt = pcpu.get('mgmt_ip')
        chassis_ip = pcpu.get('chassis_ip')
        chassis_lbl = pcpu.get('chassis')
        if chassis_ip:
            chassis_lbl = f"{chassis_lbl} ({chassis_ip})"
        findings.append({
            'type': 'pcpu_saturation',
            'severity': 'critical' if cpu >= 90 or mem >= 90 else 'high',
            'resource_key': pcpu.get('resource_key'),
            'mgmt_ip': mgmt,
            'chassis_ip': chassis_ip,
            'label': f"{chassis_lbl} PCPU{f' {mgmt}' if mgmt else ''} ({pcpu.get('port_count')} ports)",
            'score': round(max(cpu, mem), 1),
            'detail': f"Packet CPU at {cpu}% CPU / {mem}% memory — ports {_format_pcpu_port_summary(pcpu)}",
            'laas_hint': 'Spread traffic across ports/line cards or reduce concurrent reservations on this PCPU group.',
        })

    for dev in devices:
        ports = dev.get('ports') or {}
        total = ports.get('total') or 0
        if total < 2:
            continue
        hot = ports.get('hot_cpu') or 0
        if hot / total >= 0.4:
            dev_ip = dev.get('mgmt_ip')
            dev_label = f"{dev['label']} ({dev_ip})" if dev_ip else dev['label']
            findings.append({
                'type': 'chassis_port_hotspot',
                'severity': 'high' if hot / total >= 0.6 else 'medium',
                'resource_key': dev['resource_key'],
                'mgmt_ip': dev_ip,
                'label': dev_label,
                'score': round(100.0 * hot / total, 1),
                'detail': f"{hot}/{total} ports exceeded {HOT_CPU}% CPU in window",
                'laas_hint': 'Profile which LaaS tests pin to these ports; consider port rotation in reservation policy.',
            })

    mismatch = (summary.get('ports_owned_pct') or 0) - (summary.get('ports_traffic_pct') or 0)
    if mismatch > 20 and (summary.get('waste_owned_idle') or 0) > 0:
        findings.append({
            'type': 'ownership_traffic_gap',
            'severity': 'medium',
            'resource_key': None,
            'label': 'Fleet reservation mismatch',
            'score': round(mismatch, 1),
            'detail': (
                f"{summary.get('ports_owned_pct')}% ports owned vs {summary.get('ports_traffic_pct')}% with traffic "
                f"({summary.get('waste_owned_idle')} idle owned ports)"
            ),
            'laas_hint': 'Release stale reservations or enforce idle timeouts in LaaS scheduling.',
        })

    for w in waste_signals[:12]:
        findings.append({
            'type': 'owned_idle_port',
            'severity': 'medium',
            'resource_key': w.get('resource_key'),
            'label': w.get('label'),
            'score': 50.0,
            'detail': w.get('detail'),
            'laas_hint': 'Port is reserved but idle — candidate for auto-release or nudge to owner.',
        })

    by_parent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in rankings.get('bps_total') or []:
        by_parent[item.get('parent') or ''].append(item)
    for parent, items in by_parent.items():
        if len(items) < 2 or not parent:
            continue
        total_bps = sum(i['value'] for i in items)
        if total_bps <= 0:
            continue
        top = items[0]
        share = top['value'] / total_bps
        if share >= 0.65:
            dev = next((d for d in devices if d['resource_key'] == parent), None)
            findings.append({
                'type': 'traffic_concentration',
                'severity': 'high' if share >= 0.85 else 'medium',
                'resource_key': top.get('resource_key'),
                'label': f"{dev['label'] if dev else parent} → {top.get('label')}",
                'score': round(share * 100, 1),
                'detail': f"Port carries {round(share * 100)}% of chassis peak traffic ({fmt_bps_static(top['value'])})",
                'laas_hint': 'Single-port bottleneck — split flows or add parallel paths in test topology.',
            })

    for rkey, ps in port_stress.items():
        if (ps.get('stress') or 0) >= 85 and ps.get('owned'):
            findings.append({
                'type': 'hot_owned_port',
                'severity': 'high',
                'resource_key': rkey,
                'label': ps.get('label'),
                'score': ps.get('stress'),
                'detail': 'Owned port with combined CPU/mem/traffic stress',
                'laas_hint': 'Watch this port during LaaS runs — likely limiting factor for throughput tests.',
            })

    findings.sort(key=lambda f: (sev_rank.get(f.get('severity', 'low'), 9), -(f.get('score') or 0)))
    deduped: List[Dict[str, Any]] = []
    seen_keys: set = set()
    for f in findings:
        key = (f.get('type'), f.get('resource_key'), f.get('label'))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(f)
    return deduped[:30]


def fmt_bps_static(v: float) -> str:
    if v >= 1e9:
        return f'{v / 1e9:.1f} Gbps'
    if v >= 1e6:
        return f'{v / 1e6:.1f} Mbps'
    if v >= 1e3:
        return f'{v / 1e3:.1f} kbps'
    return f'{v:.0f} bps'


def _owner_from_port_events(evs: List[Dict[str, Any]]) -> str:
    """Derive current owner at end of event timeline for a port resource key."""
    owner = 'Free'
    for ev in evs:
        et = ev.get('event_type')
        pl = ev.get('payload') or {}
        if et == 'port_owned':
            o = (pl.get('owner') or '').strip()
            if o and o != 'Free':
                owner = o
        elif et == 'port_released':
            owner = 'Free'
    return owner


def _link_state_from_events(evs: List[Dict[str, Any]]) -> Optional[bool]:
    """Derive link state at end of the insights window from link_up/link_down events."""
    link_up: Optional[bool] = None
    for ev in evs:
        et = ev.get('event_type')
        if et == 'link_up':
            link_up = True
        elif et == 'link_down':
            link_up = False
    return link_up


def _resolve_port_link_up(
    evs: List[Dict[str, Any]],
    link_s: Dict[str, Any],
) -> Optional[bool]:
    """Prefer collector link events; fall back to latest link_speed sample (not window max)."""
    from_events = _link_state_from_events(evs)
    if from_events is not None:
        return from_events
    latest = link_s.get('latest')
    if latest is not None:
        return (latest or 0) > 0
    return None


def _port_owner_index(
    topo_id: int,
    events: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, str]:
    """Merge collector cache with ownership events in the insights window."""
    index = load_port_owner_index(topo_id)
    if not events:
        return index
    for rkey, evs in events.items():
        if '__' not in rkey:
            continue
        if index.get(rkey, 'Free') != 'Free':
            continue
        owner = _owner_from_port_events(evs)
        if owner != 'Free':
            index[rkey] = owner
    return index


def _format_pcpu_port_summary(pcpu: Dict[str, Any], limit: int = 8) -> str:
    ports = pcpu.get('ports') or []
    if ports and isinstance(ports[0], dict):
        parts = []
        for p in ports[:limit]:
            lbl = p.get('label') or '?'
            own = p.get('owner') or 'Free'
            parts.append(f"{lbl} ({own})" if own != 'Free' else lbl)
        extra = len(ports) - limit
        if extra > 0:
            parts.append(f'+{extra} more')
        return ', '.join(parts)
    return ', '.join(pcpu.get('sample_ports') or [])


def _insight_from_buckets(
    buckets: Dict[str, Dict[str, List[List[Any]]]],
    catalog: Dict[str, Dict[str, Any]],
    *,
    topo_id: int,
    events: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    window_minutes: float = 60.0,
    window_to=None,
) -> Dict[str, Any]:
    node_keys = [k for k, v in catalog.items() if v.get('profile') != PROFILE_KEYSIGHT_PORT and '__' not in k]
    port_keys = [k for k, v in catalog.items() if v.get('profile') == PROFILE_KEYSIGHT_PORT or '__' in k]

    devices: Dict[str, Dict[str, Any]] = {}
    for rkey in node_keys:
        meta = catalog.get(rkey, {})
        mb = buckets.get(rkey, {})
        devices[rkey] = {
            'resource_key': rkey,
            'node_id': meta.get('node_id'),
            'label': meta.get('label') or rkey,
            'mgmt_ip': meta.get('mgmt_ip') or '',
            'profile': meta.get('profile'),
            'node_type': meta.get('node_type'),
            'metrics': {m: _series_stats(mb.get(m, [])) for m in INSIGHT_METRICS if m in (meta.get('metrics') or [])},
            'ports': {
                'total': 0,
                'owned': 0,
                'link_up': 0,
                'hot_cpu': 0,
                'hot_mem': 0,
                'idle_owned': 0,
            },
            'pcpus': {},
        }

    pcpu_groups: Dict[str, Dict[str, Any]] = {}
    pcpu_apps_idx = load_pcpu_apps_index(topo_id)
    chassis_apps_by_parent = load_chassis_apps_by_parent(topo_id)
    rankings: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    waste_signals: List[Dict[str, Any]] = []
    link_errors: List[Dict[str, Any]] = []
    switch_input_discards: List[Dict[str, Any]] = []
    port_stress: Dict[str, Dict[str, Any]] = {}

    owned_count = 0
    link_up_count = 0
    traffic_count = 0
    port_total = 0
    hot_pcpu = 0
    port_mgmt = port_pcpu_mgmt_from_catalog(catalog)
    port_owners = _port_owner_index(topo_id, events)

    for rkey in port_keys:
        meta = catalog.get(rkey, {})
        parent = (meta.get('parent') or _parse_port_parent(rkey) or '').strip()
        mb = buckets.get(rkey, {})
        label = meta.get('label') or rkey.split('__', 1)[-1]
        chassis_ip = meta.get('chassis_ip') or (catalog.get(parent, {}) or {}).get('mgmt_ip') or ''
        iface_ip = _resolve_port_iface_ip(rkey, meta, catalog, port_mgmt)
        owner_name = port_owners.get(rkey, 'Free')

        cpu_s = _series_stats(mb.get('cpu_pct', []))
        mem_s = _series_stats(mb.get('mem_pct', []))
        bps_in_s = _series_stats(mb.get('bps_in', []), metric='bps_in')
        bps_out_s = _series_stats(mb.get('bps_out', []), metric='bps_out')
        own_s = _series_stats(mb.get('port_ownership', []))
        bps_peak = max(bps_in_s['max'] or 0, bps_out_s['max'] or 0)

        port_total += 1
        is_owned = (own_s['latest'] or 0) >= 0.5 or (own_s['max'] or 0) >= 0.5
        if is_owned:
            owned_count += 1

        link_s = _series_stats(mb.get('link_speed_gbps', []))
        has_traffic = bps_peak > IDLE_BPS
        if has_traffic:
            traffic_count += 1
        port_evs = (events or {}).get(rkey, [])
        link_up = _resolve_port_link_up(port_evs, link_s)
        if link_up is True:
            link_up_count += 1

        dev = devices.get(parent)
        if dev:
            dev['ports']['total'] += 1
            if is_owned:
                dev['ports']['owned'] += 1
            if link_up is True:
                dev['ports']['link_up'] += 1
            if (cpu_s['max'] or 0) >= HOT_CPU:
                dev['ports']['hot_cpu'] += 1
            if (mem_s['max'] or 0) >= HOT_MEM:
                dev['ports']['hot_mem'] += 1
            if is_owned and not has_traffic:
                dev['ports']['idle_owned'] += 1

        pcpu_ip = resolve_pcpu_mgmt_ip(
            label,
            chassis_type=(catalog.get(parent, {}) or {}).get('chassis_type') or '',
            cached=port_mgmt.get(rkey) or '',
            meta_ip=meta.get('pcpu_ip') or '',
        )
        rank_base = {
            'resource_key': rkey, 'label': label, 'parent': parent,
            'chassis_ip': chassis_ip, 'pcpu_ip': pcpu_ip, 'iface_ip': iface_ip,
            'owner': owner_name if is_owned else 'Free',
        }
        if cpu_s['max'] is not None:
            rankings['cpu_pct'].append({**rank_base, 'value': cpu_s['max']})
        if mem_s['max'] is not None:
            rankings['mem_pct'].append({**rank_base, 'value': mem_s['max']})
        bps_total = (bps_in_s['max'] or 0) + (bps_out_s['max'] or 0)
        if bps_total > 0:
            rankings['bps_total'].append({**rank_base, 'value': round(bps_total, 0)})

        # Link errors (per-interval deltas summed over the window).
        crc_sum = sum(float(p[1]) for p in mb.get('crc_errors', []) if len(p) >= 2 and p[1])
        algn_sum = sum(float(p[1]) for p in mb.get('alignment_errors', []) if len(p) >= 2 and p[1])
        frag_sum = sum(float(p[1]) for p in mb.get('fragments', []) if len(p) >= 2 and p[1])
        if crc_sum or algn_sum or frag_sum:
            link_errors.append({
                'resource_key': rkey, 'label': label, 'parent': parent, 'chassis_ip': chassis_ip,
                'crc_errors': round(crc_sum), 'alignment_errors': round(algn_sum), 'fragments': round(frag_sum),
                'total': round(crc_sum + algn_sum + frag_sum),
            })

        disc_pts = mb.get('input_discards', [])
        if disc_pts:
            total_drops = sum(float(p[1]) for p in disc_pts if len(p) >= 2 and p[1])
            if total_drops > 0:
                peak_interval = max(float(p[1]) for p in disc_pts if len(p) >= 2 and p[1] is not None)
                first_drop_at = None
                for p in disc_pts:
                    if len(p) >= 2 and float(p[1] or 0) > 0:
                        first_drop_at = p[0]
                        break
                parent_profile = (catalog.get(parent, {}) or {}).get('profile')
                switch_input_discards.append({
                    'resource_key': rkey,
                    'label': label,
                    'iface_label': label,
                    'parent': parent,
                    'chassis_ip': chassis_ip,
                    'parent_profile': parent_profile,
                    'total_drops': round(total_drops),
                    'peak_interval_drops': round(peak_interval),
                    'first_drop_at': first_drop_at,
                    'drop_rate_per_min': round(total_drops / max(window_minutes, 1.0), 2),
                    'is_fabric_mapped': _is_fabric_mapped_switch_label(label),
                })

        if is_owned and bps_peak <= IDLE_BPS:
            waste_signals.append({
                'type': 'owned_idle',
                'resource_key': rkey,
                'label': label,
                'parent': parent,
                'chassis_ip': chassis_ip,
                'owner': owner_name if owner_name != 'Free' else 'Reserved',
                'detail': 'Port reserved/owned but no meaningful traffic in window',
                'cpu_pct': cpu_s['latest'],
                'mem_pct': mem_s['latest'],
            })

        stress = _port_stress_score(cpu_s['max'], mem_s['max'], bps_peak, is_owned)
        if stress >= 10 or is_owned:
            port_stress[rkey] = {
                'resource_key': rkey,
                'label': label,
                'parent': parent,
                'chassis_ip': chassis_ip,
                'owner': owner_name if is_owned else 'Free',
                'stress': stress,
                'owned': is_owned,
                'cpu_pct': cpu_s['max'],
                'mem_pct': mem_s['max'],
                'bps_peak': bps_peak,
            }

        # Group by PCPU managementIp (not cpu/mem signature — each 10.0.x.x host is one board).
        mgmt_ip = pcpu_ip
        has_metrics = (
            cpu_s['latest'] is not None
            or mem_s['latest'] is not None
            or bps_in_s['latest'] is not None
            or bps_out_s['latest'] is not None
            or bps_peak > IDLE_BPS
        )
        if mgmt_ip and parent:
            pk = f'{parent}::{mgmt_ip}'
            grp = pcpu_groups.setdefault(pk, {
                'parent': parent,
                'mgmt_ip': mgmt_ip,
                'chassis_ip': chassis_ip,
                'cpu_pct': cpu_s['latest'],
                'mem_pct': mem_s['latest'],
                'bps_in_peak': 0.0,
                'bps_out_peak': 0.0,
                'bps_in_total': 0.0,
                'bps_out_total': 0.0,
                'bps_in_avg_total': 0.0,
                'bps_out_avg_total': 0.0,
                'port_count': 0,
                'owned_count': 0,
                'traffic_count': 0,
                'owners': set(),
                'ports': [],
            })
            grp['port_count'] += 1
            if is_owned:
                grp['owned_count'] += 1
                if owner_name != 'Free':
                    grp['owners'].add(owner_name)
            if has_traffic:
                grp['traffic_count'] += 1
            if cpu_s['latest'] is not None:
                grp['cpu_pct'] = cpu_s['latest']
            if mem_s['latest'] is not None:
                grp['mem_pct'] = mem_s['latest']
            grp['bps_in_peak'] = max(grp['bps_in_peak'], bps_in_s['max'] or 0)
            grp['bps_out_peak'] = max(grp['bps_out_peak'], bps_out_s['max'] or 0)
            bps_in_inst = bps_in_s['latest']
            bps_out_inst = bps_out_s['latest']
            grp['bps_in_total'] += bps_in_inst or 0
            grp['bps_out_total'] += bps_out_inst or 0
            grp['bps_in_avg_total'] += bps_in_s['avg'] or 0
            grp['bps_out_avg_total'] += bps_out_s['avg'] or 0
            speed = link_s.get('latest')
            last_sample_at = _latest_sample_iso(mb)
            grp['ports'].append({
                'label': label,
                'resource_key': rkey,
                'owner': owner_name if owner_name != 'Free' else ('Reserved' if is_owned else 'Free'),
                'owned': is_owned,
                'link_up': link_up,
                'speed_gbps': round(float(speed), 1) if speed else None,
                'bps_in': round(bps_in_inst, 2) if bps_in_inst is not None else None,
                'bps_out': round(bps_out_inst, 2) if bps_out_inst is not None else None,
                'bps_peak': round(bps_peak, 2),
                'cpu_pct': cpu_s['latest'],
                'mem_pct': mem_s['latest'],
                'has_traffic': has_traffic,
                'last_sample_at': last_sample_at,
                'fresh': False,
            })

    prune_stale_pcpu_ports(pcpu_groups)

    for dev in devices.values():
        if dev.get('profile') == PROFILE_KEYSIGHT_CHASSIS:
            attach_versions_to_device(
                dev,
                chassis_apps_by_parent=chassis_apps_by_parent,
            )

    for pk, grp in pcpu_groups.items():
        attach_versions_to_pcpu_group(
            grp,
            pcpu_apps=pcpu_apps_idx,
            chassis_apps_by_parent=chassis_apps_by_parent,
        )
        # Poll-mode idle: cores spin near 100% but no real traffic flows.
        traffic_peak = max(grp.get('bps_in_total') or 0.0, grp.get('bps_out_total') or 0.0,
                           grp.get('bps_in_peak') or 0.0, grp.get('bps_out_peak') or 0.0)
        grp['poll_mode'] = (grp.get('cpu_pct') or 0) >= POLL_CPU_FLOOR and traffic_peak < POLL_IDLE_BPS
        owners = grp.pop('owners', set())
        grp['owners'] = sorted(owners)
        grp['ports'].sort(key=lambda p: (not p.get('owned'), p.get('label') or ''))
        parent = grp['parent']
        if parent in devices:
            devices[parent].setdefault('pcpus', {})
            idx = len(devices[parent]['pcpus']) + 1
            devices[parent]['pcpus'][f'pcpu_{idx}'] = grp
            # Only a genuinely loaded PCPU counts as hot — not an idle poll loop.
            if not grp['poll_mode'] and ((grp['cpu_pct'] or 0) >= HOT_CPU or (grp['mem_pct'] or 0) >= HOT_MEM):
                hot_pcpu += 1

    # Build PCPU port index for DUT ↔ PCPU cross-linking (label → connection info).
    pcpu_port_by_label: Dict[str, Dict[str, str]] = {}
    for dev in devices.values():
        if dev.get('profile') != PROFILE_KEYSIGHT_CHASSIS:
            continue
        for _pid, pcpu in (dev.get('pcpus') or {}).items():
            for port in pcpu.get('ports', []):
                lbl = (port.get('label') or '').lower().strip()
                if not lbl:
                    continue
                pcpu_port_by_label[lbl] = {
                    'pcpu_chassis': dev.get('label') or '',
                    'pcpu_chassis_ip': dev.get('mgmt_ip') or '',
                    'pcpu_ip': pcpu.get('mgmt_ip') or '',
                    'owner': port.get('owner') or 'Free',
                }

    # Enrich switch_input_discards with connected PCPU info, build DUT groups.
    discards_by_parent: Dict[str, List[Dict]] = defaultdict(list)
    for d in switch_input_discards:
        lbl = (d.get('iface_label') or '').lower().strip()
        match = pcpu_port_by_label.get(lbl)
        if match and d.get('is_fabric_mapped'):
            d['connected_pcpu'] = match
        parent = d.get('parent') or ''
        if parent:
            discards_by_parent[parent].append(d)

    discard_label_set: set = set()
    dut_drops: List[Dict[str, Any]] = []
    for parent_key, port_list in discards_by_parent.items():
        parent_dev = devices.get(parent_key)
        if not parent_dev or parent_dev.get('profile') != PROFILE_ARISTA_SWITCH:
            continue
        total = sum(d.get('total_drops') or 0 for d in port_list)
        first = min(
            (d.get('first_drop_at') for d in port_list if d.get('first_drop_at')),
            default=None,
        )
        peak = max(d.get('peak_interval_drops') or 0 for d in port_list)
        rate = sum(d.get('drop_rate_per_min') or 0 for d in port_list) / max(len(port_list), 1)
        dut_drops.append({
            'label': parent_dev.get('label') or parent_key,
            'resource_key': parent_key,
            'chassis_ip': parent_dev.get('mgmt_ip') or '',
            'total_drops': round(total),
            'ports_with_drops': len(port_list),
            'first_drop_at': first,
            'peak_interval_drops': round(peak),
            'drop_rate_per_min': round(rate, 2),
            'ports': sorted(port_list, key=lambda p: -(p.get('total_drops') or 0)),
        })
        for d in port_list:
            lbl = (d.get('iface_label') or '').lower().strip()
            if lbl:
                discard_label_set.add(lbl)

    # Flag PCPU ports that connect to a DUT port with discards.
    for dev in devices.values():
        if dev.get('profile') != PROFILE_KEYSIGHT_CHASSIS:
            continue
        for _pid, pcpu in (dev.get('pcpus') or {}).items():
            for port in pcpu.get('ports', []):
                lbl = (port.get('label') or '').lower().strip()
                port['has_dut_discard'] = lbl in discard_label_set

    for metric in rankings:
        rankings[metric] = sorted(rankings[metric], key=lambda x: x['value'], reverse=True)[:20]

    chassis_cpu_vals = [
        d['metrics'].get('cpu_pct', {}).get('latest')
        for d in devices.values()
        if d.get('profile') == PROFILE_KEYSIGHT_CHASSIS and d['metrics'].get('cpu_pct', {}).get('latest') is not None
    ]
    chassis_mem_vals = [
        d['metrics'].get('mem_pct', {}).get('latest')
        for d in devices.values()
        if d.get('profile') == PROFILE_KEYSIGHT_CHASSIS and d['metrics'].get('mem_pct', {}).get('latest') is not None
    ]

    summary = {
        'devices_total': len(node_keys),
        'ports_total': port_total,
        'ports_owned': owned_count,
        'ports_owned_pct': round(100.0 * owned_count / port_total, 1) if port_total else 0.0,
        'ports_with_traffic': traffic_count,
        'ports_traffic_pct': round(100.0 * traffic_count / port_total, 1) if port_total else 0.0,
        'ports_link_up': link_up_count,
        'ports_link_up_pct': round(100.0 * link_up_count / port_total, 1) if port_total else 0.0,
        'avg_chassis_cpu_pct': round(sum(chassis_cpu_vals) / len(chassis_cpu_vals), 1) if chassis_cpu_vals else None,
        'avg_chassis_mem_pct': round(sum(chassis_mem_vals) / len(chassis_mem_vals), 1) if chassis_mem_vals else None,
        'pcpu_hosts_hot': hot_pcpu,
        'waste_owned_idle': len(waste_signals),
        'hot_ports_cpu': sum(d['ports']['hot_cpu'] for d in devices.values()),
        'hot_ports_mem': sum(d['ports']['hot_mem'] for d in devices.values()),
    }

    heat_rows = []
    for rkey, dev in sorted(devices.items(), key=lambda x: x[1]['label']):
        m = dev.get('metrics') or {}
        discard_ports = [
            d for d in switch_input_discards
            if d.get('parent') == rkey and (d.get('total_drops') or 0) > 0
        ]
        if dev.get('profile') == PROFILE_ARISTA_SWITCH and discard_ports:
            dev['discard_summary'] = {
                'ports_with_drops': len(discard_ports),
                'total_drops': sum(d.get('total_drops') or 0 for d in discard_ports),
                'first_drop_at': min(
                    (d.get('first_drop_at') for d in discard_ports if d.get('first_drop_at')),
                    default=None,
                ),
            }
        heat_rows.append({
            'resource_key': rkey,
            'label': dev['label'],
            'mgmt_ip': dev.get('mgmt_ip') or '',
            'profile': dev.get('profile'),
            'cpu_pct': (m.get('cpu_pct') or {}).get('max'),
            'mem_pct': (m.get('mem_pct') or {}).get('max'),
            'owned_pct': round(100.0 * dev['ports']['owned'] / dev['ports']['total'], 1) if dev['ports']['total'] else 0,
            'traffic_pct': round(100.0 * dev['ports']['link_up'] / dev['ports']['total'], 1) if dev['ports']['total'] else 0,
            'idle_owned': dev['ports']['idle_owned'],
        })

    return {
        'summary': summary,
        'devices': list(devices.values()),
        'heat_matrix': heat_rows,
        'rankings': dict(rankings),
        'waste_signals': sorted(waste_signals, key=lambda w: w['label'])[:50],
        'link_errors': sorted(link_errors, key=lambda e: e['total'], reverse=True)[:50],
        'switch_input_discards': sorted(
            switch_input_discards, key=lambda d: d.get('total_drops') or 0, reverse=True,
        )[:50],
        'dut_drops': sorted(dut_drops, key=lambda d: d.get('total_drops') or 0, reverse=True)[:20],
        'pcpu_fleet': _flatten_pcpus(devices),
        'port_stress': port_stress,
    }


def _flatten_pcpus(devices: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for dev in devices.values():
        if dev.get('profile') != PROFILE_KEYSIGHT_CHASSIS:
            continue
        chassis_ixos = dev.get('ixos_version') or ''
        chassis_ixn = dev.get('ixnetwork_version') or ''
        chassis_apps = dict(dev.get('applications') or {})
        for _pid, pcpu in (dev.get('pcpus') or {}).items():
            port_details = pcpu.get('ports') or []
            out.append({
                'chassis': dev['label'],
                'chassis_ip': pcpu.get('chassis_ip') or dev.get('mgmt_ip') or '',
                'resource_key': dev['resource_key'],
                'mgmt_ip': pcpu.get('mgmt_ip'),
                'cpu_pct': pcpu.get('cpu_pct'),
                'mem_pct': pcpu.get('mem_pct'),
                'bps_in_peak': pcpu.get('bps_in_peak'),
                'bps_out_peak': pcpu.get('bps_out_peak'),
                'bps_in_total': round(pcpu.get('bps_in_total') or 0.0, 2),
                'bps_out_total': round(pcpu.get('bps_out_total') or 0.0, 2),
                'bps_in_avg_total': round(pcpu.get('bps_in_avg_total') or 0.0, 2),
                'bps_out_avg_total': round(pcpu.get('bps_out_avg_total') or 0.0, 2),
                'poll_mode': bool(pcpu.get('poll_mode')),
                'port_count': pcpu.get('port_count', 0),
                'owned_count': pcpu.get('owned_count', 0),
                'traffic_count': pcpu.get('traffic_count', 0),
                'owners': pcpu.get('owners') or [],
                'ports': port_details,
                'ixos_version': chassis_ixos or pcpu.get('ixos_version') or '',
                'ixnetwork_version': chassis_ixn or pcpu.get('ixnetwork_version') or '',
                'applications': chassis_apps or dict(pcpu.get('applications') or {}),
            })
    out.sort(key=lambda x: (x.get('cpu_pct') or 0, x.get('mem_pct') or 0), reverse=True)
    return out


def _activity_summary(events: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    by_type: Dict[str, int] = defaultdict(int)
    total = 0
    for evs in events.values():
        for ev in evs:
            by_type[ev.get('event_type', 'unknown')] += 1
            total += 1
    return {
        'total_events': total,
        'by_type': dict(sorted(by_type.items(), key=lambda x: -x[1])),
    }


def _count_series_points(buckets: Dict[str, Dict[str, List[List[Any]]]], metric: str) -> int:
    total = 0
    for mb in buckets.values():
        total += len(mb.get(metric) or [])
    return total


def _data_health(
    buckets: Dict[str, Dict[str, List[List[Any]]]],
    catalog: Dict[str, Dict[str, Any]],
    events: Dict[str, List[Dict[str, Any]]],
    pcpu_fleet: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Describe which data families are actually populated so the UI can explain
    empty charts instead of rendering a confusing blank canvas."""
    chassis_keys = [k for k, v in catalog.items() if v.get('profile') == PROFILE_KEYSIGHT_CHASSIS]
    signals = {
        'chassis_cpu': sum(_count_series_points({k: buckets.get(k, {})}, 'cpu_pct') for k in chassis_keys) > 0,
        'chassis_mem': sum(_count_series_points({k: buckets.get(k, {})}, 'mem_pct') for k in chassis_keys) > 0,
        'port_traffic': (_count_series_points(buckets, 'bps_in') + _count_series_points(buckets, 'bps_out')) > 0,
        'port_ownership': _count_series_points(buckets, 'port_ownership') > 0,
        'link_errors': (_count_series_points(buckets, 'crc_errors') + _count_series_points(buckets, 'alignment_errors')) > 0,
        'switch_discards': _count_series_points(buckets, 'input_discards') > 0,
        'pcpu': len(pcpu_fleet) > 0,
        'events': any(events.values()),
    }
    missing = [k for k, present in signals.items() if not present]
    notes: List[str] = []
    if not signals['port_traffic']:
        notes.append('No port throughput collected yet (bps_in/out) — traffic charts will be flat. '
                     'AresONE/KCOS port stats populate this once the collector polls owned ports.')
    if not signals['pcpu']:
        notes.append('No PCPU samples — AresONE root SSH (10.0.x.x) has not reported port CPU/mem.')
    if not signals['chassis_cpu'] and not signals['chassis_mem']:
        notes.append('No chassis CPU/mem yet — run the metrics collector for this topology.')
    return {
        'has_any': any(signals.values()),
        'signals': signals,
        'missing': missing,
        'notes': notes,
        'catalog_size': len(catalog),
    }


def _annotate_empty_reason(health: Dict[str, Any], topo_id: int) -> None:
    """When no data exists, explain *why* so the empty state is actionable."""
    from connect.metric_collectors import collector_mode, topology_has_collectable_nodes

    if not topology_has_collectable_nodes(topo_id):
        health['reason'] = 'no_collectable_nodes'
        health['notes'].insert(
            0,
            'No collectable nodes linked in this topology — map chassis '
            '(node extra.chassis_id) or device FKs so the collector can poll them.',
        )
        return

    mode = collector_mode()
    health['reason'] = 'collector_not_writing'
    if mode == 'seeded':
        health['notes'].insert(
            0,
            'Seeded collector mode is on but no samples yet — the collector '
            'process may not have run its first tick.',
        )
    else:
        health['notes'].insert(
            0,
            'Nodes are linked but no samples yet — confirm the metrics collector '
            'is running (labvault-collector) and chassis/device credentials are valid.',
        )


def empty_insights_payload(topo_id: int, window_from, window_to, *, bucket: Optional[str] = None) -> Dict[str, Any]:
    """A fully-shaped, safe payload used when data is absent or a build fails."""
    hours = (window_to - window_from).total_seconds() / 3600.0
    bucket = bucket or _bucket_for_window(hours)
    return {
        'schema_version': 2,
        'topology_id': topo_id,
        'ok': True,
        'window': {
            'from': window_from.isoformat(),
            'to': window_to.isoformat(),
            'bucket': bucket,
            'hours': round(hours, 2),
        },
        'health': {'has_any': False, 'signals': {}, 'missing': [], 'notes': [
            'No metrics in this window yet — the collector may not have run for this topology.',
        ], 'catalog_size': 0},
        'research_notes': _research_notes(),
        'summary': {
            'devices_total': 0, 'ports_total': 0, 'ports_owned': 0, 'ports_owned_pct': 0.0,
            'ports_with_traffic': 0, 'ports_traffic_pct': 0.0, 'avg_chassis_cpu_pct': None,
            'avg_chassis_mem_pct': None, 'pcpu_hosts_hot': 0, 'waste_owned_idle': 0,
            'hot_ports_cpu': 0, 'hot_ports_mem': 0,
        },
        'devices': [],
        'heat_matrix': [],
        'rankings': {},
        'waste_signals': [],
        'link_errors': [],
        'switch_input_discards': [],
        'pcpu_fleet': [],
        'time_series': {'fleet': {'cpu_pct': [], 'mem_pct': [], 'bps_total': [], 'ports_owned_pct': []}, 'devices': []},
        'stress_graph': {'nodes': [], 'edges': [], 'meta': {'node_count': 0, 'edge_count': 0, 'bridge_devices': 0}},
        'bottlenecks': [],
        'activity': {'total_events': 0, 'by_type': {}},
    }


def build_usage_insights(
    topo_id: int,
    window_from,
    window_to,
    *,
    bucket: Optional[str] = None,
) -> Dict[str, Any]:
    hours = (window_to - window_from).total_seconds() / 3600.0
    bucket = bucket or _bucket_for_window(hours)

    # Designer catalog only — do not DISTINCT the timeseries store on the
    # request path. Observed ports are merged from the bucket keys.
    catalog = build_enriched_resource_catalog(
        topo_id, live_ports=False, observe_db=False,
    )
    raw_buckets = get_metric_buckets(topo_id, window_from, window_to, bucket=bucket)
    apply_observed_keys_to_catalog(catalog, raw_buckets.keys())
    allowed = catalog_resource_keys(catalog)
    metric_buckets = restrict_metric_buckets(raw_buckets, allowed)
    # Activity events live in the same timeseries store as raw samples.
    # Scanning them on the request path wedges gunicorn. Skip here.
    events: Dict[str, Any] = {}

    core = _insight_from_buckets(
        metric_buckets, catalog, topo_id=topo_id, events=events,
        window_minutes=max(hours * 60.0, 1.0),
        window_to=window_to,
    )
    devices = core['devices']
    port_stress = core.pop('port_stress', {})
    health = _data_health(metric_buckets, catalog, events, core['pcpu_fleet'])
    if not health.get('has_any'):
        _annotate_empty_reason(health, topo_id)
    payload = {
        'schema_version': 2,
        'topology_id': topo_id,
        'ok': True,
        'window': {
            'from': window_from.isoformat(),
            'to': window_to.isoformat(),
            'bucket': bucket,
            'hours': round(hours, 2),
        },
        'health': health,
        'research_notes': _research_notes(),
        **core,
        'time_series': {
            'fleet': _fleet_time_series(metric_buckets, catalog),
            'devices': _device_time_series(metric_buckets, devices),
        },
        'stress_graph': _build_stress_graph(
            topo_id,
            devices,
            core['rankings'],
            port_stress,
        ),
        'bottlenecks': _detect_bottlenecks(
            core['summary'],
            devices,
            core['pcpu_fleet'],
            core['waste_signals'],
            core['rankings'],
            port_stress,
            core.get('link_errors'),
            core.get('switch_input_discards'),
        ),
        'activity': _activity_summary(events),
    }
    return compact_pulse_payload(payload)


_PORT_KEEP = (
    'label', 'resource_key', 'owner', 'owned', 'link_up', 'speed_gbps',
    'bps_in', 'bps_out', 'bps_peak', 'cpu_pct', 'mem_pct',
    'has_traffic', 'has_dut_discard', 'last_sample_at', 'fresh',
)


def _compact_port(port: Any) -> Any:
    if not isinstance(port, dict):
        return port
    out = {k: port.get(k) for k in _PORT_KEEP if port.get(k) is not None}
    if 'owned' in port:
        out['owned'] = bool(port.get('owned'))
    if 'fresh' in port:
        out['fresh'] = bool(port.get('fresh'))
    if 'label' in port:
        out['label'] = port['label']
    return out


def compact_pulse_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Drop duplicated / unused Pulse fields so the browser is not waiting on 300KB+ JSON."""
    if not isinstance(payload, dict):
        return payload
    for dev in payload.get('devices') or []:
        if isinstance(dev, dict):
            dev.pop('pcpus', None)
    seen_apps: set = set()
    for rg in payload.get('pcpu_fleet') or []:
        if not isinstance(rg, dict):
            continue
        rg.pop('sample_ports', None)
        chassis = rg.get('resource_key') or rg.get('chassis')
        if chassis in seen_apps:
            rg.pop('applications', None)
        else:
            seen_apps.add(chassis)
        ports = rg.get('ports')
        if isinstance(ports, list):
            rg['ports'] = [_compact_port(p) for p in ports]
    payload['dut_drops'] = [
        d for d in (payload.get('dut_drops') or [])
        if isinstance(d, dict) and not str(d.get('resource_key') or '').startswith('dummy')
    ]
    return payload


def _research_notes() -> List[Dict[str, str]]:
    """Short UX research blurbs shown in the Lab Pulse intro panel."""
    return [
        {
            'title': 'Fleet pulse',
            'body': 'Headline KPIs answer “is the lab busy or hoarding?” — ownership % vs traffic % is the fastest mismatch signal.',
        },
        {
            'title': 'Heat matrix',
            'body': 'Row-normalized device comparison (NOC wall pattern) surfaces one hot chassis without reading every timeline lane.',
        },
        {
            'title': 'PCPU board',
            'body': 'AresONE port CPU/mem comes from internal 10.0.x.x packet CPUs — the hardware truth behind port lanes.',
        },
        {
            'title': 'Waste radar',
            'body': 'Owned + idle ports are the classic lab eye-opener: reserved capacity burning power and blocking others.',
        },
        {
            'title': 'Rankings',
            'body': 'Pareto top consumers show where to profile next — align with Keysight/BreakingPoint run hot spots.',
        },
        {
            'title': 'Time series',
            'body': 'Bucketed trends answer when stress spiked — peaks alone hide bursts that explain failed LaaS runs.',
        },
        {
            'title': 'Stress graph',
            'body': 'Graph view maps devices, hot ports, and fabric links so you see dependencies and bridge nodes, not isolated KPIs.',
        },
        {
            'title': 'Bottlenecks',
            'body': 'Scored findings tie metrics to LaaS actions: release idle reservations, rotate ports, or split traffic before the next run.',
        },
        {
            'title': 'Arista input discards',
            'body': (
                'EOS input discards on fabric Ethernet ports indicate switch-side buffer/QoS drops — '
                'compare with RoCEv2 retransmit/frame deltas on the traffic generator during long CMRI runs.'
            ),
        },
        {
            'title': 'DUT packet drops',
            'body': (
                'Per-DUT switch packet-drop dashboard groups Arista input discards by parent switch, '
                'shows first-drop timing, peak interval, and per-minute rate — cross-linked to the '
                'connected PCPU port for retransmit correlation during long CMRI RoCEv2 runs.'
            ),
        },
    ]
