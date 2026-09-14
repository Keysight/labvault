/**
 * Fetch and normalize lab usage payloads for graph views.
 */

import {
  transformFleetSeries,
  transformSeries,
  latestValue,
  mergeFactorForWindow,
} from './usage-data-mode.js';

const WINDOW_HOURS = { '4h': 4, '24h': 24, '7d': 168 };

function windowQuery(preset) {
  const hours = WINDOW_HOURS[preset] || 24;
  const to = new Date();
  const from = new Date(to.getTime() - hours * 3600 * 1000);
  const bucket = preset === '7d' ? '1h' : preset === '4h' ? '5m' : '15m';
  return {
    insights: `window=${encodeURIComponent(preset)}`,
    timeline: `from=${encodeURIComponent(from.toISOString())}&to=${encodeURIComponent(to.toISOString())}&bucket=${bucket}`,
    bucket,
  };
}

async function fetchWithTimeout(url, ms = 20000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { credentials: 'same-origin', signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchGraphData({ insightsUrl, timelineUrl, fabricUrl, windowPreset = '24h' }) {
  const q = windowQuery(windowPreset);
  const [insightsRes, timelineRes, fabricRes] = await Promise.all([
    fetchWithTimeout(`${insightsUrl}?${q.insights}`),
    fetchWithTimeout(`${timelineUrl}?${q.timeline}`),
    fetchWithTimeout(`${fabricUrl}?live=1&lldp=1&ocs=1&reservations=1`),
  ]);
  if (!insightsRes.ok) throw new Error(`insights HTTP ${insightsRes.status}`);
  if (!timelineRes.ok) throw new Error(`timeline HTTP ${timelineRes.status}`);
  const insights = await insightsRes.json();
  const timeline = timelineRes.ok ? await timelineRes.json() : null;
  const fabric = fabricRes.ok ? await fabricRes.json() : null;
  return { insights, timeline, fabric, windowPreset, bucket: q.bucket };
}

function applyBpsModeToPort(port, mode) {
  if (!port || mode === 'instant' || mode === 'timeseries') return port;
  if (mode === 'timeseries_avg') {
    return {
      ...port,
      bps_in: port.bps_in_avg ?? port.bps_in,
      bps_out: port.bps_out_avg ?? port.bps_out,
      bps_in_peak: port.bps_in_peak,
      bps_out_peak: port.bps_out_peak,
    };
  }
  return port;
}

function applyBpsModeToPcpuGroup(ch, mode) {
  if (!ch || mode === 'instant' || mode === 'timeseries') return ch;
  if (mode === 'timeseries_avg') {
    return {
      ...ch,
      bps_in_total: ch.bps_in_avg_total ?? ch.bps_in_total,
      bps_out_total: ch.bps_out_avg_total ?? ch.bps_out_total,
      ports: (ch.ports || []).map((p) => applyBpsModeToPort(p, mode)),
    };
  }
  return ch;
}

const INSTANT_FRESH_MS = 20 * 60 * 1000;

function parseSampleTs(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

function portIsLiveNow(port, windowToMs) {
  if (port.fresh === true) return true;
  if (port.fresh === false) return false;
  const ts = parseSampleTs(port.last_sample_at);
  if (ts == null) return false;
  const ref = windowToMs || Date.now();
  return (ref - ts) <= INSTANT_FRESH_MS;
}

export function filterPcpuFleetForMode(fleet, mode, windowTo) {
  if (mode !== 'instant' || !fleet?.length) return fleet || [];
  const hasLive = fleet.some((rg) => (rg.ports || []).some((p) => p.fresh === true));
  if (!hasLive) return fleet;
  return fleet.map((rg) => {
    const ports = (rg.ports || []).filter((p) => p.fresh === true);
    if (!ports.length) return null;
    return {
      ...rg,
      ports,
      port_count: ports.length,
      owned_count: ports.filter((p) => p.owned).length,
    };
  }).filter(Boolean);
}

export function applyDataModeToInsights(insights, mode, windowPreset) {
  const mergeFactor = mergeFactorForWindow(windowPreset);
  const ts = insights.time_series || {};
  const fleet = transformFleetSeries(ts.fleet, mode);

  const devices = (insights.devices || []).map((d) => {
    const m = d.metrics || {};
    if (mode === 'instant') {
      return {
        ...d,
        metrics: {
          cpu_pct: m.cpu_pct,
          mem_pct: m.mem_pct,
          bps_total: m.bps_total,
          ports_owned_pct: m.ports_owned_pct,
          ports_traffic_pct: m.ports_traffic_pct,
        },
      };
    }
    const devTs = (ts.devices || []).find((x) => x.resource_key === d.resource_key) || {};
    return {
      ...d,
      metrics: {
        cpu_pct: latestValue(devTs.cpu_pct) ?? m.cpu_pct,
        mem_pct: latestValue(devTs.mem_pct) ?? m.mem_pct,
        bps_total: latestValue(devTs.bps_total) ?? m.bps_total,
        ports_owned_pct: m.ports_owned_pct,
        ports_traffic_pct: m.ports_traffic_pct,
      },
      series: {
        cpu_pct: transformSeries(devTs.cpu_pct, mode, { mergeFactor }),
        mem_pct: transformSeries(devTs.mem_pct, mode, { mergeFactor }),
        bps_total: transformSeries(devTs.bps_total, mode, { mergeFactor }),
      },
    };
  });

  return {
    ...insights,
    time_series: { fleet, devices: ts.devices },
    devices,
    pcpu_fleet: filterPcpuFleetForMode(
      (insights.pcpu_fleet || []).map((ch) => applyBpsModeToPcpuGroup(ch, mode)),
      mode,
      insights.window?.to,
    ),
    _mode: mode,
  };
}

export function applyDataModeToTimeline(timeline, mode, windowPreset) {
  if (!timeline?.metric_buckets) return timeline;
  const mergeFactor = mergeFactorForWindow(windowPreset);
  const buckets = {};
  Object.entries(timeline.metric_buckets).forEach(([rk, metrics]) => {
    buckets[rk] = {};
    Object.entries(metrics || {}).forEach(([metric, pts]) => {
      buckets[rk][metric] = transformSeries(pts, mode, { mergeFactor });
    });
  });
  return { ...timeline, metric_buckets: buckets, _mode: mode };
}

export function flattenEvents(timeline) {
  const events = timeline?.events || {};
  const out = [];
  Object.entries(events).forEach(([rk, list]) => {
    (list || []).forEach((ev) => {
      const ts = ev.started_at || ev.ended_at;
      if (!ts) return;
      out.push({ resource_key: rk, ts: new Date(ts).getTime(), type: ev.event_type, payload: ev.payload });
    });
  });
  return out.sort((a, b) => a.ts - b.ts);
}

export function deviceRowsFromInsights(insights) {
  return (insights.devices || []).map((d) => ({
    key: d.resource_key,
    label: d.label || d.resource_key,
    cpu: d.metrics?.cpu_pct ?? 0,
    mem: d.metrics?.mem_pct ?? 0,
    owned: d.metrics?.ports_owned_pct ?? 0,
    traffic: d.metrics?.ports_traffic_pct ?? 0,
    bps: d.metrics?.bps_total ?? 0,
  }));
}
