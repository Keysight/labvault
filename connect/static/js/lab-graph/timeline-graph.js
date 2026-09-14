/**
 * D3 swimlane timeline for lab topology resource usage (incremental render).
 */

import { filterLanes } from './timeline-filters.js';

export {
  listOwnersFromTimeline,
  resourcesForOwners,
  resourcesHighUsage,
  intersectKeySets,
} from './timeline-filters.js';

const BUCKET_MS = { '5m': 300000, '15m': 900000, '1h': 3600000 };
export const LANE_HEIGHT = 57;
const MAX_PORTS_PER_DEVICE = 256;
const VIEWPORT_BUFFER = 4;
const PCT_METRICS = new Set(['cpu_pct', 'mem_pct']);
export const PCT_WARN = 70;
export const PCT_CRIT = 90;
const MS_24H = 24 * 3600000;

const METRIC_COLORS = {
  cpu_pct: '#38bdf8',
  mem_pct: '#a78bfa',
  bps_in: '#22d3ee',
  bps_out: '#fb923c',
  port_ownership: '#3b82f6',
  link_speed_gbps: '#a3e635',
  input_discards: '#f43f5e',
};

const METRIC_OPACITY = {
  bps_in: 0.92,
  bps_out: 0.92,
  link_speed_gbps: 0.72,
  port_ownership: 0.85,
  input_discards: 0.9,
};

const DEVICE_METRIC_ORDER = ['cpu_pct', 'mem_pct', 'bps_in', 'bps_out'];
const PORT_METRIC_ORDER = ['port_ownership', 'link_speed_gbps', 'bps_in', 'bps_out', 'input_discards', 'cpu_pct', 'mem_pct'];

/** Traffic-flow device groups — parents stay together when reordering. */
export const DEVICE_GROUP_DEFS = [
  { id: 'aresone', label: 'AresONE', profiles: ['keysight_chassis'] },
  { id: 'arista', label: 'Arista / Sonic', profiles: ['arista_switch', 'sonic_switch'] },
  { id: 'other', label: 'Other', profiles: ['generic'] },
  { id: 'ocs', label: 'OCS', profiles: ['ocs'] },
];

export const DEFAULT_DEVICE_GROUP_ORDER = ['aresone', 'arista', 'other', 'ocs'];

export function deviceGroupForProfile(profile) {
  for (const def of DEVICE_GROUP_DEFS) {
    if (def.profiles.includes(profile)) return def.id;
  }
  return 'other';
}

export function normalizeDeviceGroupOrder(order) {
  const seen = new Set();
  const out = [];
  for (const id of order || []) {
    if (DEVICE_GROUP_DEFS.some((d) => d.id === id) && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  for (const def of DEVICE_GROUP_DEFS) {
    if (!seen.has(def.id)) out.push(def.id);
  }
  return out;
}

function sortParentsByGroup(parentEntries, groupOrder) {
  const order = normalizeDeviceGroupOrder(groupOrder);
  const grouped = new Map();
  for (const entry of parentEntries) {
    const gid = deviceGroupForProfile(entry[1].profile);
    if (!grouped.has(gid)) grouped.set(gid, []);
    grouped.get(gid).push(entry);
  }
  for (const list of grouped.values()) {
    list.sort((a, b) => String(a[1].label).localeCompare(String(b[1].label), undefined, { numeric: true }));
  }
  const sorted = [];
  for (const gid of order) {
    for (const entry of grouped.get(gid) || []) sorted.push(entry);
  }
  return sorted;
}

export const METRIC_DEFS = [
  { id: 'cpu_pct', label: 'CPU', color: '#38bdf8', dash: null, lanes: ['device', 'port'] },
  { id: 'mem_pct', label: 'Memory', color: '#a78bfa', dash: '4 2', lanes: ['device', 'port'] },
  { id: 'port_ownership', label: 'Ownership', color: '#3b82f6', dash: null, lanes: ['port'] },
  { id: 'link_speed_gbps', label: 'Link Gbps', color: '#a3e635', dash: '2 2', lanes: ['port'] },
  { id: 'bps_in', label: 'Tput in', color: '#22d3ee', dash: '2 3', lanes: ['device', 'port'] },
  { id: 'bps_out', label: 'Tput out', color: '#fb923c', dash: '3 2', lanes: ['device', 'port'] },
  { id: 'input_discards', label: 'Input discards', color: '#f43f5e', dash: null, lanes: ['port'] },
];

function timelineTheme() {
  const cs = getComputedStyle(document.body);
  const pick = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
  return {
    laneEven: pick('--tl-lane-bg-even', '#0a162866'),
    laneOdd: pick('--tl-lane-bg-odd', '#0d1b2a44'),
    label: pick('--tl-label', '#e2e8f0'),
    labelMuted: pick('--tl-label-muted', '#94a3b8'),
    axis: pick('--tl-axis', '#94a3b8'),
    sep: pick('--tl-sep', '#1e3a5f'),
  };
}

function formatBpsShort(v) {
  const n = Number(v) || 0;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gbps`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mbps`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} Kbps`;
  return `${Math.round(n)} bps`;
}

function parseTs(iso) {
  return iso ? new Date(iso).getTime() : 0;
}

function normalizeEvents(events) {
  if (!events) return {};
  if (Array.isArray(events)) {
    const grouped = {};
    for (const e of events) {
      const k = e.resource_key;
      if (!grouped[k]) grouped[k] = [];
      grouped[k].push(e);
    }
    return grouped;
  }
  return events;
}

function deviceLanes(catalog, opts = {}) {
  const focusDeviceId = opts.focusDeviceId || null;
  const focusDeviceIds = opts.focusDeviceIds || null;
  const includePorts = !!opts.includePorts;
  const collapsedDeviceKeys = opts.collapsedDeviceKeys || null;
  const groupOrder = opts.groupOrder || null;
  const parents = new Map();
  const children = new Map();
  for (const [key, meta] of Object.entries(catalog || {})) {
    if (meta.parent) {
      if (!children.has(meta.parent)) children.set(meta.parent, []);
      children.get(meta.parent).push({ key, ...meta });
    } else {
      parents.set(key, { key, ...meta });
    }
  }
  const parentEntries = sortParentsByGroup([...parents.entries()], groupOrder);
  const lanes = [];
  for (const [pkey, parent] of parentEntries) {
    if (focusDeviceIds?.size && !focusDeviceIds.has(pkey)) continue;
    if (focusDeviceId && pkey !== focusDeviceId) continue;
    const portSubs = (children.get(pkey) || []).sort((a, b) => String(a.label).localeCompare(String(b.label)));
    const portCount = portSubs.length;
    const portsCollapsed = !!(includePorts && collapsedDeviceKeys?.has(pkey));
    lanes.push({
      ...parent,
      depth: 0,
      laneType: 'device',
      portCount,
      portsCollapsed,
    });
    if (includePorts && !portsCollapsed) {
      for (const sub of portSubs.slice(0, MAX_PORTS_PER_DEVICE)) {
        lanes.push({ ...sub, depth: 1, laneType: 'port', parentKey: pkey });
      }
    }
  }
  return lanes;
}

function eventColor(type) {
  const map = {
    port_owned: '#3b82f6',
    port_released: '#475569',
    patch_add: '#22c55e',
    patch_remove: '#ef4444',
    patch_move: '#f59e0b',
    link_up: '#64748b',
    link_down: '#475569',
    input_discard: '#f43f5e',
  };
  return map[type] || '#94a3b8';
}

/** Window-level stats for the Usage timeline summary strip (big picture without clutter). */
export function computeTimelineSummary(data, { fromMs, toMs } = {}) {
  const catalog = data?.catalog || {};
  const buckets = data?.metric_buckets || {};
  const winFrom = fromMs ?? parseTs(data?.window?.from);
  const winTo = toMs ?? parseTs(data?.window?.to);

  let deviceCount = 0;
  let portCount = 0;
  let cpuPeaks = [];
  let memPeaks = [];
  let hotCpuPorts = 0;
  let trafficPeak = 0;
  let totalInputDiscards = 0;
  let switchDiscardPorts = 0;
  let firstDiscardAt = null;

  for (const [key, meta] of Object.entries(catalog)) {
    const isPort = meta.profile === 'keysight_port' || key.includes('__');
    if (isPort) portCount += 1;
    else deviceCount += 1;

    const mb = buckets[key] || {};
    const cpuSeries = mb.cpu_pct || [];
    const memSeries = mb.mem_pct || [];
    let cpuMax = 0;
    let memMax = 0;
    for (const [iso, val] of cpuSeries) {
      const t = parseTs(iso);
      if (t >= winFrom && t < winTo) cpuMax = Math.max(cpuMax, Number(val) || 0);
    }
    for (const [iso, val] of memSeries) {
      const t = parseTs(iso);
      if (t >= winFrom && t < winTo) memMax = Math.max(memMax, Number(val) || 0);
    }
    if (!isPort) {
      if (cpuMax) cpuPeaks.push(cpuMax);
      if (memMax) memPeaks.push(memMax);
    } else {
      if (cpuMax >= PCT_WARN) hotCpuPorts += 1;
      for (const m of ['bps_in', 'bps_out']) {
        for (const [iso, val] of mb[m] || []) {
          const t = parseTs(iso);
          if (t >= winFrom && t < winTo) trafficPeak = Math.max(trafficPeak, Number(val) || 0);
        }
      }
      let portDiscards = 0;
      for (const [iso, val] of mb.input_discards || []) {
        const t = parseTs(iso);
        const v = Number(val) || 0;
        if (t >= winFrom && t < winTo) {
          portDiscards += v;
          if (v > 0 && !firstDiscardAt) firstDiscardAt = iso;
          else if (v > 0 && firstDiscardAt && parseTs(iso) < parseTs(firstDiscardAt)) {
            firstDiscardAt = iso;
          }
        }
      }
      if (portDiscards > 0) {
        switchDiscardPorts += 1;
        totalInputDiscards += portDiscards;
      }
    }
  }

  const grouped = normalizeEvents(data?.events);
  let ownedEpisodes = 0;
  for (const evts of Object.values(grouped)) {
    for (const e of evts) {
      if (e.event_type !== 'port_owned') continue;
      const start = parseTs(e.started_at);
      const end = parseTs(e.ended_at || data?.window?.to) || winTo;
      if (end > winFrom && start < winTo) ownedEpisodes += 1;
    }
  }

  const avg = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
  return {
    device_count: deviceCount,
    port_count: portCount,
    avg_cpu_peak: avg(cpuPeaks) != null ? Math.round(avg(cpuPeaks)) : null,
    avg_mem_peak: avg(memPeaks) != null ? Math.round(avg(memPeaks)) : null,
    hot_cpu_ports: hotCpuPorts,
    owned_episodes: ownedEpisodes,
    traffic_peak_bps: Math.round(trafficPeak),
    switch_discard_ports: switchDiscardPorts,
    total_input_discards: Math.round(totalInputDiscards),
    first_discard_at: firstDiscardAt,
  };
}

const OWNER_PALETTE = [
  '#3b82f6', '#22c55e', '#f59e0b', '#a78bfa', '#ec4899',
  '#14b8a6', '#eab308', '#f97316', '#6366f1', '#84cc16',
];

/** Build stable owner → color map from grouped events. */
export function buildOwnerColorMap(eventsGrouped) {
  const owners = new Set();
  for (const evts of Object.values(eventsGrouped || {})) {
    for (const e of evts) {
      const pl = e.payload || {};
      if (e.event_type === 'port_owned' && pl.owner && pl.owner !== 'Free') {
        owners.add(pl.owner);
      }
      if (e.event_type === 'port_released' && pl.previous_owner) {
        owners.add(pl.previous_owner);
      }
    }
  }
  const sorted = [...owners].sort((a, b) => a.localeCompare(b));
  const map = {};
  sorted.forEach((o, i) => { map[o] = OWNER_PALETTE[i % OWNER_PALETTE.length]; });
  return map;
}

export function ownerLegendEntries(eventsGrouped) {
  const map = buildOwnerColorMap(eventsGrouped);
  return Object.entries(map).map(([owner, color]) => ({ owner, color }));
}

function fillForEvent(e, ownerColors) {
  if (e.event_type === 'port_owned') {
    const owner = e.payload?.owner;
    if (owner && owner !== 'Free') return ownerColors[owner] || eventColor('port_owned');
  }
  if (e.event_type === 'port_released') return '#1e293b';
  return eventColor(e.event_type);
}

function ownershipEvents(laneEvents) {
  return (laneEvents || []).filter((e) => e.event_type === 'port_owned' || e.event_type === 'port_released');
}

function metricsForLane(lane, rawBuckets, enabledMetrics) {
  const order = lane.laneType === 'device' ? DEVICE_METRIC_ORDER : PORT_METRIC_ORDER;
  const enabled = enabledMetrics || null;
  return order.filter((m) => {
    if (!(rawBuckets[m] || []).length) return false;
    if (enabled && !enabled.has(m)) return false;
    return true;
  });
}

/** Resource keys with metric activity or events overlapping [fromMs, toMs). */
export function activeResourceKeysInRange(data, fromMs, toMs) {
  const keys = new Set();
  const winEnd = parseTs(data.window?.to);
  for (const [key, buckets] of Object.entries(data.metric_buckets || {})) {
    for (const series of Object.values(buckets || {})) {
      for (const [iso, val] of series || []) {
        const t = parseTs(iso);
        if (t >= fromMs && t < toMs && Number(val) > 0) {
          keys.add(key);
          break;
        }
      }
    }
  }
  const grouped = normalizeEvents(data.events);
  for (const [key, evts] of Object.entries(grouped)) {
    for (const e of evts) {
      const start = parseTs(e.started_at);
      const end = parseTs(e.ended_at || data.window?.to) || winEnd;
      if (end > fromMs && start < toMs) keys.add(key);
    }
  }
  return keys;
}

function yScaleForMetric(metric, series, laneHeight, d3lib, { fixedPctScale } = {}) {
  const d3 = d3lib || window.d3;
  if (!d3) return (v) => laneHeight / 2;
  const vals = series.map((p) => Number(p[1]) || 0);
  if (metric === 'port_ownership') {
    return d3.scaleLinear().domain([0, 1]).range([laneHeight - 4, laneHeight - 11]);
  }
  if (PCT_METRICS.has(metric) && fixedPctScale) {
    return d3.scaleLinear().domain([0, 100]).range([laneHeight - 4, 4]);
  }
  const maxV = Math.max(1e-9, d3.max(vals) || 1);
  return d3.scaleLinear().domain([0, maxV]).range([laneHeight - 6, 4]);
}

function pctOverloadColor(v) {
  const n = Number(v) || 0;
  if (n >= PCT_CRIT) return '#ef4444';
  if (n >= PCT_WARN) return '#f59e0b';
  return null;
}

function strokeDashForMetric(metric) {
  if (metric === 'mem_pct') return '4 2';
  if (metric === 'bps_out') return '3 2';
  if (metric === 'link_speed_gbps') return '2 2';
  return null;
}

function rebucketSeries(series, bucketMs, winFrom, winTo) {
  if (!series?.length || !bucketMs) return series || [];
  const buckets = new Map();
  for (const [iso, val] of series) {
    const t = parseTs(iso);
    if (t < winFrom || t >= winTo) continue;
    const b = Math.floor(t / bucketMs) * bucketMs;
    const arr = buckets.get(b) || [];
    arr.push(Number(val) || 0);
    buckets.set(b, arr);
  }
  return [...buckets.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([ms, vals]) => [new Date(ms).toISOString(), vals.reduce((a, v) => a + v, 0) / vals.length]);
}

function labelSideForMetric(name) {
  if (name === 'cpu_pct' || name === 'bps_in') return { x: 4, anchor: 'start' };
  if (name === 'mem_pct' || name === 'bps_out') return { x: null, anchor: 'end' }; // x set per-lane
  return { x: null, anchor: 'end' };
}

function timelineScrollEl(hostEl) {
  return hostEl?.closest?.('#ug-timeline-scroll') || hostEl?.parentElement || hostEl;
}

const LINK_FLIP_R = 2;
const LINK_RAIL_Y_OFFSET = 5;

/** ~10mm from timeline left + padding on each side of the +/- control (96dpi). */
const EXPAND_CORNER_INSET = 38;
const EXPAND_BTN_SIZE = 14;
const EXPAND_BTN_PAD = 8;
const EXPAND_LABEL_GAP = 14;
const LABEL_GUTTER = 92;
const TIMELINE_MARGIN_LEFT = EXPAND_CORNER_INSET + EXPAND_BTN_PAD + EXPAND_BTN_SIZE
  + EXPAND_BTN_PAD + EXPAND_LABEL_GAP + LABEL_GUTTER;
const EXPAND_BTN_CENTER_SVG = EXPAND_CORNER_INSET + EXPAND_BTN_PAD + EXPAND_BTN_SIZE / 2;

function throttleRaf(fn) {
  let scheduled = false;
  let lastArg = null;
  return (arg) => {
    lastArg = arg;
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      fn(lastArg);
    });
  };
}

export function renderTimelinePanel(container, data, {
  onBrushChange,
  onHover,
  onBrushFilter,
  enabledMetrics,
  focusDeviceId,
  focusDeviceIds,
  includePortLanes,
  onOwnerLegend,
  laneFilterKeys,
  collapsedDeviceKeys,
  groupOrder,
  onDeviceCollapseToggle,
  showOverloadMarkers = false,
  showDiscardMarkers = false,
  showOwnershipVisuals = true,
  showLinkVisuals = false,
  overloadThreshold = PCT_WARN,
} = {}) {
  const renderOpts = {
    onBrushChange,
    onHover,
    onBrushFilter,
    enabledMetrics,
    focusDeviceId,
    focusDeviceIds,
    includePortLanes,
    onOwnerLegend,
    laneFilterKeys,
    collapsedDeviceKeys,
    groupOrder,
    onDeviceCollapseToggle,
    showOverloadMarkers,
    showDiscardMarkers,
    showOwnershipVisuals,
    showLinkVisuals,
    overloadThreshold,
  };
  const el = typeof container === 'string' ? document.querySelector(container) : container;
  if (!el || !window.d3) return null;

  const d3 = window.d3;
  const margin = { top: 24, right: 12, bottom: 32, left: TIMELINE_MARGIN_LEFT };

  let state = el.__tlState;
  if (!state) {
    el.innerHTML = '';
    const width = el.clientWidth || 480;
    const svg = d3.select(el).append('svg').attr('class', 'tl-svg');
    const g = svg.append('g').attr('class', 'tl-root').attr('transform', `translate(${margin.left},${margin.top})`);
    const axisG = g.append('g').attr('class', 'tl-axis');
    const lanesG = g.append('g').attr('class', 'tl-lanes');
    const brushG = g.append('g').attr('class', 'tl-brush');
    state = {
      el,
      svg,
      g,
      axisG,
      lanesG,
      brushG,
      margin,
      width,
      highlightKey: null,
      loadedData: null,
      loadedWinFrom: 0,
      loadedWinTo: 0,
      brushWinFrom: null,
      brushWinTo: null,
      renderOpts: null,
    };
    el.__tlState = state;
  }

  state.renderOpts = renderOpts;

  const fullWinFrom = parseTs(data.window?.from);
  const fullWinTo = parseTs(data.window?.to);
  state.loadedData = data;
  state.loadedWinFrom = fullWinFrom;
  state.loadedWinTo = fullWinTo;

  const viewFrom = state.brushWinFrom ?? fullWinFrom;
  const viewTo = state.brushWinTo ?? fullWinTo;
  const fixedPctScale = (viewTo - viewFrom) <= MS_24H;
  const theme = timelineTheme();

  const width = el.clientWidth || state.width || 480;
  state.width = width;
  let lanes = deviceLanes(data.catalog, {
    focusDeviceId: focusDeviceId || null,
    focusDeviceIds: focusDeviceIds?.size ? focusDeviceIds : null,
    includePorts: !!includePortLanes,
    collapsedDeviceKeys: collapsedDeviceKeys?.size ? collapsedDeviceKeys : null,
    groupOrder: groupOrder || null,
  });
  if (laneFilterKeys?.size) {
    lanes = filterLanes(lanes, laneFilterKeys);
  }
  const innerW = width - margin.left - margin.right;
  const innerH = Math.max(120, lanes.length * LANE_HEIGHT);
  const height = innerH + margin.top + margin.bottom;

  state.svg
    .attr('width', '100%')
    .attr('height', height)
    .attr('viewBox', [0, 0, width, height]);

  const x = d3.scaleTime().domain([viewFrom, viewTo]).range([0, innerW]);
  const eventsGrouped = normalizeEvents(data.events);
  const ownerColors = buildOwnerColorMap(eventsGrouped);
  if (onOwnerLegend) onOwnerLegend(ownerLegendEntries(eventsGrouped));
  const bucketKey = data.window?.bucket || '5m';
  const bucketMs = BUCKET_MS[bucketKey] || BUCKET_MS['5m'];

  state.axisG
    .attr('transform', `translate(0,${innerH})`)
    .call(d3.axisBottom(x).ticks(6))
    .selectAll('text')
    .attr('fill', theme.axis);
  state.axisG.selectAll('path,line').attr('stroke', theme.sep);

  const scrollEl = timelineScrollEl(el);
  const scrollTop = scrollEl.scrollTop || 0;
  const hostH = Math.max(scrollEl.clientHeight || 400, 200);
  const firstVisible = Math.max(0, Math.floor(scrollTop / LANE_HEIGHT) - VIEWPORT_BUFFER);
  const lastVisible = Math.min(lanes.length, Math.ceil((scrollTop + hostH) / LANE_HEIGHT) + VIEWPORT_BUFFER);

  const laneJoin = state.lanesG
    .selectAll('.tl-lane')
    .data(lanes, (d) => d.key);

  laneJoin.exit().remove();

  const laneEnter = laneJoin.enter().append('g').attr('class', 'tl-lane');
  laneEnter.append('rect').attr('class', 'tl-lane-bg');
  laneEnter.append('g').attr('class', 'tl-expand');
  laneEnter.append('text').attr('class', 'tl-label');
  laneEnter.append('line').attr('class', 'tl-sep');
  laneEnter.append('g').attr('class', 'tl-events');
  laneEnter.append('g').attr('class', 'tl-thresholds');
  laneEnter.append('g').attr('class', 'tl-sparks');
  laneEnter.append('g').attr('class', 'tl-markers');
  laneEnter.append('g').attr('class', 'tl-value-labels');

  const laneMerge = laneEnter.merge(laneJoin);
  laneMerge
    .attr('transform', (_, i) => `translate(0,${i * LANE_HEIGHT})`)
    .attr('data-resource-key', (d) => d.key)
    .style('display', (_, i) => (i >= firstVisible && i < lastVisible ? null : 'none'));

  laneMerge.select('.tl-lane-bg')
    .attr('x', 0)
    .attr('y', 1)
    .attr('width', innerW)
    .attr('height', LANE_HEIGHT - 2)
    .attr('fill', (_, i) => (i % 2 === 0 ? theme.laneEven : theme.laneOdd))
    .attr('rx', 2);

  const expandX = EXPAND_BTN_CENTER_SVG - margin.left;
  laneMerge.select('.tl-expand').each(function (d) {
    const eg = d3.select(this);
    eg.selectAll('*').remove();
    if (d.laneType !== 'device' || !includePortLanes || !(d.portCount > 0)) {
      eg.attr('display', 'none');
      return;
    }
    eg.attr('display', null);
    const collapsed = !!d.portsCollapsed;
    const btn = eg.append('g')
      .attr('class', 'tl-expand-btn')
      .attr('transform', `translate(${expandX},${LANE_HEIGHT / 2})`)
      .style('cursor', 'pointer');
    btn.append('rect')
      .attr('class', 'tl-expand-hit')
      .attr('x', -(EXPAND_BTN_SIZE / 2 + EXPAND_BTN_PAD))
      .attr('y', -(EXPAND_BTN_SIZE / 2 + EXPAND_BTN_PAD))
      .attr('width', EXPAND_BTN_SIZE + EXPAND_BTN_PAD * 2)
      .attr('height', EXPAND_BTN_SIZE + EXPAND_BTN_PAD * 2)
      .attr('fill', 'transparent');
    btn.append('rect')
      .attr('class', 'tl-expand-face')
      .attr('x', -EXPAND_BTN_SIZE / 2)
      .attr('y', -EXPAND_BTN_SIZE / 2)
      .attr('width', EXPAND_BTN_SIZE)
      .attr('height', EXPAND_BTN_SIZE)
      .attr('rx', 3);
    btn.append('text')
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'middle')
      .attr('font-size', 11)
      .attr('font-weight', 600)
      .attr('fill', '#cbd5e1')
      .attr('pointer-events', 'none')
      .text(collapsed ? '+' : '−');
    btn.append('title').text(
      collapsed
        ? `Expand ${d.portCount} port lane${d.portCount === 1 ? '' : 's'}`
        : `Collapse ${d.portCount} port lane${d.portCount === 1 ? '' : 's'}`,
    );
    btn.on('click', (event) => {
      event.stopPropagation();
      if (onDeviceCollapseToggle) onDeviceCollapseToggle(d.key);
    });
  });

  laneMerge.select('.tl-label')
    .attr('x', includePortLanes ? -EXPAND_LABEL_GAP : -8)
    .attr('y', LANE_HEIGHT / 2)
    .attr('text-anchor', 'end')
    .attr('dominant-baseline', 'middle')
    .attr('fill', (d) => (d.depth ? theme.labelMuted : theme.label))
    .attr('font-size', (d) => (d.depth ? 9 : 10))
    .attr('font-weight', (d) => (d.depth ? 400 : 500))
    .text((d) => {
      if (d.laneType === 'device') {
        const ip = d.mgmt_ip ? ` · ${d.mgmt_ip}` : '';
        if (d.portsCollapsed && d.portCount > 0) return `${d.label}${ip} (${d.portCount})`;
        return `${d.label}${ip}`;
      }
      // Port lanes: bare port numbers (e.g. "1.1") are ambiguous across chassis —
      // append the chassis IP so each lane is self-identifying.
      const cip = d.chassis_ip ? ` · ${d.chassis_ip}` : '';
      return `${d.label}${cip}`;
    });
  laneMerge.select('.tl-label').each(function (d) {
    const full = d.laneType === 'device'
      ? `${d.label}${d.mgmt_ip ? ` (${d.mgmt_ip})` : ''}`
      : `${d.label}${d.chassis_ip ? ` on ${d.chassis_ip}` : ''}`;
    let title = this.querySelector('title');
    if (!title) {
      title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      this.appendChild(title);
    }
    title.textContent = full;
  });

  laneMerge.select('.tl-sep')
    .attr('x1', 0)
    .attr('x2', innerW)
    .attr('y1', LANE_HEIGHT - 1)
    .attr('y2', LANE_HEIGHT - 1)
    .attr('stroke', theme.sep);

  const windowEndIso = data.window?.to;

  laneMerge.each(function (lane, laneIdx) {
    const lg = d3.select(this);
    if (laneIdx < firstVisible || laneIdx >= lastVisible) {
      lg.select('.tl-sparks').selectAll('*').remove();
      lg.select('.tl-events').selectAll('*').remove();
      lg.select('.tl-markers').selectAll('*').remove();
      lg.select('.tl-value-labels').selectAll('*').remove();
      lg.select('.tl-thresholds').selectAll('*').remove();
      return;
    }
    const laneEvents = eventsGrouped[lane.key] || [];
    const ownEvts = ownershipEvents(laneEvents);
    const linkEvts = laneEvents.filter((e) => e.event_type === 'link_up' || e.event_type === 'link_down');
    const patchEvts = laneEvents.filter((e) => (
      e.event_type !== 'port_owned'
      && e.event_type !== 'port_released'
      && e.event_type !== 'link_up'
      && e.event_type !== 'link_down'
    ));

    // Ownership: start/end caps + dashed thread (owner color) — does not fill the lane
    const ownData = showOwnershipVisuals
      ? ownEvts.filter((e) => e.event_type === 'port_owned')
      : [];
    const ownSel = lg.select('.tl-events').selectAll('.tl-own').data(ownData, (e) => `${e.started_at}:${e.payload?.owner || ''}`);
    ownSel.exit().remove();
    const ownEnter = ownSel.enter().append('g').attr('class', 'tl-own');
    ownEnter.append('line').attr('class', 'tl-own-thread');
    ownEnter.append('rect').attr('class', 'tl-own-cap-start');
    ownEnter.append('rect').attr('class', 'tl-own-cap-end');
    ownEnter.append('title');
    const capH = Math.min(18, LANE_HEIGHT - 14);
    const capW = 3;
    const midY = LANE_HEIGHT / 2;
    ownSel.merge(ownEnter).each(function (e) {
      const g = d3.select(this);
      const x0 = x(parseTs(e.started_at));
      const x1 = x(parseTs(e.ended_at || windowEndIso));
      const owner = e.payload?.owner || '';
      const endLabel = e.ended_at ? (e.ended_at || '').slice(0, 16) : 'now';
      g.select('title').text(`${owner} · ${(e.started_at || '').slice(0, 16)} → ${endLabel}`);
      const fill = fillForEvent(e, ownerColors);
      const threadX0 = x0 + capW;
      const threadX1 = Math.max(threadX0 + 2, x1 - capW);
      g.select('.tl-own-thread')
        .attr('x1', threadX0)
        .attr('x2', threadX1)
        .attr('y1', midY)
        .attr('y2', midY)
        .attr('stroke', fill)
        .attr('stroke-width', 1.2)
        .attr('stroke-dasharray', '4 5')
        .attr('opacity', 0.55);
      g.select('.tl-own-cap-start')
        .attr('x', x0)
        .attr('y', midY - capH / 2)
        .attr('width', capW)
        .attr('height', capH)
        .attr('fill', fill)
        .attr('opacity', 0.75)
        .attr('rx', 1);
      g.select('.tl-own-cap-end')
        .attr('x', x1 - capW)
        .attr('y', midY - capH / 2)
        .attr('width', capW)
        .attr('height', capH)
        .attr('fill', fill)
        .attr('opacity', e.ended_at ? 0.65 : 0.35)
        .attr('rx', 1);
    });

    // Link up/down: transition ticks only (opt-in) — no continuous green/red rails
    lg.select('.tl-events').selectAll('.tl-link-item').remove();
    if (showLinkVisuals && linkEvts.length) {
      const linkRailY = LANE_HEIGHT - LINK_RAIL_Y_OFFSET;
      const linkFlat = linkEvts.map((e) => ({
        key: `${e.started_at}:${e.event_type}`,
        t: parseTs(e.started_at),
        event: e,
      }));
      const linkSel = lg.select('.tl-events').selectAll('.tl-link-item').data(linkFlat, (d) => d.key);
      linkSel.exit().remove();
      const linkEnter = linkSel.enter().append('g').attr('class', 'tl-link-item');
      linkEnter.append('line').attr('class', 'tl-link-tick');
      linkEnter.append('title');
      linkSel.merge(linkEnter).each(function (d) {
        const g = d3.select(this);
        const label = d.event.event_type === 'link_up' ? 'Link up' : 'Link down';
        g.select('title').text(`${label} · ${(d.event.started_at || '').slice(0, 16)}`);
        const cx = x(d.t);
        g.select('.tl-link-tick')
          .attr('x1', cx)
          .attr('x2', cx)
          .attr('y1', linkRailY - 4)
          .attr('y2', linkRailY + 2)
          .attr('stroke', eventColor(d.event.event_type))
          .attr('stroke-width', 1)
          .attr('opacity', 0.55);
      });
    }

    const evSel = lg.select('.tl-events').selectAll('.tl-ev').data(patchEvts, (e) => `${e.started_at}:${e.event_type}`);
    evSel.exit().remove();
    const evEnter = evSel.enter().append('rect').attr('class', 'tl-ev');
    evEnter.append('title');
    evSel.merge(evEnter)
      .attr('x', (e) => x(parseTs(e.started_at)))
      .attr('y', 4)
      .attr('width', (e) => Math.max(2, x(parseTs(e.ended_at || windowEndIso)) - x(parseTs(e.started_at))))
      .attr('height', 6)
      .attr('fill', (e) => eventColor(e.event_type))
      .attr('opacity', 0.75)
      .attr('rx', 2)
      .select('title')
      .text((e) => `${e.event_type} · ${(e.started_at || '').slice(0, 16)}`);

    const rawBuckets = (data.metric_buckets || {})[lane.key] || {};
    let metricNames = metricsForLane(lane, rawBuckets, enabledMetrics);
    if (ownEvts.length && metricNames.includes('port_ownership')) {
      metricNames = metricNames.filter((m) => m !== 'port_ownership');
    }
    const sparksG = lg.select('.tl-sparks');
    const threshG = lg.select('.tl-thresholds');
    const labelsG = lg.select('.tl-value-labels');

    if (fixedPctScale && metricNames.some((m) => PCT_METRICS.has(m))) {
      const pctY = yScaleForMetric('cpu_pct', [[0, 0]], LANE_HEIGHT, d3, { fixedPctScale });
      const threshData = [PCT_WARN, PCT_CRIT];
      const thSel = threshG.selectAll('.tl-thresh').data(threshData);
      thSel.exit().remove();
      thSel.enter().append('line').attr('class', 'tl-thresh')
        .merge(thSel)
        .attr('x1', 0)
        .attr('x2', innerW)
        .attr('y1', (v) => pctY(v))
        .attr('y2', (v) => pctY(v))
        .attr('stroke', (v) => (v >= PCT_CRIT ? '#ef444444' : '#f59e0b33'))
        .attr('stroke-dasharray', '3 3')
        .attr('stroke-width', 1);
    } else {
      threshG.selectAll('*').remove();
    }

    const sparkData = metricNames.map((name) => {
      let series = rawBuckets[name] || [];
      if (state.brushWinFrom != null) {
        series = rebucketSeries(series, bucketMs, viewFrom, viewTo);
      }
      return { name, series };
    });
    const sparkSel = sparksG.selectAll('.tl-spark').data(sparkData, (d) => d.name);
    sparkSel.exit().remove();
    const sparkEnter = sparkSel.enter().append('g').attr('class', 'tl-spark');
    sparkEnter.append('path').attr('class', 'tl-spark-area');
    sparkEnter.append('path').attr('class', 'tl-spark-line');
    sparkEnter.append('title');

    sparkSel.merge(sparkEnter).each(function (d) {
      const sg = d3.select(this);
      if (!d.series?.length) {
        sg.selectAll('path').attr('d', null);
        sg.select('title').text('');
        return;
      }
      const y = yScaleForMetric(d.name, d.series, LANE_HEIGHT, d3, { fixedPctScale });
      const last = d.series[d.series.length - 1];
      const lastVal = Number(last[1]) || 0;
      const line = d3.line().x((p) => x(parseTs(p[0]))).y((p) => y(p[1]));
      const isPct = PCT_METRICS.has(d.name) && fixedPctScale;
      // Line only for CPU/Mem — filled area was masking ownership/link rails
      sg.select('.tl-spark-area').attr('d', null);
      const isTraffic = d.name === 'bps_in' || d.name === 'bps_out' || d.name === 'link_speed_gbps';
      const isDiscards = d.name === 'input_discards';
      sg.select('.tl-spark-line')
        .attr('fill', 'none')
        .attr('stroke', isPct && lastVal >= PCT_WARN
          ? (lastVal >= PCT_CRIT ? '#ef4444' : '#f59e0b')
          : (METRIC_COLORS[d.name] || '#94a3b8'))
        .attr('stroke-width', isTraffic || isDiscards ? 1.85 : (d.name === 'port_ownership' ? 1.6 : 1.5))
        .attr('stroke-dasharray', strokeDashForMetric(d.name))
        .attr('opacity', METRIC_OPACITY[d.name] ?? (isTraffic || isDiscards ? 0.92 : 0.88))
        .attr('d', line(d.series));
      const tipVal = isPct
        ? `${Math.round(lastVal)}%`
        : (isDiscards ? `${Math.round(lastVal)} drops/interval`
          : (isTraffic ? formatBpsShort(lastVal) : `${lastVal}`));
      sg.select('title').text(`${d.name}: ${tipVal} (latest in window)`);
    });

    const labelMetrics = sparkData.filter((d) => {
      if (!d.series?.length) return false;
      if (PCT_METRICS.has(d.name) && fixedPctScale) return true;
      return d.name === 'bps_in' || d.name === 'bps_out';
    });
    const labelSel = labelsG.selectAll('.tl-val-label').data(labelMetrics, (d) => d.name);
    labelSel.exit().remove();
    labelSel.enter().append('text').attr('class', 'tl-val-label')
      .merge(labelSel)
      .attr('x', (d) => {
        const side = labelSideForMetric(d.name);
        return side.anchor === 'start' ? (side.x ?? 4) : (innerW - 4);
      })
      .attr('y', (d) => {
        const y = yScaleForMetric(d.name, d.series, LANE_HEIGHT, d3, { fixedPctScale });
        const last = Number(d.series[d.series.length - 1][1]) || 0;
        return y(last);
      })
      .attr('text-anchor', (d) => labelSideForMetric(d.name).anchor)
      .attr('dominant-baseline', 'middle')
      .attr('fill', (d) => {
        const last = Number(d.series[d.series.length - 1][1]) || 0;
        if (PCT_METRICS.has(d.name)) return pctOverloadColor(last) || theme.labelMuted;
        return METRIC_COLORS[d.name] || theme.labelMuted;
      })
      .attr('font-size', 10)
      .attr('font-weight', (d) => {
        const last = Number(d.series[d.series.length - 1][1]) || 0;
        return PCT_METRICS.has(d.name) && last >= PCT_WARN ? 700 : 600;
      })
      .text((d) => {
        const last = Number(d.series[d.series.length - 1][1]) || 0;
        if (d.name === 'cpu_pct') return `CPU ${Math.round(last)}%`;
        if (d.name === 'mem_pct') return `Mem ${Math.round(last)}%`;
        if (d.name === 'bps_in') return `↓ ${formatBpsShort(last)}`;
        if (d.name === 'bps_out') return `↑ ${formatBpsShort(last)}`;
        if (d.name === 'input_discards') return `${Math.round(last)} drops`;
        return `${Math.round(last)}`;
      });

    const markersG = lg.select('.tl-markers');
    const markerPoints = [];
    if (showOverloadMarkers) {
      for (const d of sparkData) {
        if (!PCT_METRICS.has(d.name) || !d.series?.length) continue;
        for (const [iso, val] of d.series) {
          const v = Number(val) || 0;
          if (v >= overloadThreshold) {
            markerPoints.push({
              iso,
              val: v,
              metric: d.name,
              level: v >= PCT_CRIT ? 'crit' : 'warn',
              kind: 'overload',
            });
          }
        }
      }
    }
    if (showDiscardMarkers) {
      for (const d of sparkData) {
        if (d.name !== 'input_discards' || !d.series?.length) continue;
        let prevVal = 0;
        let sawFirst = false;
        for (const [iso, val] of d.series) {
          const v = Number(val) || 0;
          if (v > 0 && !sawFirst) {
            markerPoints.push({
              iso,
              val: v,
              metric: d.name,
              level: 'discard',
              kind: 'first_discard',
            });
            sawFirst = true;
          }
          if (v > prevVal && prevVal > 0 && v > 0) {
            markerPoints.push({
              iso,
              val: v,
              metric: d.name,
              level: 'discard-rate',
              kind: 'discard_rate',
            });
          }
          prevVal = v;
        }
      }
    }
    if (markerPoints.length) {
      const mSel = markersG.selectAll('.tl-spike').data(markerPoints, (p) => `${p.kind}:${p.iso}:${p.metric}`);
      mSel.exit().remove();
      const mEnter = mSel.enter().append('line').attr('class', 'tl-spike');
      mEnter.append('title');
      mSel.merge(mEnter)
        .attr('x1', (p) => x(parseTs(p.iso)))
        .attr('x2', (p) => x(parseTs(p.iso)))
        .attr('y1', (p) => {
          const py = yScaleForMetric(p.metric, [[0, p.val]], LANE_HEIGHT, d3, { fixedPctScale })(p.val);
          return Math.max(4, py - 5);
        })
        .attr('y2', (p) => {
          const py = yScaleForMetric(p.metric, [[0, p.val]], LANE_HEIGHT, d3, { fixedPctScale })(p.val);
          return Math.min(LANE_HEIGHT - 4, py + 5);
        })
        .attr('stroke', (p) => {
          if (p.kind === 'first_discard') return '#f43f5e';
          if (p.kind === 'discard_rate') return '#fb7185';
          return p.level === 'crit' ? '#ef4444' : '#f59e0b';
        })
        .attr('stroke-width', (p) => (p.kind === 'first_discard' ? 2.5 : 2))
        .attr('opacity', (p) => (p.kind === 'discard_rate' ? 0.55 : 0.85))
        .attr('stroke-dasharray', (p) => (p.kind === 'discard_rate' ? '2 3' : null))
        .select('title')
        .text((p) => {
          if (p.kind === 'first_discard') return `First input discard: ${Math.round(p.val)} @ ${(p.iso || '').slice(11, 16)}`;
          if (p.kind === 'discard_rate') return `Rising discards: ${Math.round(p.val)} @ ${(p.iso || '').slice(11, 16)}`;
          return `${p.metric} ${Math.round(p.val)}% @ ${(p.iso || '').slice(11, 16)}`;
        });
    } else {
      markersG.selectAll('*').remove();
    }
  });

  const throttledHover = throttleRaf((resourceKey) => {
    if (onHover) onHover(resourceKey);
  });

  laneMerge
    .on('mouseenter', (_, d) => throttledHover(d.key))
    .on('mouseleave', () => throttledHover(null));

  const opacityFor = (d) => {
    if (!state.highlightKey) return 1;
    return (d.key === state.highlightKey || d.parent === state.highlightKey) ? 1 : 0.35;
  };
  laneMerge.attr('opacity', opacityFor);

  const brush = d3.brushX()
    .extent([[0, 0], [innerW, innerH]])
    .on('end', (ev) => {
      if (!ev.selection) {
        state.brushWinFrom = null;
        state.brushWinTo = null;
        if (onBrushChange) onBrushChange(null, null);
        if (onBrushFilter) onBrushFilter(null, null, null);
        renderTimelinePanel(el, state.loadedData, state.renderOpts || renderOpts);
        return;
      }
      const [x0, x1] = ev.selection;
      const bFrom = x.invert(x0);
      const bTo = x.invert(x1);
      state.brushWinFrom = bFrom.getTime();
      state.brushWinTo = bTo.getTime();
      if (onBrushFilter) {
        const active = activeResourceKeysInRange(state.loadedData, state.brushWinFrom, state.brushWinTo);
        onBrushFilter(bFrom, bTo, active);
      }
      const needsRefetch = state.brushWinFrom < state.loadedWinFrom || state.brushWinTo > state.loadedWinTo;
      if (needsRefetch && onBrushChange) {
        onBrushChange(bFrom, bTo);
      } else {
        renderTimelinePanel(el, state.loadedData, state.renderOpts || renderOpts);
      }
    });

  state.brushG.call(brush);
  state.brushG.selectAll('.overlay').attr('cursor', 'crosshair');

  if (!scrollEl._tlScrollBound) {
    scrollEl._tlScrollBound = true;
    scrollEl.addEventListener('scroll', throttleRaf(() => {
      const st = el.__tlState;
      if (st?.loadedData) {
        renderTimelinePanel(el, st.loadedData, st.renderOpts || {});
      }
    }));
  }

  function setHighlight(resourceKey) {
    state.highlightKey = resourceKey;
    state.lanesG.selectAll('.tl-lane').attr('opacity', opacityFor);
  }

  return {
    setHighlight,
    destroy: () => {
      el.innerHTML = '';
      delete el.__tlState;
    },
  };
}
