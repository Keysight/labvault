"""Per-device metric collectors for lab topology timeline.

One collect cycle (``collect_all_topologies``) polls every node of each topology with
``metrics_collection_enabled=True`` and writes to ``np_timeseries``:

- chassis (IxOS/KCOS REST): cpu/mem, per-port ownership, link speed, bps, PCPU cpu/mem;
- Arista/SONiC switches: cpu/mem, per-interface bps and (Arista) input-discard deltas;
- OCS: crossconnect diffs as ``patch_*`` / ``link_*`` events only (no samples).

Driven by ``manage.py run_metric_collector`` (systemd/compose, 60 s) or
``collect_topology_metrics``. ``CollectorState`` carries counter baselines between ticks;
only byte-counter baselines survive a process restart. ``seed_*`` helpers write synthetic
rows for demos and are not used in ``idle``/``live`` mode unless
``LABVAULT_COLLECTOR_SWITCH_DISCARDS_SEEDED`` is set.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from django.db import close_old_connections
from django.utils import timezone

from connect.drivers import get_driver as get_switch_driver
from connect.keysight_drivers import get_driver as get_chassis_driver
from connect.lab_metrics import (
    EventWriteBuffer,
    bulk_write_metrics,
    close_open_events,
    open_resource_event,
    upsert_hourly_rollups,
)
from connect.models import KeysightChassis, LabMetricSample, LabTopology, LabTopologyNode
from connect.topology_resource_catalog import (
    build_switch_counter_aliases,
    chassis_port_metric_label,
    node_resource_key,
    port_resource_key,
    pcpu_mgmt_ip_from_port,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Collector runtime configuration (mode / allowlist) and node collectability.
# These make first-deploy behavior deterministic without manual intervention.
# ---------------------------------------------------------------------------

def collector_mode() -> str:
    """'idle' (healthy no-op), 'live' (poll gear), or legacy 'seeded' (rejected on customer SKU)."""
    try:
        from connect.runtime_settings import get_setting
        mode = str(get_setting('collector_mode') or 'idle').strip().lower()
    except Exception:
        mode = (os.environ.get('LABVAULT_COLLECTOR_MODE') or 'idle').strip().lower()
    if mode == 'seeded':
        # Customer SKU: never synthesize demo rows
        return 'idle'
    if mode in ('idle', 'live'):
        return mode
    return 'idle'


def collector_topology_allowlist() -> Optional[Set[int]]:
    """Optional LABVAULT_COLLECTOR_TOPOLOGY_IDS allowlist (comma/space separated)."""
    raw = (os.environ.get('LABVAULT_COLLECTOR_TOPOLOGY_IDS') or '').strip()
    if not raw:
        return None
    ids: Set[int] = set()
    for part in raw.replace(';', ',').replace(' ', ',').split(','):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids or None


def collector_switch_discards_seeded() -> bool:
    """When set, synthesize Arista input-discard timeseries (no SSH) for the demo."""
    return (os.environ.get('LABVAULT_COLLECTOR_SWITCH_DISCARDS_SEEDED') or '').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )


def collector_node_types() -> Optional[Set[str]]:
    """Optional LABVAULT_COLLECTOR_NODE_TYPES filter (e.g. ``chassis``).

    Chassis (AresONE/IxOS) collect over REST only. Switch (Arista/SONiC) and OCS
    (TL1) drivers use SSH, so restricting to ``chassis`` keeps collection API-only.
    """
    raw = (os.environ.get('LABVAULT_COLLECTOR_NODE_TYPES') or '').strip()
    if not raw:
        return None
    types = {t.strip().lower() for t in raw.replace(';', ',').split(',') if t.strip()}
    return types or None


def topology_node_mgmt_ip(node: LabTopologyNode) -> str:
    """Management IP from node extra, then the linked Device row."""
    extra = node.extra or {}
    for key in ('device_ip', 'mgmt_ipv4', 'mgmt_display'):
        val = str(extra.get(key) or '').strip()
        if val:
            return val
    device = getattr(node, 'device', None)
    if device is not None:
        return (device.ip_address or '').strip()
    return ''


def resolve_topology_chassis(node: LabTopologyNode) -> Optional[KeysightChassis]:
    """Resolve a topology node to a KeysightChassis after dataset import.

    Export extras often keep a source-DB ``chassis_id`` that does not exist
    here, or omit it entirely when the same IP is also a Device row.
    """
    extra = node.extra or {}
    chassis_id = extra.get('chassis_id')
    if chassis_id:
        chassis = KeysightChassis.objects.filter(pk=chassis_id).first()
        if chassis:
            return chassis
    ip = topology_node_mgmt_ip(node)
    if ip:
        return KeysightChassis.objects.filter(ip_address=ip).first()
    return None


def bind_topology_chassis(node: LabTopologyNode, chassis: KeysightChassis) -> None:
    """Persist the live chassis PK so later ticks and Insights stay bound."""
    extra = dict(node.extra or {})
    changed = False
    if extra.get('chassis_id') != chassis.pk:
        extra['chassis_id'] = chassis.pk
        changed = True
    if chassis.chassis_type and extra.get('chassis_type') != chassis.chassis_type:
        extra['chassis_type'] = chassis.chassis_type
        changed = True
    ip = (chassis.ip_address or '').strip()
    if ip and extra.get('device_ip') != ip:
        extra['device_ip'] = ip
        changed = True
    if not changed:
        return
    node.extra = extra
    node.save(update_fields=['extra'])


def node_is_collectable(node: LabTopologyNode) -> bool:
    """True when a node resolves to a real chassis/device the collector can poll.

    Reachability/credentials are not checked here — only that the topology node
    is linked to something concrete (so empty imported LLDP nodes are skipped).
    """
    extra = node.extra or {}
    if node.node_type == 'chassis' or extra.get('chassis_id'):
        return resolve_topology_chassis(node) is not None
    if node.node_type == 'ocs':
        return node.device_id is not None
    if node.node_type == 'switch':
        if not node.device_id or not node.device:
            return False
        vendor = (node.device.vendor_type or '').lower()
        return vendor in ('arista', 'sonic')
    return False


def topology_has_collectable_nodes(topo_id: int) -> bool:
    """True when at least one node passes :func:`node_is_collectable` (no network I/O)."""
    nodes = LabTopologyNode.objects.filter(topology_id=topo_id).select_related('device')
    return any(node_is_collectable(n) for n in nodes)


def _rkey_seed(resource_key: str) -> int:
    return sum(ord(c) for c in (resource_key or 'x'))


def _seeded_value(metric: str, seed: int, tick: int) -> float:
    """Deterministic, plausible synthetic value so demo charts look alive."""
    phase = (seed % 17) / 17.0 * (2 * math.pi)
    wave = (math.sin((tick / 6.0) + phase) + 1.0) / 2.0  # 0..1
    if metric == 'cpu_pct':
        # Packet-CPU load: spread 20..95 so some boards read "hot" (>75%).
        return round(20.0 + wave * 75.0, 2)
    if metric == 'mem_pct':
        return round(30.0 + wave * 50.0, 2)      # 30..80 %
    if metric in ('bps_in', 'bps_out'):
        return round((2.0 + wave * 30.0) * 1e9, 2)  # 2..32 Gbps
    if metric == 'port_ownership':
        return 1.0 if (seed + tick) % 3 else 0.0
    if metric == 'link_speed_gbps':
        return 100.0
    if metric == 'input_discards':
        # Occasional small per-interval drops (mostly zero) for switch panels.
        return round(wave * 12.0) if (seed + tick) % 4 == 0 else 0.0
    return round(wave * 100.0, 2)


def seed_topology_metrics(
    topo_id: int,
    *,
    backfill_points: int = 24,
    spacing_seconds: int = 300,
    now: Optional[datetime] = None,
    max_ports_per_device: int = 8,
    max_switch_ports: int = 6,
    max_total_ports: int = 160,
) -> int:
    """Write synthetic LabMetricSample rows for a topology (seeded/demo mode).

    Seeds ports PER device (not a single global cap) so EVERY chassis shows on
    the PCPU board. Keysight ports get per-port cpu_pct/mem_pct (packet-CPU load),
    plus link_speed_gbps and traffic. A per-chassis PCPU management-IP map is
    written to the cache that ``attach_mgmt_ips_to_catalog`` reads, so even
    chassis whose port labels do not encode a card.port (e.g. ``port_1``) still
    group into a board. Never touches the network.
    """
    from collections import defaultdict

    from connect.cache_utils import cache_set
    from connect.topology_resource_catalog import (
        PORT_PCPU_MGMT_CACHE_TTL,
        PROFILE_ARISTA_SWITCH,
        PROFILE_KEYSIGHT_CHASSIS,
        PROFILE_SONIC_SWITCH,
        build_resource_catalog,
        infer_pcpu_mgmt_ip_from_label,
        invalidate_resource_catalog_cache,
    )

    now = now or timezone.now()
    catalog = build_resource_catalog(topo_id, live_ports=False)
    if not catalog:
        return 0

    device_targets: List[Tuple[str, List[str]]] = []
    parent_profile: Dict[str, str] = {}
    ports_by_parent: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for rkey, entry in catalog.items():
        profile = entry.get('profile')
        if profile in (PROFILE_KEYSIGHT_CHASSIS, PROFILE_ARISTA_SWITCH, PROFILE_SONIC_SWITCH):
            device_targets.append((rkey, ['cpu_pct', 'mem_pct']))
            parent_profile[rkey] = profile
        elif profile in ('keysight_port', 'switch_port', 'ocs_port'):
            parent = entry.get('parent')
            if parent:
                ports_by_parent[parent].append((rkey, entry.get('label') or '', profile))

    port_targets: List[Tuple[str, List[str]]] = []
    pcpu_cache_by_node: Dict[int, Dict[str, str]] = defaultdict(dict)
    total_ports = 0
    for parent, plist in ports_by_parent.items():
        if total_ports >= max_total_ports:
            break
        prof = parent_profile.get(parent)
        if prof == PROFILE_KEYSIGHT_CHASSIS:
            cap, metrics = max_ports_per_device, [
                'bps_in', 'bps_out', 'port_ownership',
                'cpu_pct', 'mem_pct', 'link_speed_gbps',
            ]
        elif prof in (PROFILE_ARISTA_SWITCH, PROFILE_SONIC_SWITCH):
            cap, metrics = max_switch_ports, ['bps_in', 'bps_out', 'input_discards']
        else:
            cap, metrics = max_switch_ports, ['bps_in', 'bps_out', 'port_ownership']
        try:
            node_pk = int(parent.split('_', 1)[1])
        except (IndexError, ValueError):
            node_pk = None
        count = 0
        for idx, (prkey, label, _pprofile) in enumerate(plist):
            if count >= cap or total_ports >= max_total_ports:
                break
            port_targets.append((prkey, metrics))
            count += 1
            total_ports += 1
            if prof == PROFILE_KEYSIGHT_CHASSIS and node_pk is not None:
                mgmt = infer_pcpu_mgmt_ip_from_label(label) or f'10.0.{(node_pk % 250) + 1}.{idx + 1}'
                pcpu_cache_by_node[node_pk][prkey] = mgmt

    # Publish per-chassis PCPU mgmt-IP maps so the insights catalog attributes
    # each board to its chassis (read by attach_mgmt_ips_to_catalog).
    for node_pk, node_map in pcpu_cache_by_node.items():
        if node_map:
            cache_set(f'pcpu_mgmt_ip:{topo_id}:{node_pk}', node_map, PORT_PCPU_MGMT_CACHE_TTL)
    if pcpu_cache_by_node:
        invalidate_resource_catalog_cache(topo_id)

    targets = device_targets + port_targets
    if not targets:
        return 0

    backfill_points = max(1, backfill_points)
    samples: List[LabMetricSample] = []
    for i in range(backfill_points):
        ts = now - timedelta(seconds=spacing_seconds * (backfill_points - 1 - i))
        for rkey, metrics in targets:
            seed = _rkey_seed(rkey)
            for metric in metrics:
                samples.append(LabMetricSample(
                    topology_id=topo_id,
                    resource_key=rkey,
                    metric=metric,
                    value=_seeded_value(metric, seed, i),
                    sampled_at=ts,
                ))

    written = bulk_write_metrics(samples)
    try:
        upsert_hourly_rollups(topo_id, samples)
    except Exception:
        logger.warning('seed rollup upsert failed topo=%s', topo_id, exc_info=True)
    return written


def _arista_switch_discard_ports(topo_id: int, max_ports_per_switch: int = 8) -> List[Tuple[str, str, str]]:
    """Return (port_rkey, port_label, switch_rkey) for Arista switch ports in the catalog."""
    from collections import defaultdict

    from connect.topology_resource_catalog import (
        PROFILE_ARISTA_SWITCH,
        build_resource_catalog,
    )

    catalog = build_resource_catalog(topo_id, live_ports=False)
    arista_parents = {k for k, v in catalog.items() if v.get('profile') == PROFILE_ARISTA_SWITCH}
    by_parent: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for rkey, entry in catalog.items():
        if entry.get('profile') == 'switch_port' and entry.get('parent') in arista_parents:
            by_parent[entry['parent']].append((rkey, entry.get('label') or ''))
    out: List[Tuple[str, str, str]] = []
    for parent, plist in by_parent.items():
        for prkey, label in plist[:max_ports_per_switch]:
            out.append((prkey, label, parent))
    return out


def seed_switch_discards(
    topo_id: int,
    *,
    now: Optional[datetime] = None,
    backfill_points: int = 1,
    spacing_seconds: int = 300,
    state: Optional['CollectorState'] = None,
) -> int:
    """Synthesize Arista input-discard timeseries for the demo (no SSH).

    Writes per-interval ``input_discards`` deltas across every Arista switch port
    so the Lab Pulse explicit (per-port) table and combined (per-DUT ``dut_drops``)
    view populate. Maintains a running counter with occasional resets and emits
    marker events (``input_discard`` bursts, ``counter_reset``) for the timeline.
    """
    from connect.lab_metrics import bulk_write_events
    from connect.models import LabResourceEvent

    now = now or timezone.now()
    targets = _arista_switch_discard_ports(topo_id)
    if not targets:
        return 0

    samples: List[LabMetricSample] = []
    events: List[LabResourceEvent] = []
    base_tick = int(now.timestamp() // max(spacing_seconds, 1))

    for prkey, label, _parent in targets:
        seed = _rkey_seed(prkey)
        ckey = f'{prkey}::disc'
        if state is not None:
            with state._lock:
                cum = float((state.if_counters.get(ckey) or {}).get('c', 0.0))
        else:
            cum = 0.0
        for i in range(backfill_points):
            # Global tick index so consecutive live cycles keep varying.
            tick = (base_tick - (backfill_points - 1 - i)) if backfill_points > 1 else base_tick
            ts = now - timedelta(seconds=spacing_seconds * (backfill_points - 1 - i))
            phase = (seed % 13) / 13.0 * (2 * math.pi)
            wave = (math.sin(tick / 4.0 + phase) + 1.0) / 2.0
            if (seed + tick) % 20 == 0:
                # Counter reset (ifCounters cleared) — marker, no delta this interval.
                cum = 0.0
                events.append(LabResourceEvent(
                    topology_id=topo_id, resource_key=prkey, event_type='counter_reset',
                    started_at=ts, ended_at=ts,
                    payload={'iface': label, 'reason': 'ifCounters cleared'},
                ))
                continue
            if (seed + tick) % 5 == 0:
                continue  # quiet interval (no drops)
            delta = float(round(wave * wave * 400))  # bursty 0..400 drops / interval
            if delta <= 0:
                continue
            cum += delta
            samples.append(LabMetricSample(
                topology_id=topo_id, resource_key=prkey, metric='input_discards',
                value=delta, sampled_at=ts,
            ))
            if delta >= 200:  # notable burst → timeline marker
                events.append(LabResourceEvent(
                    topology_id=topo_id, resource_key=prkey, event_type='input_discard',
                    started_at=ts, ended_at=ts,
                    payload={'iface': label, 'delta': delta, 'counter': cum},
                ))
        if state is not None:
            with state._lock:
                state.if_counters[ckey] = {'c': cum}

    written = bulk_write_metrics(samples)
    if written:
        try:
            upsert_hourly_rollups(topo_id, samples)
        except Exception:
            logger.warning('switch discard rollup upsert failed topo=%s', topo_id, exc_info=True)
    if events:
        try:
            bulk_write_events(events)
        except Exception:
            logger.warning('switch discard events write failed topo=%s', topo_id, exc_info=True)
    return written


# Survive collector restarts — without this every reboot writes 0 bps until the next poll.
_IF_COUNTER_CACHE_KEY = 'collector:if_counters:v1'
_IF_COUNTER_CACHE_TTL = 86400
_IF_COUNTER_FILE_NAME = 'collector_if_counters.json'


def _if_counter_blob_path() -> Path:
    from django.conf import settings

    var_dir = Path(settings.BASE_DIR) / 'var'
    var_dir.mkdir(parents=True, exist_ok=True)
    return var_dir / _IF_COUNTER_FILE_NAME


def _read_if_counter_blob() -> Optional[dict]:
    """Load baselines from Django cache, falling back to var/ JSON on disk."""
    from connect.cache_utils import cache_get

    blob = cache_get(_IF_COUNTER_CACHE_KEY)
    if isinstance(blob, dict) and isinstance(blob.get('counters'), dict) and blob['counters']:
        return blob
    path = _if_counter_blob_path()
    if not path.is_file():
        return None
    try:
        with path.open(encoding='utf-8') as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning('if_counters file read failed: %s', exc)
        return None


def _write_if_counter_blob(blob: dict) -> None:
    """Persist baselines to cache and var/ JSON (LocMem does not survive process exit)."""
    from connect.cache_utils import cache_set

    cache_set(_IF_COUNTER_CACHE_KEY, blob, _IF_COUNTER_CACHE_TTL)
    path = _if_counter_blob_path()
    tmp = path.with_suffix('.json.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as fh:
            json.dump(blob, fh)
        tmp.replace(path)
    except OSError as exc:
        logger.warning('if_counters file write failed: %s', exc)


class CollectorState:
    """In-memory state between collector runs (daemon loop)."""

    def __init__(self):
        self.port_owners: Dict[str, str] = {}
        self.port_link_state: Dict[str, bool] = {}
        self.if_counters: Dict[str, Dict[str, Any]] = {}
        self.if_counter_ts: Dict[str, datetime] = {}
        self.ocs_xconns: Dict[int, Dict[str, dict]] = {}
        self.chassis_drivers: Dict[int, Any] = {}
        self.event_buffer = EventWriteBuffer()
        self._lock = threading.Lock()

    def load_if_counters(self) -> None:
        """Restore byte-counter baselines from cache or disk (daemon restart / one-shot)."""
        from django.utils.dateparse import parse_datetime

        blob = _read_if_counter_blob()
        if not isinstance(blob, dict):
            return
        counters = blob.get('counters')
        ts_map = blob.get('ts')
        with self._lock:
            if isinstance(counters, dict):
                self.if_counters.update(counters)
            if isinstance(ts_map, dict):
                for prkey, raw_ts in ts_map.items():
                    if not raw_ts:
                        continue
                    dt = parse_datetime(str(raw_ts))
                    if dt is None:
                        continue
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt, timezone.get_current_timezone())
                    self.if_counter_ts[prkey] = dt

    def persist_if_counters(self) -> None:
        """Persist byte-counter baselines for the next collector run."""
        with self._lock:
            ts_map = {
                prkey: ts.isoformat()
                for prkey, ts in self.if_counter_ts.items()
                if ts is not None
            }
            _write_if_counter_blob(
                {'counters': dict(self.if_counters), 'ts': ts_map},
            )


def _counter_delta(prev: float, cur: float) -> float:
    """Non-negative delta; if counter decreased assume clear/reset."""
    if cur < prev:
        return max(0.0, cur)
    return max(0.0, cur - prev)


def _mem_pct(health: dict) -> float:
    total = float(health.get('memory_total') or 0)
    used = float(health.get('memory_used') or 0)
    if health.get('memory_percent') is not None:
        return float(health['memory_percent'])
    return round(used / total * 100, 2) if total else 0.0


def _chassis_port_label(port: dict) -> str:
    return chassis_port_metric_label(port)


def _parse_port_bitrate(stat: dict) -> Tuple[float, float]:
    """Best-effort parse of IxOS /portstats row → (bps_in, bps_out)."""
    rx_byte_keys = ('rxBytesRate', 'rxByteRate', 'receiveBytesRate')
    tx_byte_keys = ('txBytesRate', 'txByteRate', 'transmitBytesRate')
    rx_bit_keys = ('rxBitRate', 'rxRate', 'receiveBitRate', 'rxBitrate')
    tx_bit_keys = ('txBitRate', 'txRate', 'transmitBitRate', 'txBitrate')

    def _pick(keys: tuple[str, ...], byte_keys: frozenset[str]) -> Tuple[float, bool]:
        for k in keys:
            if k in stat and stat[k] is not None:
                return float(stat[k]), k in byte_keys
        return 0.0, False

    rx_byte_set = frozenset(rx_byte_keys)
    tx_byte_set = frozenset(tx_byte_keys)
    rx, rx_bytes = _pick(rx_byte_keys + rx_bit_keys, rx_byte_set)
    tx, tx_bytes = _pick(tx_byte_keys + tx_bit_keys, tx_byte_set)
    if rx_bytes:
        rx *= 8.0
    if tx_bytes:
        tx *= 8.0
    return rx, tx


def _port_memory_kb(port: dict) -> Optional[float]:
    """IxOS /ports ``portMemory`` field (kilobytes allocated to the port NP)."""
    raw = port.get('port_memory_kb')
    if raw is None:
        raw = port.get('portMemory')
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _ixos_port_mem_pct(port: dict, max_mem_kb: float) -> Optional[float]:
    mem_kb = _port_memory_kb(port)
    if mem_kb is None or not max_mem_kb:
        return None
    return round(mem_kb / max_mem_kb * 100.0, 2)


def _parse_port_util_pct(stat: dict) -> Tuple[Optional[float], Optional[float]]:
    """Parse optional per-port CPU/memory % from IxOS/KCOS portstats rows."""
    cpu_keys = (
        'cpuUsagePercent', 'cpuPercent', 'cpu_utilization', 'processorLoad',
        'portCpuPercent', 'portCpuUsage',
    )
    mem_keys = (
        'memoryUsagePercent', 'memPercent', 'memory_percent', 'memUsagePercent',
        'portMemPercent', 'portMemoryUsage',
    )
    cpu = None
    mem = None
    for k in cpu_keys:
        if k in stat and stat[k] is not None:
            try:
                cpu = float(stat[k])
            except (TypeError, ValueError):
                pass
            break
    for k in mem_keys:
        if k in stat and stat[k] is not None:
            try:
                mem = float(stat[k])
            except (TypeError, ValueError):
                pass
            break
    return cpu, mem


def _index_port_stats(
    stats_raw: list,
) -> Tuple[Dict[Any, dict], Dict[Tuple[Any, Any], dict], Dict[Any, dict]]:
    """Index /portstats rows by stat id, (card, port), and parent port id.

    AresONE returns cumulative byte counters keyed by ``parentId`` → ``/ports`` id
    (not cardNumber/portNumber). XGS often expose ``rxBitRate`` on the row.
    """
    by_id: Dict[Any, dict] = {}
    by_card_port: Dict[Tuple[Any, Any], dict] = {}
    by_parent_id: Dict[Any, dict] = {}
    for s in stats_raw:
        if not isinstance(s, dict):
            continue
        pid = s.get('id')
        if pid is not None:
            by_id[pid] = s
        parent_id = s.get('parentId', s.get('parent_id'))
        if parent_id is not None:
            by_parent_id[parent_id] = s
        cn = s.get('cardNumber', s.get('card_number'))
        pn = s.get('portNumber', s.get('port_number'))
        if cn is not None and pn is not None:
            by_card_port[(cn, pn)] = s
    return by_id, by_card_port, by_parent_id


def _lookup_port_stat(
    port: dict,
    by_id: Dict[Any, dict],
    by_card_port: Dict[Tuple[Any, Any], dict],
    by_parent_id: Optional[Dict[Any, dict]] = None,
) -> Optional[dict]:
    pid = port.get('id')
    if pid is not None:
        if pid in by_id:
            return by_id[pid]
        if by_parent_id and pid in by_parent_id:
            return by_parent_id[pid]
    cn = port.get('card_number')
    pn = port.get('port_number')
    if cn is not None and pn is not None:
        hit = by_card_port.get((cn, pn))
        if hit is not None:
            return hit
    return None


def _parse_port_byte_counters(stat: dict) -> Tuple[Optional[float], Optional[float]]:
    """Cumulative byte counters from IxOS /portstats (AresONE-style)."""
    rx_keys = ('bytesReceived', 'bytes_received', 'rxBytes', 'rx_bytes')
    tx_keys = ('bytesSent', 'bytes_sent', 'txBytes', 'tx_bytes')
    rx = tx = None
    for k in rx_keys:
        if k in stat and stat[k] is not None:
            try:
                rx = float(stat[k])
            except (TypeError, ValueError):
                pass
            break
    for k in tx_keys:
        if k in stat and stat[k] is not None:
            try:
                tx = float(stat[k])
            except (TypeError, ValueError):
                pass
            break
    return rx, tx


# Upper bound for a single port sample (800 Gbps line rate).
_MAX_SANE_PORT_BPS = 800e9
# Lab Pulse: counter glitches often land in Gbps; do not persist above 1 Gbps.
_MAX_PULSE_WRITE_BPS = 1e9


def _bps_from_byte_counter_delta(
    *,
    prkey: str,
    bytes_in: float,
    bytes_out: float,
    now: datetime,
    state: CollectorState,
) -> Tuple[float, float, bool]:
    """Derive bps from successive cumulative byte counter samples.

    Returns (bps_in, bps_out, measured). ``measured`` is False on the first
    sample, after a counter reset, or when the delta is implausible — callers
    should not persist 0 bps in those cases (it overwrites good history).
    """
    with state._lock:
        prev = state.if_counters.get(prkey)
        prev_ts = state.if_counter_ts.get(prkey)
    bps_in = bps_out = 0.0
    measured = False
    if prev and prev_ts:
        prev_in = float(prev.get('bytes_in', 0))
        prev_out = float(prev.get('bytes_out', 0))
        # Counter reset or collector restart with stale baseline — re-seed, no spike.
        if bytes_in < prev_in or bytes_out < prev_out:
            with state._lock:
                state.if_counters[prkey] = {'bytes_in': bytes_in, 'bytes_out': bytes_out}
                state.if_counter_ts[prkey] = now
            return 0.0, 0.0, False
        elapsed = max((now - prev_ts).total_seconds(), 1.0)
        bps_in = max(0.0, (bytes_in - prev_in) * 8.0 / elapsed)
        bps_out = max(0.0, (bytes_out - prev_out) * 8.0 / elapsed)
        if (
            bps_in > _MAX_SANE_PORT_BPS
            or bps_out > _MAX_SANE_PORT_BPS
            or bps_in > _MAX_PULSE_WRITE_BPS
            or bps_out > _MAX_PULSE_WRITE_BPS
        ):
            with state._lock:
                state.if_counters[prkey] = {'bytes_in': bytes_in, 'bytes_out': bytes_out}
                state.if_counter_ts[prkey] = now
            return 0.0, 0.0, False
        measured = True
    with state._lock:
        state.if_counters[prkey] = {'bytes_in': bytes_in, 'bytes_out': bytes_out}
        state.if_counter_ts[prkey] = now
    return bps_in, bps_out, measured


def _port_throughput_bps(
    stat: dict,
    *,
    prkey: str,
    now: datetime,
    state: CollectorState,
) -> Tuple[float, float, bool]:
    """Instant bit rates when present; else delta from cumulative byte counters.

    AresONE /portstats exposes cumulative ``bytesReceived``/``bytesSent``; prefer
    the delta path whenever byte counters exist so we never treat totals as bps.
    """
    bytes_in, bytes_out = _parse_port_byte_counters(stat)
    if bytes_in is not None or bytes_out is not None:
        return _bps_from_byte_counter_delta(
            prkey=prkey,
            bytes_in=float(bytes_in or 0),
            bytes_out=float(bytes_out or 0),
            now=now,
            state=state,
        )
    rx, tx = _parse_port_bitrate(stat)
    if rx > _MAX_SANE_PORT_BPS:
        rx = 0.0
    if tx > _MAX_SANE_PORT_BPS:
        tx = 0.0
    return rx, tx, True


def _parse_speed_gbps(speed_raw: Any) -> float:
    if speed_raw is None:
        return 0.0
    s = str(speed_raw).upper().replace(' ', '')
    if not s:
        return 0.0
    m = re.search(r'([\d.]+)\s*G', s)
    if m:
        return float(m.group(1))
    m = re.search(r'([\d.]+)\s*M', s)
    if m:
        return round(float(m.group(1)) / 1000.0, 4)
    try:
        val = float(re.sub(r'[^\d.]', '', s) or 0)
        if val >= 1000:
            # IxOS REST ``speed`` is typically Mbps (e.g. 400000 → 400 Gbps).
            return round(val / 1000.0, 4)
        if val > 1_000_000:
            return round(val / 1e9, 4)
        return val
    except ValueError:
        return 0.0


def _port_is_link_up(port: dict) -> Optional[bool]:
    link = port.get('link_state') or port.get('link') or port.get('pcsLinkStatus') or ''
    s = str(link).upper()
    if s in ('UP', 'LINK_UP', '1', 'TRUE', 'LINKED'):
        return True
    if s in ('DOWN', 'LINK_DOWN', '0', 'FALSE', 'NOTLINKED', 'UNKNOWN'):
        if s == 'UNKNOWN':
            return None
        return False
    return None


def _get_chassis_driver_cached(chassis: KeysightChassis, state: CollectorState):
    with state._lock:
        cached = state.chassis_drivers.get(chassis.pk)
        if cached is not None:
            return cached
    try:
        driver = get_chassis_driver(chassis)
    except Exception:
        raise
    with state._lock:
        state.chassis_drivers[chassis.pk] = driver
    return driver


def _emit_port_event(state: CollectorState, *args, **kwargs) -> None:
    """Thread-safe event emission via buffer."""
    state.event_buffer.open_event(*args, **kwargs)


def _close_port_event(state: CollectorState, *args, **kwargs) -> None:
    state.event_buffer.close_events(*args, **kwargs)


def collect_chassis(
    topo_id: int,
    node: LabTopologyNode,
    chassis: KeysightChassis,
    state: CollectorState,
    *,
    sampled_at: Optional[datetime] = None,
) -> int:
    """IxOS/KCOS chassis: CPU/mem, port ownership, link state, throughput."""
    now = sampled_at or timezone.now()
    node_id = node.pk
    rkey = node_resource_key(node)
    samples: List[LabMetricSample] = []

    try:
        driver = _get_chassis_driver_cached(chassis, state)
    except Exception as e:
        logger.warning('chassis driver topo=%s node=%s: %s', topo_id, node_id, e)
        return 0

    chassis_cpu = 0.0
    chassis_mem_pct = 0.0
    health_res = driver.get_health()
    if health_res.success and isinstance(health_res.data, dict):
        h = health_res.data
        chassis_cpu = float(h.get('cpu_utilization') or 0)
        chassis_mem_pct = _mem_pct(h)
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=rkey, metric='cpu_pct',
            value=chassis_cpu, sampled_at=now,
        ))
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=rkey, metric='mem_pct',
            value=chassis_mem_pct, sampled_at=now,
        ))

    ports_res = driver.get_ports()
    ports: List[dict] = []
    if ports_res.success and isinstance(ports_res.data, list):
        from connect.keysight_drivers.ixos import fill_inferred_pcpu_mgmt_ips

        ports = fill_inferred_pcpu_mgmt_ips(list(ports_res.data))

    stats_raw: List[dict] = []
    if hasattr(driver, 'get_port_stats'):
        ps = driver.get_port_stats()
        if ps.success:
            raw = ps.data
            if isinstance(raw, list):
                stats_raw = raw
            elif isinstance(raw, dict):
                stats_raw = raw.get('ports') or raw.get('portStats') or []

    by_id, by_card_port, by_parent_id = _index_port_stats(stats_raw)
    pcpu_health: Dict[str, dict] = {}
    if hasattr(driver, 'get_pcpu_health_by_mgmt_ip'):
        ph = driver.get_pcpu_health_by_mgmt_ip()
        if ph.success and isinstance(ph.data, dict):
            pcpu_health = ph.data

    for port in ports:
        label = _chassis_port_label(port)
        prkey = port_resource_key(node_id, label)
        owner = (port.get('owner') or 'Free').strip() or 'Free'

        with state._lock:
            prev_owner = state.port_owners.get(prkey, 'Free')
            prev_link = state.port_link_state.get(prkey)

        owned = 1.0 if owner != 'Free' else 0.0
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=prkey, metric='port_ownership',
            value=owned, sampled_at=now,
        ))

        speed_gbps = _parse_speed_gbps(port.get('speed') or port.get('link_speed'))
        link_up = _port_is_link_up(port)
        if link_up is False:
            samples.append(LabMetricSample(
                topology_id=topo_id, resource_key=prkey, metric='link_speed_gbps',
                value=0.0, sampled_at=now,
            ))
        elif speed_gbps:
            samples.append(LabMetricSample(
                topology_id=topo_id, resource_key=prkey, metric='link_speed_gbps',
                value=speed_gbps, sampled_at=now,
            ))

        if owner != 'Free' and prev_owner == 'Free':
            _emit_port_event(
                state, topo_id, prkey, 'port_owned', started_at=now,
                payload={'owner': owner, 'label': label},
            )
        elif owner == 'Free' and prev_owner != 'Free':
            _close_port_event(state, topo_id, prkey, 'port_owned', ended_at=now)
            _emit_port_event(
                state, topo_id, prkey, 'port_released', started_at=now,
                payload={'previous_owner': prev_owner, 'label': label},
            )

        if link_up is True and prev_link is not True:
            _emit_port_event(state, topo_id, prkey, 'link_up', started_at=now, payload={'label': label})
        elif link_up is False and prev_link is not False:
            _close_port_event(state, topo_id, prkey, 'link_up', ended_at=now)
            _emit_port_event(state, topo_id, prkey, 'link_down', started_at=now, payload={'label': label})

        with state._lock:
            state.port_owners[prkey] = owner
            if link_up is not None:
                state.port_link_state[prkey] = link_up

        stat = _lookup_port_stat(port, by_id, by_card_port, by_parent_id)
        rx, tx = (0.0, 0.0)
        cpu_p, mem_p = None, None
        mgmt_ip = pcpu_mgmt_ip_from_port(port)
        pcpu = pcpu_health.get(mgmt_ip) if mgmt_ip else None
        if isinstance(pcpu, dict):
            if pcpu.get('cpu_pct') is not None:
                cpu_p = float(pcpu['cpu_pct'])
            if pcpu.get('mem_pct') is not None:
                mem_p = float(pcpu['mem_pct'])
        tput_measured = False
        if stat:
            rx, tx, tput_measured = _port_throughput_bps(stat, prkey=prkey, now=now, state=state)
            stat_cpu, stat_mem = _parse_port_util_pct(stat)
            if cpu_p is None:
                cpu_p = stat_cpu
            if mem_p is None:
                mem_p = stat_mem
        # IxOS has no per-port CPU in /portstats; use chassis mgmt until PCPU path exists.
        if cpu_p is None and chassis_cpu:
            cpu_p = chassis_cpu
        if mem_p is None and chassis_mem_pct:
            mem_p = chassis_mem_pct
        if cpu_p is not None:
            samples.append(LabMetricSample(
                topology_id=topo_id, resource_key=prkey, metric='cpu_pct',
                value=round(cpu_p, 2), sampled_at=now,
            ))
        if mem_p is not None:
            samples.append(LabMetricSample(
                topology_id=topo_id, resource_key=prkey, metric='mem_pct',
                value=round(mem_p, 2), sampled_at=now,
            ))
        # Record throughput when we have a real measurement. Skip the first baseline
        # sample and counter-reset re-seeds (0 bps) so Lab Pulse keeps the last good
        # rate instead of showing 0/0 after every collector restart.
        if stat is not None and tput_measured:
            samples.append(LabMetricSample(
                topology_id=topo_id, resource_key=prkey, metric='bps_in',
                value=rx, sampled_at=now,
            ))
            samples.append(LabMetricSample(
                topology_id=topo_id, resource_key=prkey, metric='bps_out',
                value=tx, sampled_at=now,
            ))

    pcpu_index: Dict[str, str] = {}
    owner_index: Dict[str, str] = {}
    for port in ports:
        if not isinstance(port, dict):
            continue
        label = _chassis_port_label(port)
        if not label:
            continue
        prkey = port_resource_key(node_id, label)
        mgmt_ip = pcpu_mgmt_ip_from_port(port)
        if mgmt_ip:
            pcpu_index[prkey] = mgmt_ip
        owner = (port.get('owner') or 'Free').strip() or 'Free'
        if owner != 'Free':
            owner_index[prkey] = owner
    if hasattr(driver, 'get_pcpu_apps_by_mgmt_ip'):
        try:
            pa = driver.get_pcpu_apps_by_mgmt_ip()
            if pa.success and isinstance(pa.data, dict) and pa.data:
                from connect.cache_utils import cache_get, cache_set
                from connect.pcpu_versions import PCPU_APPS_CACHE_TTL

                cache_set(f'pcpu_apps:{topo_id}:{node_id}', pa.data, PCPU_APPS_CACHE_TTL)
                merged_apps = dict(cache_get(f'pcpu_apps:{topo_id}') or {})
                merged_apps.update(pa.data)
                cache_set(f'pcpu_apps:{topo_id}', merged_apps, PCPU_APPS_CACHE_TTL)
        except Exception as exc:
            logger.debug('PCPU app versions topo=%s node=%s: %s', topo_id, node_id, exc)

    if pcpu_index or owner_index:
        from connect.cache_utils import cache_get, cache_set

        if pcpu_index:
            from connect.topology_resource_catalog import PORT_PCPU_MGMT_CACHE_TTL

            cache_set(f'pcpu_mgmt_ip:{topo_id}:{node_id}', pcpu_index, PORT_PCPU_MGMT_CACHE_TTL)
            topo_key = f'port_mgmt_ip:{topo_id}'
            prefix = f'node_{node_id}__'
            merged = {
                k: v for k, v in (cache_get(topo_key) or {}).items()
                if not str(k).startswith(prefix)
            }
            merged.update(pcpu_index)
            cache_set(topo_key, merged, PORT_PCPU_MGMT_CACHE_TTL)
        if owner_index:
            cache_set(f'port_owner_names:{topo_id}:{node_id}', owner_index, 600)
            topo_owners = dict(cache_get(f'port_owner_names:{topo_id}') or {})
            topo_owners.update(owner_index)
            cache_set(f'port_owner_names:{topo_id}', topo_owners, 600)

    written = bulk_write_metrics(samples)
    if written:
        upsert_hourly_rollups(topo_id, samples)
    return written


def collect_switch(
    topo_id: int,
    node: LabTopologyNode,
    state: CollectorState,
    *,
    sampled_at: Optional[datetime] = None,
) -> int:
    """Arista/SONiC switch: device CPU/mem + per-interface counter deltas."""
    if not node.device:
        return 0
    now = sampled_at or timezone.now()
    node_id = node.pk
    rkey = node_resource_key(node)
    samples: List[LabMetricSample] = []

    try:
        driver = get_switch_driver(node.device)
    except Exception as e:
        logger.warning('switch driver topo=%s node=%s: %s', topo_id, node_id, e)
        return 0

    health_res = driver.get_health()
    if health_res.success and isinstance(health_res.data, dict):
        h = health_res.data
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=rkey, metric='cpu_pct',
            value=float(h.get('cpu_utilization') or 0), sampled_at=now,
        ))
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=rkey, metric='mem_pct',
            value=_mem_pct(h), sampled_at=now,
        ))

    counters_res = driver.get_interface_counters()
    if not counters_res.success or not isinstance(counters_res.data, list):
        written = bulk_write_metrics(samples)
        if written:
            upsert_hourly_rollups(topo_id, samples)
        return written

    counter_aliases = build_switch_counter_aliases(node)
    vendor = (node.device.vendor_type or '').lower()
    track_discards = vendor == 'arista'

    def _append_bps_samples(resource_key: str, bps_in: float, bps_out: float) -> None:
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=resource_key, metric='bps_in',
            value=round(bps_in, 2), sampled_at=now,
        ))
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=resource_key, metric='bps_out',
            value=round(bps_out, 2), sampled_at=now,
        ))

    def _append_discard_samples(resource_key: str, delta: float, iface: str) -> None:
        if delta <= 0:
            return
        samples.append(LabMetricSample(
            topology_id=topo_id, resource_key=resource_key, metric='input_discards',
            value=round(delta, 0), sampled_at=now,
        ))
        _emit_port_event(
            state, topo_id, resource_key, 'input_discard',
            started_at=now, payload={'delta': round(delta, 0), 'iface': iface},
        )

    for c in counters_res.data:
        if not isinstance(c, dict):
            continue
        name = (c.get('name') or '').strip()
        if not name:
            continue
        prkey = port_resource_key(node_id, name)
        with state._lock:
            prev = state.if_counters.get(prkey)
            prev_ts = state.if_counter_ts.get(prkey)
        bytes_in = float(c.get('bytes_in') or 0)
        bytes_out = float(c.get('bytes_out') or 0)
        if prev and prev_ts:
            elapsed = max((now - prev_ts).total_seconds(), 1.0)
            bps_in = max(0.0, (bytes_in - float(prev.get('bytes_in', 0))) * 8 / elapsed)
            bps_out = max(0.0, (bytes_out - float(prev.get('bytes_out', 0))) * 8 / elapsed)
            _append_bps_samples(prkey, bps_in, bps_out)
            for label in counter_aliases.get(name) or []:
                _append_bps_samples(port_resource_key(node_id, label), bps_in, bps_out)
            if track_discards and 'input_discards' in c:
                cur_disc = float(c.get('input_discards') or 0)
                delta = _counter_delta(float(prev.get('input_discards', cur_disc)), cur_disc)
                _append_discard_samples(prkey, delta, name)
                for label in counter_aliases.get(name) or []:
                    _append_discard_samples(port_resource_key(node_id, label), delta, name)
        with state._lock:
            prev_row = dict(state.if_counters.get(prkey) or {})
            prev_row['bytes_in'] = bytes_in
            prev_row['bytes_out'] = bytes_out
            if track_discards and 'input_discards' in c:
                prev_row['input_discards'] = float(c.get('input_discards') or 0)
            state.if_counters[prkey] = prev_row
            state.if_counter_ts[prkey] = now

    written = bulk_write_metrics(samples)
    if written:
        upsert_hourly_rollups(topo_id, samples)
    return written


def _normalize_ocs_xconns(rows: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get('name') or row.get('n') or '').strip()
        if not name:
            continue
        out[name] = {
            'port_a': str(row.get('port_a') or ''),
            'port_b': str(row.get('port_b') or ''),
            'group': str(row.get('group') or ''),
        }
    return out


def _parse_ocs_row(row: dict) -> Optional[Tuple[str, str, str]]:
    """Return (name, port_a, port_b) from raw crossconnect row."""
    name = str(row.get('name') or row.get('connid') or '').strip()
    h1 = row.get('half1') or {}
    if not isinstance(h1, dict):
        return None
    c1 = str(h1.get('conn') or '')
    m = re.match(r'([^>]+)>([^>]+)', c1)
    if not m:
        return None
    return name or c1, m.group(1).strip(), m.group(2).strip()


def collect_ocs(
    topo_id: int,
    node: LabTopologyNode,
    state: CollectorState,
    *,
    sampled_at: Optional[datetime] = None,
) -> int:
    """OCS photonic switch: diff crossconnects → patch events."""
    if not node.device:
        return 0
    now = sampled_at or timezone.now()
    node_id = node.pk

    try:
        driver = get_switch_driver(node.device)
    except Exception as e:
        logger.warning('ocs driver topo=%s node=%s: %s', topo_id, node_id, e)
        return 0

    parsed: Dict[str, dict] = {}
    if hasattr(driver, 'get_ocs_crossconnects'):
        res = driver.get_ocs_crossconnects()
        if res.success and isinstance(res.data, list):
            parsed = _normalize_ocs_xconns(res.data)
    elif hasattr(driver, 'fetch_crossconnect_list'):
        raw = driver.fetch_crossconnect_list()
        for row in raw or []:
            t = _parse_ocs_row(row)
            if t:
                name, pa, pb = t
                parsed[name] = {'port_a': pa, 'port_b': pb, 'group': ''}

    with state._lock:
        prev = dict(state.ocs_xconns.get(node_id, {}))

    for name, xc in parsed.items():
        prkey = port_resource_key(node_id, xc['port_a'])
        if name not in prev:
            _emit_port_event(
                state, topo_id, prkey, 'patch_add', started_at=now,
                payload={'name': name, 'port_a': xc['port_a'], 'port_b': xc['port_b']},
            )
            _emit_port_event(state, topo_id, prkey, 'link_up', started_at=now, payload={'name': name})
        elif xc['port_b'] != prev[name].get('port_b'):
            _close_port_event(state, topo_id, prkey, 'patch_add', ended_at=now)
            _emit_port_event(
                state, topo_id, prkey, 'patch_move', started_at=now,
                payload={
                    'name': name,
                    'port_a': xc['port_a'],
                    'port_b': xc['port_b'],
                    'previous_port_b': prev[name].get('port_b'),
                },
            )

    for name in prev:
        if name not in parsed:
            pa = prev[name].get('port_a', '')
            prkey = port_resource_key(node_id, pa) if pa else node_resource_key(node)
            _close_port_event(state, topo_id, prkey, 'patch_add', ended_at=now)
            _emit_port_event(
                state, topo_id, prkey, 'patch_remove', started_at=now,
                payload={'name': name, 'port_a': pa, 'port_b': prev[name].get('port_b')},
            )
            _close_port_event(state, topo_id, prkey, 'link_up', ended_at=now)
            _emit_port_event(state, topo_id, prkey, 'link_down', started_at=now, payload={'name': name})

    with state._lock:
        state.ocs_xconns[node_id] = parsed
    return 0


def collect_topology_node(
    topo_id: int,
    node: LabTopologyNode,
    state: CollectorState,
    *,
    sampled_at: Optional[datetime] = None,
) -> int:
    """Dispatch collector for a single topology node."""
    extra = node.extra or {}
    chassis_id = extra.get('chassis_id')
    total = 0

    if node.node_type == 'chassis' or chassis_id:
        chassis = resolve_topology_chassis(node)
        if chassis:
            bind_topology_chassis(node, chassis)
            total += collect_chassis(topo_id, node, chassis, state, sampled_at=sampled_at)
        return total

    if node.node_type == 'ocs' and node.device:
        collect_ocs(topo_id, node, state, sampled_at=sampled_at)
        return 0

    if node.node_type == 'switch' and node.device:
        vendor = (node.device.vendor_type or '').lower()
        if vendor in ('arista', 'sonic'):
            total += collect_switch(topo_id, node, state, sampled_at=sampled_at)
        else:
            from connect.driver_registry import discover_plugin_manifests
            manifest = discover_plugin_manifests().get(vendor)
            if manifest and manifest.collector_hook:
                try:
                    mod_name, _, fn_name = manifest.collector_hook.partition(':')
                    mod = __import__(mod_name, fromlist=[fn_name or 'collect'])
                    fn = getattr(mod, fn_name or 'collect', None)
                    if callable(fn):
                        fn(topo_id, node, state, sampled_at=sampled_at)
                except Exception:
                    logger.exception('plugin collector %s failed', vendor)
    return total


MAX_COLLECT_WORKERS = 8
NODE_COLLECT_TIMEOUT = 120


def _collect_topology_node_safe(topo_id, node, state, *, sampled_at):
    """Thread worker: close Django DB connections so Postgres is not leaked."""
    close_old_connections()
    try:
        return collect_topology_node(topo_id, node, state, sampled_at=sampled_at)
    finally:
        close_old_connections()


def collect_all_topologies(
    topology_id: Optional[int] = None,
    *,
    state: Optional[CollectorState] = None,
    workers: int = MAX_COLLECT_WORKERS,
) -> int:
    """One live/seeded collect cycle for enabled topologies.

    Used by ``run_metric_collector`` (Compose/systemd) and
    ``collect_topology_metrics``. Returns the number of metric rows written.
    """
    own_state = state is None
    if own_state:
        state = CollectorState()
        state.load_if_counters()

    topo_qs = LabTopology.objects.filter(metrics_collection_enabled=True)
    if topology_id:
        topo_qs = topo_qs.filter(pk=topology_id)
    allow = collector_topology_allowlist()
    if allow:
        topo_qs = topo_qs.filter(pk__in=allow)
    mode = collector_mode()
    total = 0
    now = timezone.now()
    workers = max(1, int(workers or MAX_COLLECT_WORKERS))

    if mode == 'seeded':
        for topo in topo_qs:
            total += seed_topology_metrics(topo.pk, backfill_points=1, now=now)
        if own_state:
            state.persist_if_counters()
        return total

    node_types = collector_node_types()
    seed_discards = collector_switch_discards_seeded()
    for topo in topo_qs:
        if seed_discards:
            has_recent = LabMetricSample.objects.using('np_timeseries').filter(
                topology_id=topo.pk, metric='input_discards',
                sampled_at__gte=now - timedelta(hours=1),
            ).exists()
            total += seed_switch_discards(
                topo.pk, now=now,
                backfill_points=1 if has_recent else 24,
                state=state,
            )
        nodes = list(
            LabTopologyNode.objects.filter(topology_id=topo.pk).select_related('device')
        )
        if node_types is not None:
            nodes = [n for n in nodes if (n.node_type or '').lower() in node_types]
        if not nodes:
            continue
        with ThreadPoolExecutor(max_workers=min(workers, len(nodes))) as pool:
            futures = {
                pool.submit(
                    _collect_topology_node_safe, topo.pk, node, state, sampled_at=now,
                ): node
                for node in nodes
            }
            for fut in as_completed(futures):
                node = futures[fut]
                try:
                    total += fut.result(timeout=NODE_COLLECT_TIMEOUT)
                except Exception as e:
                    logger.warning(
                        'collector skip topo=%s node=%s: %s',
                        topo.pk, node.pk, e, exc_info=True,
                    )
        state.event_buffer.flush()
    close_old_connections()
    if own_state:
        state.persist_if_counters()
    return total
