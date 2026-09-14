/**
 * Lab Pulse — experimental usage insights dashboards.
 * Consumes usage-insights.json; does not touch the main Usage graph/timeline.
 */

import { mountDataModeToggle, getStoredDataMode } from './usage-data-mode.js?v=20260911d';
import { applyDataModeToInsights } from './usage-graph-data.js?v=20260911e';
import { PROW } from './chassis-layout.js';

const OWNER_PALETTE = [
  '#3b82f6', '#22c55e', '#f59e0b', '#a78bfa', '#ec4899',
  '#14b8a6', '#eab308', '#f97316', '#6366f1', '#84cc16',
];

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function pctColor(v, hot = 75) {
  if (v == null || Number.isNaN(v)) return '#334155';
  if (v >= hot) return '#ef4444';
  if (v >= hot * 0.6) return '#f59e0b';
  if (v >= hot * 0.3) return '#3b82f6';
  return '#22c55e';
}

function heatColor(v, max = 100) {
  if (v == null || Number.isNaN(v)) return '#1e293b';
  const t = Math.max(0, Math.min(1, v / max));
  const r = Math.round(30 + t * 220);
  const g = Math.round(40 + (1 - t) * 80);
  const b = Math.round(60 + (1 - t) * 100);
  return `rgb(${r},${g},${b})`;
}

function fmtBps(v) {
  if (v == null) return '—';
  const n = Number(v);
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} Gbps`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mbps`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kbps`;
  return `${n.toFixed(0)} bps`;
}

function fmtBpsShort(v) {
  if (v == null) return '—';
  const n = Number(v);
  if (!n) return '0';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}G`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}k`;
  return `${n.toFixed(0)}`;
}

const PCPU_VIEW_KEY = 'lab_pulse:pcpu_view';
const PCPU_OWNER_FILTER_KEY = 'lab_pulse:pcpu_owner_filter';
const PCPU_LEGEND_EXPANDED_KEY = 'lab_pulse:pcpu_legend_expanded';
const SWITCH_DISCARD_SCOPE_KEY = 'lab_pulse:switch_discard_scope';

function parseTs(ts) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? null : d.getTime();
}

const CHART_SERIES = [
  { key: 'cpu_pct', label: 'CPU', color: '#f472b6', scale: 'pct', unit: '%' },
  { key: 'mem_pct', label: 'Memory', color: '#a78bfa', scale: 'pct', unit: '%' },
  { key: 'ports_owned_pct', label: 'Owned', color: '#38bdf8', scale: 'pct', unit: '%' },
  { key: 'bps_total', label: 'Traffic', color: '#4ade80', scale: 'bps', unit: 'bps' },
];

function niceCeil(v) {
  if (!(v > 0)) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const n = v / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * base;
}

function fmtClock(ts, spanMs) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (spanMs > 48 * 3600000) {
    return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`;
  }
  return `${hh}:${mm}`;
}

function seriesPoints(fleetSeries, key) {
  return (fleetSeries[key] || [])
    .map((p) => ({ ts: parseTs(p[0]), v: Number(p[1]) }))
    .filter((p) => p.ts != null && !Number.isNaN(p.v));
}

/**
 * Dual-axis time-series chart. Percentage series share a fixed 0–100% left axis;
 * the traffic series uses an independent right axis (auto-scaled, bps-formatted)
 * so a flat traffic line never visually drowns CPU/mem and vice-versa.
 * Renders gridlines, time ticks, end-of-line value tags, and a hover crosshair.
 */
function drawMultiSeriesChart(canvas, fleetSeries, { height = 120, seriesKeys = ['cpu_pct', 'mem_pct'] } = {}) {
  if (!canvas || !fleetSeries) return;
  const active = CHART_SERIES.filter((s) => seriesKeys.includes(s.key));
  const hasBps = active.some((s) => s.scale === 'bps' && seriesPoints(fleetSeries, s.key).length > 0);
  const dpr = window.devicePixelRatio || 1;

  const seriesData = active
    .map((s) => ({ ...s, pts: seriesPoints(fleetSeries, s.key) }))
    .filter((s) => s.pts.length > 0);
  const allTs = seriesData.flatMap((s) => s.pts.map((p) => p.ts));

  const tMin = allTs.length ? Math.min(...allTs) : 0;
  const tMax = allTs.length ? Math.max(...allTs) : 1;
  const span = Math.max(1, tMax - tMin);
  let bpsMax = 1;
  seriesData.forEach((s) => {
    if (s.scale === 'bps') s.pts.forEach((p) => { bpsMax = Math.max(bpsMax, p.v); });
  });
  bpsMax = niceCeil(bpsMax);

  const render = (hoverX) => {
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(320, rect.width || canvas.clientWidth || 640);
    const h = height;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.height = `${h}px`;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#0d1b2a';
    ctx.fillRect(0, 0, w, h);

    const pad = { l: 40, r: hasBps ? 64 : 16, t: 12, b: 24 };
    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;
    const xAt = (ts) => pad.l + ((ts - tMin) / span) * plotW;
    const yPct = (v) => pad.t + plotH - (Math.max(0, Math.min(100, v)) / 100) * plotH;
    const yBps = (v) => pad.t + plotH - (Math.max(0, v) / bpsMax) * plotH;

    if (!seriesData.length) {
      ctx.fillStyle = '#64748b';
      ctx.font = '12px system-ui';
      ctx.textAlign = 'center';
      ctx.fillText('No time-series data in this window yet', w / 2, h / 2);
      ctx.textAlign = 'left';
      return;
    }

    // Horizontal gridlines + left % axis
    ctx.font = '10px system-ui';
    ctx.textBaseline = 'middle';
    for (const pct of [0, 25, 50, 75, 100]) {
      const y = yPct(pct);
      ctx.strokeStyle = pct === 0 ? '#334155' : '#1e293b';
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + plotW, y);
      ctx.stroke();
      ctx.fillStyle = '#64748b';
      ctx.textAlign = 'right';
      ctx.fillText(`${pct}%`, pad.l - 6, y);
    }

    // Right bps axis
    if (hasBps) {
      ctx.fillStyle = '#4ade80';
      ctx.textAlign = 'left';
      for (const frac of [0, 0.5, 1]) {
        const y = yPct(frac * 100);
        ctx.fillText(fmtBps(bpsMax * frac), pad.l + plotW + 6, y);
      }
    }

    // X time ticks
    ctx.fillStyle = '#64748b';
    ctx.textBaseline = 'top';
    const ticks = 4;
    for (let i = 0; i <= ticks; i += 1) {
      const ts = tMin + (span * i) / ticks;
      const x = xAt(ts);
      ctx.textAlign = i === 0 ? 'left' : i === ticks ? 'right' : 'center';
      ctx.fillText(fmtClock(ts, span), Math.max(pad.l, Math.min(pad.l + plotW, x)), pad.t + plotH + 6);
    }

    // Lines
    ctx.textBaseline = 'middle';
    seriesData.forEach((s) => {
      const yFn = s.scale === 'bps' ? yBps : yPct;
      if (s.pts.length >= 2) {
        ctx.beginPath();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        s.pts.forEach((p, i) => {
          const x = xAt(p.ts);
          const y = yFn(p.v);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      }
      // End-of-line value tag
      const last = s.pts[s.pts.length - 1];
      const lx = xAt(last.ts);
      const ly = yFn(last.v);
      ctx.fillStyle = s.color;
      ctx.beginPath();
      ctx.arc(lx, ly, 2.5, 0, Math.PI * 2);
      ctx.fill();
    });

    // Hover crosshair + readout
    if (hoverX != null && hoverX >= pad.l && hoverX <= pad.l + plotW) {
      const tHover = tMin + ((hoverX - pad.l) / plotW) * span;
      ctx.strokeStyle = '#475569';
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(hoverX, pad.t);
      ctx.lineTo(hoverX, pad.t + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      const rows = seriesData.map((s) => {
        let best = null;
        let bestD = Infinity;
        for (const p of s.pts) {
          const d = Math.abs(p.ts - tHover);
          if (d < bestD) { bestD = d; best = p; }
        }
        const txt = s.scale === 'bps' ? fmtBps(best.v) : `${Math.round(best.v)}%`;
        return { color: s.color, label: s.label, txt, p: best };
      });
      rows.forEach((r) => {
        const yFn = (active.find((a) => a.label === r.label)?.scale) === 'bps' ? yBps : yPct;
        const y = yFn(r.p.v);
        ctx.fillStyle = r.color;
        ctx.beginPath();
        ctx.arc(xAt(r.p.ts), y, 3.5, 0, Math.PI * 2);
        ctx.fill();
      });
      // Tooltip box (top-left)
      const boxW = 120;
      const boxH = 14 + rows.length * 14;
      const bx = Math.min(hoverX + 8, w - boxW - 4);
      const by = pad.t + 4;
      ctx.fillStyle = 'rgba(2,8,18,.92)';
      ctx.strokeStyle = '#334155';
      ctx.fillRect(bx, by, boxW, boxH);
      ctx.strokeRect(bx, by, boxW, boxH);
      ctx.fillStyle = '#94a3b8';
      ctx.textAlign = 'left';
      ctx.fillText(fmtClock(tHover, span), bx + 6, by + 8);
      rows.forEach((r, i) => {
        ctx.fillStyle = r.color;
        ctx.fillText(`${r.label}: ${r.txt}`, bx + 6, by + 20 + i * 14);
      });
    }
  };

  render(null);

  if (!canvas.__hoverBound) {
    canvas.__hoverBound = true;
    canvas.addEventListener('mousemove', (ev) => {
      const rect = canvas.getBoundingClientRect();
      if (canvas.__render) canvas.__render(ev.clientX - rect.left);
    });
    canvas.addEventListener('mouseleave', () => { if (canvas.__render) canvas.__render(null); });
  }
  canvas.__render = render;
}

function drawSparkline(canvas, series, color = '#38bdf8', { warnAt = 70 } = {}) {
  if (!canvas) return;
  const pts = (series || []).map((p) => Number(p[1])).filter((v) => !Number.isNaN(v));
  const dpr = window.devicePixelRatio || 1;
  const W = 100;
  const H = 30;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = `${W}px`;
  canvas.style.height = `${H}px`;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!pts.length) {
    ctx.fillStyle = '#475569';
    ctx.font = '9px system-ui';
    ctx.fillText('no data', 2, 18);
    return;
  }
  // Fixed 0–100 scale (these are percentages) so cards are comparable.
  const max = 100;
  const top = 4;
  const bottom = H - 4;
  const xFor = (i) => (pts.length === 1 ? W / 2 : (i / (pts.length - 1)) * (W - 22) + 2);
  const yFor = (v) => bottom - (Math.max(0, Math.min(max, v)) / max) * (bottom - top);
  // Warn baseline
  ctx.strokeStyle = '#3f2a2a';
  ctx.setLineDash([2, 2]);
  ctx.beginPath();
  ctx.moveTo(2, yFor(warnAt));
  ctx.lineTo(W - 20, yFor(warnAt));
  ctx.stroke();
  ctx.setLineDash([]);
  if (pts.length >= 2) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    pts.forEach((v, i) => {
      const x = xFor(i);
      const y = yFor(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  const lastV = pts[pts.length - 1];
  const peak = Math.max(...pts);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(xFor(pts.length - 1), yFor(lastV), 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#cbd5e1';
  ctx.font = '9px system-ui';
  ctx.textAlign = 'right';
  ctx.fillText(`${Math.round(lastV)}%`, W - 1, yFor(lastV) - 3 < 8 ? 10 : yFor(lastV) - 3);
  ctx.fillStyle = '#64748b';
  ctx.fillText(`pk ${Math.round(peak)}`, W - 1, H - 1);
  ctx.textAlign = 'left';
}

function legendHtml(keys, fleetSeries) {
  return CHART_SERIES.filter((s) => keys.includes(s.key)).map((s) => {
    const pts = seriesPoints(fleetSeries, s.key);
    const last = pts.length ? pts[pts.length - 1].v : null;
    const peak = pts.length ? Math.max(...pts.map((p) => p.v)) : null;
    const fmt = s.scale === 'bps' ? fmtBps : (v) => `${Math.round(v)}%`;
    const cur = last != null ? ` <strong style="color:#e2e8f0">${fmt(last)}</strong>` : ' <span style="color:#475569">—</span>';
    const pk = peak != null ? ` <span style="color:#64748b">pk ${fmt(peak)}</span>` : '';
    return `<span><i style="background:${s.color}"></i>${esc(s.label)}${cur}${pk}</span>`;
  }).join('');
}

export function initUsageInsights({ topoId, apiUrl, usageUrl }) {
  const els = {
    window: document.getElementById('ui-window'),
    refresh: document.getElementById('ui-refresh'),
    loading: document.getElementById('ui-loading'),
    error: document.getElementById('ui-error'),
    kpis: document.getElementById('ui-kpis'),
    health: document.getElementById('ui-health'),
    research: document.getElementById('ui-research-list'),
    devices: document.getElementById('ui-device-snapshot'),
    pcpu: document.getElementById('ui-pcpu-grid'),
    pcpuLegend: document.getElementById('ui-pcpu-legend'),
    pcpuOwnerFilter: document.getElementById('ui-pcpu-owner-filter'),
    pcpuViewToggle: document.getElementById('ui-pcpu-view-toggle'),
    rankings: document.getElementById('ui-rankings'),
    waste: document.getElementById('ui-waste-list'),
    activity: document.getElementById('ui-activity'),
    heatCanvas: document.getElementById('ui-heat-canvas'),
    heatLegend: document.getElementById('ui-heat-legend'),
    fleetStrip: document.getElementById('ui-fleet-strip'),
    fleetLegend: document.getElementById('ui-fleet-legend'),
    trendsFleet: document.getElementById('ui-trends-fleet'),
    trendsFleetLegend: document.getElementById('ui-trends-fleet-legend'),
    trendsDevices: document.getElementById('ui-trends-devices'),
    stressSvg: document.getElementById('ui-stress-graph'),
    graphDetail: document.getElementById('ui-graph-detail'),
    graphHint: document.getElementById('ui-graph-hint'),
    bottlenecks: document.getElementById('ui-bottlenecks'),
    switchDiscards: document.getElementById('ui-switch-discards'),
    discardMappedOnly: document.getElementById('ui-discard-mapped-only'),
    discardRefresh: document.getElementById('ui-discard-refresh'),
    dutDrops: document.getElementById('ui-dut-drops'),
    dutSummary: document.getElementById('ui-dut-summary'),
    dutCard: document.getElementById('ui-dut-card'),
    dutMappedOnly: document.getElementById('ui-dut-mapped-only'),
    dutTab: document.getElementById('ui-dut-tab'),
    tabs: document.querySelectorAll('.ui-view-tab'),
    panels: document.querySelectorAll('.ui-panel'),
  };

  let lastData = null;
  let rawData = null;
  let dataMode = getStoredDataMode();
  let pcpuView = sessionStorage.getItem(PCPU_VIEW_KEY) || 'compact';
  let pcpuOwnerFilter = sessionStorage.getItem(PCPU_OWNER_FILTER_KEY) || '';
  let pcpuLegendExpanded = sessionStorage.getItem(PCPU_LEGEND_EXPANDED_KEY) === '1';
  let windowPreset = '24h';
  let graphSim = null;
  let loadGen = 0;
  let warmingTries = 0;
  let switchDiscardMappedOnly = sessionStorage.getItem(SWITCH_DISCARD_SCOPE_KEY) === 'mapped';
  let dutMappedOnly = false;

  function sessionCacheKey() {
    return `lab_pulse:v7:${topoId}:${windowPreset}`;
  }

  function isSnapshotWarming(data) {
    return Boolean(data && data.health && data.health.reason === 'snapshot_warming');
  }

  function readSessionCache() {
    try {
      const raw = sessionStorage.getItem(sessionCacheKey());
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed?.data || Date.now() - (parsed.at || 0) > 45 * 1000) return null;
      if (isSnapshotWarming(parsed.data)) return null;
      return parsed.data;
    } catch {
      return null;
    }
  }

  function writeSessionCache(data) {
    if (isSnapshotWarming(data)) return;
    try {
      sessionStorage.setItem(sessionCacheKey(), JSON.stringify({ at: Date.now(), data }));
    } catch {
      /* quota */
    }
  }

  async function fetchInsightsJson(force) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 20000);
    try {
      const res = await fetch(`${apiUrl}?window=${encodeURIComponent(windowPreset)}`, {
        credentials: 'same-origin',
        cache: force ? 'no-store' : 'default',
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function deferHeavy(fn) {
    if (typeof requestIdleCallback === 'function') {
      requestIdleCallback(fn, { timeout: 400 });
    } else {
      setTimeout(fn, 0);
    }
  }

  if (els.discardMappedOnly) {
    els.discardMappedOnly.checked = switchDiscardMappedOnly;
    els.discardMappedOnly.addEventListener('change', () => {
      switchDiscardMappedOnly = !!els.discardMappedOnly.checked;
      sessionStorage.setItem(SWITCH_DISCARD_SCOPE_KEY, switchDiscardMappedOnly ? 'mapped' : 'all');
      if (lastData) renderSwitchDiscards(lastData.switch_input_discards || [], usageUrl);
    });
  }
  if (els.discardRefresh) {
    els.discardRefresh.addEventListener('click', () => loadInsights());
  }
  if (els.dutMappedOnly) {
    els.dutMappedOnly.addEventListener('change', () => {
      dutMappedOnly = !!els.dutMappedOnly.checked;
      if (lastData) renderDutDrops(lastData.dut_drops || [], usageUrl);
    });
  }

  if (els.pcpuViewToggle) {
    els.pcpuViewToggle.querySelectorAll('[data-pcpu-view]').forEach((btn) => {
      btn.classList.toggle('on', btn.dataset.pcpuView === pcpuView);
      btn.addEventListener('click', () => {
        pcpuView = btn.dataset.pcpuView || 'compact';
        sessionStorage.setItem(PCPU_VIEW_KEY, pcpuView);
        els.pcpuViewToggle.querySelectorAll('[data-pcpu-view]').forEach((b) => {
          b.classList.toggle('on', b.dataset.pcpuView === pcpuView);
        });
        if (lastData) renderPcpu(lastData.pcpu_fleet || []);
      });
    });
  }

  if (els.pcpuOwnerFilter) {
    els.pcpuOwnerFilter.addEventListener('change', () => {
      pcpuOwnerFilter = els.pcpuOwnerFilter.value || '';
      sessionStorage.setItem(PCPU_OWNER_FILTER_KEY, pcpuOwnerFilter);
      if (lastData) renderPcpu(lastData.pcpu_fleet || []);
    });
  }

  if (els.pcpuLegend) {
    els.pcpuLegend.addEventListener('click', (ev) => {
      const expandBtn = ev.target.closest('[data-legend-expand]');
      if (expandBtn) {
        pcpuLegendExpanded = expandBtn.dataset.legendExpand === 'more';
        sessionStorage.setItem(PCPU_LEGEND_EXPANDED_KEY, pcpuLegendExpanded ? '1' : '');
        if (lastData) renderPcpu(lastData.pcpu_fleet || []);
        return;
      }
      const chip = ev.target.closest('[data-owner-filter]');
      if (!chip) return;
      const next = chip.dataset.ownerFilter || '';
      pcpuOwnerFilter = pcpuOwnerFilter === next ? '' : next;
      sessionStorage.setItem(PCPU_OWNER_FILTER_KEY, pcpuOwnerFilter);
      if (els.pcpuOwnerFilter) els.pcpuOwnerFilter.value = pcpuOwnerFilter;
      if (lastData) renderPcpu(lastData.pcpu_fleet || []);
    });
  }

  mountDataModeToggle(document.getElementById('ui-data-mode'), {
    initial: dataMode,
    onChange: (m) => {
      dataMode = m;
      if (rawData) renderAll(applyDataModeToInsights(rawData, dataMode, windowPreset));
    },
  });

  if (els.dutDrops) {
    els.dutDrops.addEventListener('click', (ev) => {
      const chip = ev.target.closest('[data-pcpu-switch]');
      if (chip) {
        const owner = chip.dataset.pcpuOwner;
        if (owner) switchToPcpuWithOwner(owner);
        return;
      }
      if (ev.target.closest('.ui-dut-summary-chip')) {
        const tab = document.querySelector('[data-view="dut-drops"]');
        if (tab) tab.click();
      }
    });
  }
  if (els.pcpu) {
    els.pcpu.addEventListener('click', (ev) => {
      if (ev.target.closest('.ui-pcpu-dut-badge')) {
        const tab = document.querySelector('[data-view="dut-drops"]');
        if (tab) tab.click();
      }
    });
  }

  els.tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const view = tab.dataset.view;
      els.tabs.forEach((t) => t.classList.toggle('on', t === tab));
      els.panels.forEach((p) => {
        const on = p.dataset.panel === view;
        p.classList.toggle('on', on);
        p.hidden = !on;
      });
      if (view === 'heatmap' && lastData) drawHeatMatrix(lastData.heat_matrix || []);
      if (view === 'graph' && lastData) drawStressGraph(lastData.stress_graph || {});
      if (view === 'pcpu') requestAnimationFrame(() => layoutPcpuCompactGrid());
    });
  });

  if (typeof ResizeObserver !== 'undefined' && els.pcpu) {
    const pcpuLayoutObs = new ResizeObserver(() => layoutPcpuCompactGrid());
    pcpuLayoutObs.observe(els.pcpu);
    const mainEl = document.getElementById('ui-main');
    if (mainEl) pcpuLayoutObs.observe(mainEl);
  }

  async function load({ force = false } = {}) {
    els.error.hidden = true;
    windowPreset = els.window.value || '24h';
    const gen = ++loadGen;

    if (!force) {
      const cached = readSessionCache();
      if (cached) {
        rawData = cached;
        lastData = applyDataModeToInsights(rawData, dataMode, windowPreset);
        els.loading.hidden = true;
        renderAll(lastData, { deferHeavyPanels: true });
      } else {
        els.loading.hidden = false;
      }
    } else {
      els.loading.hidden = false;
    }

    let keepLoading = false;
    try {
      const data = await fetchInsightsJson(force);
      if (gen !== loadGen) return;

      rawData = data;
      writeSessionCache(data);
      if (rawData.ok === false && rawData.error) {
        els.error.textContent = rawData.error;
        els.error.hidden = false;
      }
      lastData = applyDataModeToInsights(rawData, dataMode, windowPreset);
      renderAll(lastData, { deferHeavyPanels: true });
      if (isSnapshotWarming(rawData) && warmingTries < 8) {
        warmingTries += 1;
        keepLoading = true;
        setTimeout(() => {
          if (gen === loadGen) load({ force: true });
        }, 8000);
      } else {
        warmingTries = 0;
      }
    } catch (e) {
      if (gen !== loadGen) return;
      if (!lastData) {
        const msg = e.name === 'AbortError' ? 'timed out' : e.message;
        els.error.textContent = `Failed to load insights: ${msg}`;
        els.error.hidden = false;
      }
      if (warmingTries < 8) {
        warmingTries += 1;
        keepLoading = true;
        setTimeout(() => {
          if (gen === loadGen) load({ force: true });
        }, 8000);
      }
    } finally {
      if (gen === loadGen && !keepLoading) els.loading.hidden = true;
    }
  }

  function renderAll(data, { deferHeavyPanels = false } = {}) {
    renderHealth(data.health);
    renderKpis(data.summary || {});
    renderResearch(data.research_notes || []);
    renderFleetCharts(data.time_series || {});
    renderDevices(data.devices || [], data.time_series || {});
    renderSwitchDiscards(data.switch_input_discards || [], usageUrl);
    renderDutDrops(data.dut_drops || [], usageUrl);
    renderDutCard(data.dut_drops || []);
    renderBottlenecks(data.bottlenecks || [], usageUrl);

    const hasDutDrops = (data.dut_drops || []).length > 0;
    if (els.dutTab) els.dutTab.style.display = hasDutDrops ? '' : 'none';

    const heavy = () => {
      renderPcpu(data.pcpu_fleet || []);
      renderRankings(data.rankings || {});
      renderWaste(data.waste_signals || [], data.summary || {});
      renderActivity(data.activity || {});
      renderTrendDevices(data.time_series || {});
      const active = document.querySelector('.ui-panel.on');
      if (active?.dataset.panel === 'heatmap') drawHeatMatrix(data.heat_matrix || []);
      if (active?.dataset.panel === 'graph') drawStressGraph(data.stress_graph || {});
    };

    if (deferHeavyPanels) deferHeavy(heavy);
    else heavy();
  }

  function renderFleetCharts(ts) {
    const fleet = ts.fleet || {};
    drawMultiSeriesChart(els.fleetStrip, fleet, { height: 120, seriesKeys: ['cpu_pct', 'mem_pct', 'ports_owned_pct'] });
    if (els.fleetLegend) {
      els.fleetLegend.innerHTML = legendHtml(['cpu_pct', 'mem_pct', 'ports_owned_pct'], fleet);
    }
    drawMultiSeriesChart(els.trendsFleet, fleet, {
      height: 220,
      seriesKeys: ['cpu_pct', 'mem_pct', 'ports_owned_pct', 'bps_total'],
    });
    if (els.trendsFleetLegend) {
      els.trendsFleetLegend.innerHTML = legendHtml(['cpu_pct', 'mem_pct', 'ports_owned_pct', 'bps_total'], fleet);
    }
  }

  function renderTrendDevices(ts) {
    if (!els.trendsDevices) return;
    const devs = ts.devices || [];
    if (!devs.length) {
      els.trendsDevices.innerHTML = '<p class="ui-hint">No per-device CPU/memory series in this window. These populate once the collector samples chassis controllers.</p>';
      return;
    }
    els.trendsDevices.innerHTML = devs.map((d, i) => `
      <div class="ui-trend-card">
        <h3>${esc(d.label)}</h3>
        <div class="ui-trend-spark"><span class="ui-trend-tag" style="color:#f472b6">CPU</span><canvas class="ui-trend-canvas" data-idx="${i}" data-metric="cpu"></canvas></div>
        <div class="ui-trend-spark"><span class="ui-trend-tag" style="color:#a78bfa">MEM</span><canvas class="ui-trend-canvas" data-idx="${i}" data-metric="mem"></canvas></div>
      </div>`).join('');
    els.trendsDevices.querySelectorAll('.ui-trend-canvas').forEach((c) => {
      const idx = Number(c.dataset.idx);
      const d = devs[idx];
      if (c.dataset.metric === 'mem') drawSparkline(c, d.mem_pct, '#a78bfa', { warnAt: 80 });
      else drawSparkline(c, d.cpu_pct, '#f472b6', { warnAt: 75 });
    });
  }

  function renderHealth(health) {
    const el = els.health;
    if (!el) return;
    const notes = (health && health.notes) || [];
    if (!health || (health.has_any && !notes.length)) {
      el.hidden = true;
      el.innerHTML = '';
      return;
    }
    el.hidden = false;
    const sig = health.signals || {};
    const chips = [
      ['Chassis CPU', sig.chassis_cpu], ['Chassis MEM', sig.chassis_mem],
      ['Port traffic', sig.port_traffic], ['Ownership', sig.port_ownership],
      ['Link errors', sig.link_errors], ['Switch discards', sig.switch_discards],
      ['PCPU', sig.pcpu], ['Events', sig.events],
    ].map(([lbl, on]) =>
      `<span class="ui-health-chip ${on ? 'on' : 'off'}">${on ? '✓' : '○'} ${esc(lbl)}</span>`,
    ).join('');
    const noteHtml = notes.length
      ? `<ul class="ui-health-notes">${notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`
      : '';
    el.innerHTML = `
      <div class="ui-health-head"><i class="fas fa-database"></i> Data collected in this window</div>
      <div class="ui-health-chips">${chips}</div>
      ${noteHtml}`;
  }

  function fmtDropTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function renderSwitchDiscards(rows, usageLink) {
    if (!els.switchDiscards) return;
    const filtered = (rows || []).filter((r) => {
      if (!switchDiscardMappedOnly) return true;
      return r.is_fabric_mapped;
    });
    if (!filtered.length) {
      els.switchDiscards.innerHTML = '<p class="ui-hint">No input discards in this window'
        + (switchDiscardMappedOnly ? ' on topology-mapped ports' : '')
        + ' — clean fabric baseline or collector still warming up (needs two polls).</p>';
      return;
    }
    const usageBase = usageLink || '';
    els.switchDiscards.innerHTML = `<table class="ui-discard-table">
      <thead><tr>
        <th>Switch</th><th>Interface</th><th>Total drops</th><th>First drop</th>
        <th>Peak / interval</th><th>~Rate / min</th><th></th>
      </tr></thead>
      <tbody>${filtered.map((r) => {
        const parent = r.parent ? esc(r.parent) : '';
        const drill = r.resource_key && usageBase
          ? `${usageBase}?resource=${encodeURIComponent(r.resource_key)}`
          : usageBase;
        return `<tr>
          <td>${esc(r.chassis_ip || parent || '—')}</td>
          <td>${esc(r.iface_label || r.label)}${r.is_fabric_mapped ? '' : ' <span class="ui-hint">(raw)</span>'}</td>
          <td>${esc(r.total_drops)}</td>
          <td>${esc(fmtDropTime(r.first_drop_at))}</td>
          <td>${esc(r.peak_interval_drops)}</td>
          <td>${esc(r.drop_rate_per_min)}</td>
          <td>${drill ? `<a href="${esc(drill)}" style="color:#38bdf8">Timeline</a>` : ''}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
  }

  function switchToPcpuWithOwner(owner) {
    if (!owner) return;
    pcpuOwnerFilter = owner;
    sessionStorage.setItem(PCPU_OWNER_FILTER_KEY, owner);
    if (els.pcpuOwnerFilter) els.pcpuOwnerFilter.value = owner;
    const pcpuTab = document.querySelector('[data-view="pcpu"]');
    if (pcpuTab) pcpuTab.click();
    if (lastData) renderPcpu(lastData.pcpu_fleet || []);
  }

  function applyDutMappedFilter(dutList) {
    if (!dutMappedOnly) return dutList;
    return dutList
      .map((dut) => ({
        ...dut,
        ports: (dut.ports || []).filter((p) => p.is_fabric_mapped),
      }))
      .filter((dut) => dut.ports.length > 0);
  }

  function fmtDropTimeShort(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function renderDutDrops(dutList, usageLink) {
    if (!els.dutDrops) return;
    const filtered = applyDutMappedFilter(dutList);
    if (!filtered.length) {
      els.dutDrops.innerHTML = '<p class="ui-hint">No DUT packet drops in this window'
        + (dutMappedOnly ? ' on topology-mapped ports' : '')
        + ' — clean fabric baseline or collector still warming up (needs two polls).</p>';
      return;
    }
    const usageBase = usageLink || '';
    els.dutDrops.innerHTML = filtered.map((dut) => {
      const ip = dut.chassis_ip ? ` <span class="ui-ip">${esc(dut.chassis_ip)}</span>` : '';
      const firstTime = fmtDropTimeShort(dut.first_drop_at);
      return `<div class="ui-dut-card" style="--dut-accent:#f43f5e">
        <div class="ui-dut-header">
          <h3>${esc(dut.label)}${ip}</h3>
          <div class="ui-dut-totals">
            <span><strong>${dut.ports_with_drops}</strong> port${dut.ports_with_drops === 1 ? '' : 's'}</span>
            <span><strong>${esc(dut.total_drops)}</strong> drops</span>
            <span><strong>${esc(dut.drop_rate_per_min)}</strong>/min avg</span>
            <span>First: ${esc(firstTime)}</span>
          </div>
        </div>
        <div>${dut.ports.map((p) => {
          const drill = p.resource_key && usageBase
            ? `${usageBase}?resource=${encodeURIComponent(p.resource_key)}`
            : '';
          const pcpuHtml = p.connected_pcpu
            ? `<span class="ui-dut-pcpu-chip" data-pcpu-switch data-pcpu-owner="${esc(p.connected_pcpu.owner)}" title="Switch to PCPU fleet: ${esc(p.connected_pcpu.owner)}">📡 ${esc(p.connected_pcpu.pcpu_ip || p.connected_pcpu.owner)}</span>`
            : '';
          return `<div class="ui-dut-port-row">
            <span class="ui-dut-iface">${esc(p.iface_label || p.label)}${p.is_fabric_mapped ? '' : ' <span class="ui-hint">(raw)</span>'}</span>
            <span class="ui-dut-val num">${esc(p.total_drops)}</span>
            <span class="ui-dut-val time">${esc(fmtDropTimeShort(p.first_drop_at))}</span>
            <span class="ui-dut-val num">${esc(p.peak_interval_drops)}</span>
            <span class="ui-dut-val rate">${esc(p.drop_rate_per_min)}</span>
            <span>${pcpuHtml}</span>
            <span class="ui-dut-drill">${drill ? `<a href="${esc(drill)}">Timeline</a>` : ''}</span>
          </div>`;
        }).join('')}</div>
      </div>`;
    }).join('');
  }

  function renderDutCard(dutList) {
    if (!els.dutCard || !els.dutSummary) return;
    if (!dutList.length) {
      els.dutCard.classList.remove('has-data');
      return;
    }
    els.dutCard.classList.add('has-data');
    els.dutSummary.innerHTML = `<div class="ui-dut-summary-bar">${dutList.slice(0, 12).map((dut) => {
      const ip = dut.chassis_ip ? ` <span class="ui-ip">${esc(dut.chassis_ip)}</span>` : '';
      return `<div class="ui-dut-summary-chip" title="${esc(dut.label)}: ${dut.total_drops} drops across ${dut.ports_with_drops} ports — click for DUT drops tab">
        <strong>${esc(dut.label)}</strong><span>${esc(dut.total_drops)} drops</span><span>${dut.ports_with_drops}p</span><span>${esc(dut.drop_rate_per_min)}/min</span>${ip}
      </div>`;
    }).join('')}</div>`;
  }

  function renderBottlenecks(items, usageLink) {
    if (!els.bottlenecks) return;
    if (!items.length) {
      els.bottlenecks.innerHTML = '<p class="ui-hint">No bottlenecks detected in this window — fleet looks balanced.</p>';
      return;
    }
    els.bottlenecks.innerHTML = items.map((b) => {
      const sev = b.severity || 'low';
      return `<div class="ui-bn-row sev-${esc(sev)}">
        <span class="ui-bn-badge sev-${esc(sev)}">${esc(sev)}</span>
        <div>
          <strong>${esc(b.label)}</strong>
          <div>${esc(b.detail)}</div>
          ${b.laas_hint ? `<div class="ui-bn-hint"><strong>LaaS:</strong> ${esc(b.laas_hint)}</div>` : ''}
          ${usageLink ? `<div class="ui-hint" style="margin-top:6px"><a href="${esc(usageLink)}" style="color:#38bdf8">Open Usage timeline →</a> for drill-down</div>` : ''}
        </div>
        <span class="ui-bn-score">${b.score != null ? esc(b.score) : '—'}</span>
      </div>`;
    }).join('');
  }

  function ensureD3() {
    if (typeof window.d3 !== 'undefined') return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js';
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('d3 load failed'));
      document.head.appendChild(s);
    });
  }

  function drawStressGraph(graph) {
    const svgEl = els.stressSvg;
    if (!svgEl) return;
    if (typeof d3 === 'undefined') {
      ensureD3().then(() => drawStressGraph(graph)).catch(() => {});
      return;
    }
    const nodes = (graph.nodes || []).map((n) => ({ ...n }));
    const links = (graph.edges || []).map((e) => ({ ...e, source: e.source, target: e.target }));
    if (!nodes.length) {
      d3.select(svgEl).selectAll('*').remove();
      d3.select(svgEl).append('text').attr('x', 20).attr('y', 40).attr('fill', '#64748b').text('No graph data — add devices and fabric links to this topology.');
      return;
    }

    if (els.graphHint && graph.meta?.graph_thinking) {
      els.graphHint.textContent = graph.meta.graph_thinking;
    }

    const width = svgEl.clientWidth || 800;
    const height = svgEl.clientHeight || 420;
    const svg = d3.select(svgEl).attr('viewBox', `0 0 ${width} ${height}`);
    svg.selectAll('*').remove();

    if (graphSim) graphSim.stop();

    const g = svg.append('g');
    svg.call(d3.zoom().scaleExtent([0.3, 3]).on('zoom', (ev) => g.attr('transform', ev.transform)));

    // In-SVG legend so the encoding is self-explanatory
    const legend = svg.append('g').attr('transform', 'translate(12,14)');
    const legendItems = [
      { c: '#22c55e', t: 'low stress' },
      { c: '#3b82f6', t: 'moderate' },
      { c: '#f59e0b', t: 'elevated' },
      { c: '#ef4444', t: 'hot (≥70)' },
    ];
    legendItems.forEach((it, i) => {
      const row = legend.append('g').attr('transform', `translate(0,${i * 15})`);
      row.append('circle').attr('r', 5).attr('cx', 5).attr('cy', 0).attr('fill', it.c);
      row.append('text').attr('x', 16).attr('y', 3).attr('fill', '#94a3b8').attr('font-size', 9).text(it.t);
    });
    const ringRow = legend.append('g').attr('transform', `translate(0,${legendItems.length * 15 + 2})`);
    ringRow.append('circle').attr('r', 5).attr('cx', 5).attr('cy', 0).attr('fill', '#0f172a').attr('stroke', '#fbbf24').attr('stroke-width', 2);
    ringRow.append('text').attr('x', 16).attr('y', 3).attr('fill', '#94a3b8').attr('font-size', 9).text('bridge device');
    legend.append('text').attr('x', 0).attr('y', legendItems.length * 15 + 22).attr('fill', '#64748b').attr('font-size', 9)
      .text('size = stress · big = chassis/switch · small = hot port');

    if (!links.length && nodes.length) {
      svg.append('text').attr('x', width / 2).attr('y', height - 14).attr('text-anchor', 'middle')
        .attr('fill', '#64748b').attr('font-size', 11)
        .text('No fabric links or hot ports yet — nodes show device stress only.');
    }

    const link = g.append('g').attr('stroke-opacity', 0.55).selectAll('line')
      .data(links).join('line')
      .attr('stroke', (d) => (d.type === 'fabric' ? '#64748b' : '#334155'))
      .attr('stroke-width', (d) => Math.max(1, (d.weight || 10) / 25));

    const node = g.append('g').selectAll('g')
      .data(nodes).join('g')
      .attr('cursor', 'pointer')
      .call(d3.drag()
        .on('start', (ev, d) => { if (!ev.active) graphSim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
        .on('end', (ev, d) => { if (!ev.active) graphSim.alphaTarget(0); d.fx = null; d.fy = null; }));

    node.append('circle')
      .attr('r', (d) => (d.type === 'device' ? 10 + (d.stress || 0) / 8 : 5 + (d.stress || 0) / 20))
      .attr('fill', (d) => pctColor(d.stress, 70))
      .attr('stroke', (d) => (d.role === 'bridge' ? '#fbbf24' : '#0f172a'))
      .attr('stroke-width', (d) => (d.role === 'bridge' ? 2.5 : 1));

    node.append('text')
      .text((d) => String(d.label || '').slice(0, 14))
      .attr('x', 12)
      .attr('y', 4)
      .attr('fill', '#94a3b8')
      .attr('font-size', 9)
      .attr('pointer-events', 'none');

    node.on('click', (_, d) => {
      if (!els.graphDetail) return;
      const ipLine = d.mgmt_ip ? `<div class="ui-ip-line">${esc(d.mgmt_ip)}</div>`
        : (d.chassis_ip ? `<div class="ui-ip-line">chassis ${esc(d.chassis_ip)}</div>` : '');
      els.graphDetail.innerHTML = `
        <strong>${esc(d.label)}</strong>
        ${ipLine}
        <div class="ui-detail-stress">${d.stress ?? '—'} stress</div>
        <div>Type: ${esc(d.type)}${d.role ? ` · ${esc(d.role)}` : ''}</div>
        ${d.ports_hot != null ? `<div>Hot ports: ${d.ports_hot}</div>` : ''}
        ${d.ports_owned != null ? `<div>Owned ports: ${d.ports_owned}</div>` : ''}
        ${d.owner && d.owner !== 'Free' ? `<div>Owner: <span class="ui-owner">${esc(d.owner)}</span></div>` : ''}
        ${d.parent ? `<div>Chassis: ${esc(d.parent)}</div>` : ''}
        <p class="ui-hint" style="margin-top:8px">Bridge nodes (gold ring) connect multiple fabric peers — saturation here affects many LaaS paths.</p>`;
    });

    graphSim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id((d) => d.id).distance(80).strength(0.4))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide().radius((d) => (d.type === 'device' ? 22 : 12)))
      .on('tick', () => {
        link
          .attr('x1', (d) => d.source.x)
          .attr('y1', (d) => d.source.y)
          .attr('x2', (d) => d.target.x)
          .attr('y2', (d) => d.target.y);
        node.attr('transform', (d) => `translate(${d.x},${d.y})`);
      });
  }

  function renderKpis(s) {
    const ownedPct = s.ports_owned_pct ?? 0;
    const trafficPct = s.ports_traffic_pct ?? 0;
    const mismatch = ownedPct - trafficPct;
    const cards = [
      { val: `${ownedPct}%`, lbl: 'Ports owned', cls: ownedPct > 60 ? 'ui-kpi-warn' : '', accent: '#38bdf8' },
      { val: `${trafficPct}%`, lbl: 'Ports with traffic', cls: trafficPct < 20 ? 'ui-kpi-warn' : 'ui-kpi-good', accent: '#4ade80' },
      { val: s.waste_owned_idle ?? 0, lbl: 'Owned but idle', cls: (s.waste_owned_idle || 0) > 0 ? 'ui-kpi-hot' : '', accent: '#f87171' },
      { val: s.pcpu_hosts_hot ?? 0, lbl: 'Hot PCPU hosts', cls: (s.pcpu_hosts_hot || 0) > 0 ? 'ui-kpi-hot' : '', accent: '#fb923c' },
      { val: s.avg_chassis_cpu_pct != null ? `${s.avg_chassis_cpu_pct}%` : '—', lbl: 'Avg chassis CPU', cls: '', accent: '#a78bfa' },
      { val: s.avg_chassis_mem_pct != null ? `${s.avg_chassis_mem_pct}%` : '—', lbl: 'Avg chassis MEM', cls: '', accent: '#f472b6' },
      { val: s.hot_ports_cpu ?? 0, lbl: 'Ports CPU hot', cls: '', accent: '#ef4444' },
      { val: mismatch > 15 ? `+${mismatch.toFixed(0)}%` : `${mismatch.toFixed(0)}%`, lbl: 'Own vs traffic gap', cls: mismatch > 15 ? 'ui-kpi-hot' : '', accent: '#fbbf24' },
    ];
    els.kpis.innerHTML = cards.map((c) => `
      <div class="ui-kpi ${c.cls}" style="--kpi-accent:${c.accent}">
        <div class="ui-kpi-val">${esc(c.val)}</div>
        <div class="ui-kpi-lbl">${esc(c.lbl)}</div>
      </div>`).join('');
  }

  function renderResearch(notes) {
    els.research.innerHTML = notes.map((n) => `
      <div class="ui-research-item">
        <strong>${esc(n.title)}</strong>
        <span>${esc(n.body)}</span>
      </div>`).join('');
  }

  function renderDevices(devices, ts) {
    const devSeries = {};
    (ts.devices || []).forEach((d) => { devSeries[d.resource_key] = d; });
    const rows = devices
      .filter((d) => d.ports?.total > 0 || d.metrics?.cpu_pct?.latest != null)
      .slice(0, 12);
    els.devices.innerHTML = rows.length ? rows.map((d, i) => {
      const cpu = d.metrics?.cpu_pct?.latest;
      const mem = d.metrics?.mem_pct?.latest;
      const owned = d.ports?.total ? Math.round(100 * d.ports.owned / d.ports.total) : 0;
      const series = devSeries[d.resource_key];
      const ip = d.mgmt_ip ? `<span class="ui-ip">${esc(d.mgmt_ip)}</span>` : '';
      const discardHint = d.discard_summary
        ? `<span class="ui-discard-badge" title="Input discards in window">${d.discard_summary.total_drops} discards · ${d.discard_summary.ports_with_drops} port(s)</span>`
        : '';
      return `<div class="ui-device-row">
        <span>${esc(d.label)}${ip}${discardHint}</span>
        <span title="CPU">${cpu != null ? cpu + '%' : '—'} CPU</span>
        <span title="Owned ports">${owned}% own</span>
        <canvas class="ui-spark" data-spark="${i}" width="80" height="22"></canvas>
        <div class="ui-bar" title="Memory"><i style="width:${mem ?? 0}%;background:${pctColor(mem, 80)}"></i></div>
      </div>`;
    }).join('') : '<p class="ui-hint">No device metrics in this window yet. Run the metrics collector.</p>';
    rows.forEach((d, i) => {
      const c = els.devices.querySelector(`canvas[data-spark="${i}"]`);
      const series = devSeries[d.resource_key];
      if (c && series) drawSparkline(c, series.cpu_pct, '#f472b6');
    });
  }

  function portOwnerKey(port) {
    if (!port?.owned) return 'Free';
    const o = (port.owner || '').trim();
    if (!o || o === 'Free') return 'Reserved';
    return o;
  }

  function comparePortLabels(a, b) {
    const pa = String(a?.label || '').split(/[./]/).map((x) => parseInt(x, 10) || 0);
    const pb = String(b?.label || '').split(/[./]/).map((x) => parseInt(x, 10) || 0);
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i += 1) {
      const d = (pa[i] || 0) - (pb[i] || 0);
      if (d) return d;
    }
    return String(a?.label || '').localeCompare(String(b?.label || ''));
  }

  function collectOwnersFromFleet(fleet) {
    const owners = new Set();
    for (const rg of fleet) {
      for (const port of rg.ports || []) {
        const key = portOwnerKey(port);
        if (key !== 'Free') owners.add(key);
      }
    }
    return [...owners].sort((a, b) => a.localeCompare(b));
  }

  function portMatchesOwnerFilter(port, filter) {
    if (!filter) return true;
    return portOwnerKey(port) === filter;
  }

  function applyOwnerFilterToFleet(fleet, filter) {
    if (!filter) return fleet;
    return fleet
      .map((rg) => {
        const ports = (rg.ports || []).filter((p) => portMatchesOwnerFilter(p, filter));
        return { ...rg, ports, port_count: ports.length };
      })
      .filter((rg) => (rg.ports || []).length > 0);
  }

  function syncPcpuOwnerFilterSelect(fleet) {
    if (!els.pcpuOwnerFilter) return;
    const owners = collectOwnersFromFleet(fleet);
    const opts = [
      '<option value="">All owners</option>',
      '<option value="Free">Unowned</option>',
      '<option value="Reserved">Reserved</option>',
      ...owners.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`),
    ];
    els.pcpuOwnerFilter.innerHTML = opts.join('');
    if (pcpuOwnerFilter && [...els.pcpuOwnerFilter.options].some((o) => o.value === pcpuOwnerFilter)) {
      els.pcpuOwnerFilter.value = pcpuOwnerFilter;
    } else {
      pcpuOwnerFilter = '';
      els.pcpuOwnerFilter.value = '';
      sessionStorage.setItem(PCPU_OWNER_FILTER_KEY, '');
    }
  }

  function buildOwnerColorMap(owners) {
    const map = { Free: '#334155', Reserved: '#3b82f6' };
    owners.forEach((o, i) => {
      map[o] = OWNER_PALETTE[i % OWNER_PALETTE.length];
    });
    return map;
  }

  function dominantOwnerKey(ports) {
    const counts = {};
    for (const port of ports || []) {
      const key = portOwnerKey(port);
      if (key === 'Free') continue;
      counts[key] = (counts[key] || 0) + 1;
    }
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
    return top ? top[0] : 'Free';
  }

  function groupFleetByChassis(fleet) {
    const byChassis = new Map();
    for (const rg of fleet) {
      const key = rg.resource_key || rg.chassis || 'unknown';
      if (!byChassis.has(key)) {
        byChassis.set(key, {
          chassis: rg.chassis,
          chassis_ip: rg.chassis_ip,
          resource_key: rg.resource_key,
          groups: [],
        });
      }
      byChassis.get(key).groups.push(rg);
    }
    for (const ch of byChassis.values()) {
      ch.groups.sort((a, b) => {
        const ip = String(a.mgmt_ip || '').localeCompare(String(b.mgmt_ip || ''));
        if (ip) return ip;
        return (b.port_count || 0) - (a.port_count || 0);
      });
    }
    return [...byChassis.values()].sort((a, b) => String(a.chassis).localeCompare(String(b.chassis)));
  }

  function ownerFilterChip(o, colorMap, { dense = false } = {}) {
    const active = pcpuOwnerFilter === o;
    const label = o.length > 10 ? `${o.slice(0, 9)}…` : o;
    if (dense) {
      return `<button type="button" class="ui-owner-dot-only ui-pcpu-filter-chip${active ? ' active' : ''}" data-owner-filter="${esc(o)}" title="${esc(o)} — click to filter" style="background:${colorMap[o]}"></button>`;
    }
    return `<button type="button" class="ui-owner-chip ui-pcpu-filter-chip${active ? ' active' : ''}" data-owner-filter="${esc(o)}" title="${esc(o)} — click to filter" style="border-color:${colorMap[o]}"><span class="ui-owner-dot" style="background:${colorMap[o]}"></span>${esc(label)}</button>`;
  }

  function renderPcpuLegend(fleet, colorMap) {
    if (!els.pcpuLegend) return;
    const owners = collectOwnersFromFleet(fleet);
    const maxCollapsed = 8;
    const expanded = pcpuLegendExpanded || owners.length <= maxCollapsed;
    const shown = expanded ? owners : owners.slice(0, maxCollapsed);
    const extra = owners.length - shown.length;
    const staticChips = [
      { key: 'Free', label: 'free', title: 'Unowned' },
      { key: 'Reserved', label: 'rsvd', title: 'Reserved' },
    ].map(({ key, label, title }) => {
      const active = pcpuOwnerFilter === key;
      const color = colorMap[key] || '#334155';
      return `<button type="button" class="ui-owner-chip ui-pcpu-filter-chip${active ? ' active' : ''}" data-owner-filter="${key}" title="${title}"><span class="ui-owner-dot" style="background:${color}"></span>${label}</button>`;
    }).join('');
    let ownerHtml = owners.length
      ? shown.map((o) => ownerFilterChip(o, colorMap, { dense: false })).join('')
      : '<span class="ui-hint inline">No owners</span>';
    if (extra > 0) {
      ownerHtml += `<button type="button" class="ui-pcpu-legend-expand" data-legend-expand="more" title="${esc(owners.slice(maxCollapsed).join(', '))}">+${extra} more</button>`;
    } else if (owners.length > maxCollapsed && expanded) {
      ownerHtml += `<button type="button" class="ui-pcpu-legend-expand" data-legend-expand="less">Fewer</button>`;
    }
    const linkHtml = `<span class="ui-owner-chip ui-pcpu-legend-link" title="Link up"><span class="ui-pcpu-link-dot up"></span>up</span>
      <span class="ui-owner-chip ui-pcpu-legend-link" title="Link down"><span class="ui-pcpu-link-dot down"></span>down</span>
      <span class="ui-owner-chip ui-pcpu-legend-link" title="Link state unknown"><span class="ui-pcpu-link-dot unknown"></span>?</span>
      <span class="ui-owner-chip ui-pcpu-legend-link" title="Unowned port"><span class="ui-pcpu-port-slot unowned mini" aria-hidden="true"></span>free</span>`;
    els.pcpuLegend.classList.remove('dense');
    els.pcpuLegend.classList.toggle('expanded', expanded);
    els.pcpuLegend.innerHTML = `
      <span class="ui-pcpu-legend-label">Owners</span>
      <span class="ui-pcpu-legend-inline ui-pcpu-legend-owners">${staticChips}${ownerHtml}</span>
      <span class="ui-pcpu-legend-sep" aria-hidden="true"></span>
      <span class="ui-pcpu-legend-inline ui-pcpu-legend-links">${linkHtml}</span>`;
  }

  function linkDotClass(linkUp) {
    if (linkUp === true) return 'up';
    if (linkUp === false) return 'down';
    return 'unknown';
  }

  function linkStateLabel(linkUp) {
    if (linkUp === true) return 'Link up';
    if (linkUp === false) return 'Link down';
    return 'Link unknown';
  }

  function renderTputPeakSub(port, dir) {
    const peak = dir === 'rx' ? port.bps_in_peak : port.bps_out_peak;
    const inst = dir === 'rx' ? port.bps_in : port.bps_out;
    if (peak == null) return '';
    if (inst != null && peak === inst) return '';
    return `<span class="ui-pcpu-tput-peak">peak ${esc(fmtBps(peak))}</span>`;
  }

  function renderPcpuPortCellDetail(port, colorMap) {
    if (!port) {
      return '<span class="ui-pcpu-port-cell empty" aria-hidden="true"></span>';
    }
    const ownerKey = portOwnerKey(port);
    const owned = ownerKey !== 'Free';
    const ownerColor = colorMap[ownerKey] || colorMap.Free;
    const linkCls = linkDotClass(port.link_up);
    const traffic = port.has_traffic || (port.bps_peak || 0) > 1000;
    const rx = fmtBps(port.bps_in);
    const tx = fmtBps(port.bps_out);
    const tip = [
      port.label,
      owned ? ownerKey : 'Unowned',
      linkStateLabel(port.link_up),
      `↓ now ${rx}`,
      `↑ now ${tx}`,
      port.bps_in_peak != null ? `↓ peak ${fmtBps(port.bps_in_peak)}` : '',
      port.bps_out_peak != null ? `↑ peak ${fmtBps(port.bps_out_peak)}` : '',
    ].filter(Boolean).join(' · ');
    return `<div class="ui-pcpu-port-row${traffic ? ' traffic' : ''}" style="--owner-color:${ownerColor}" title="${esc(tip)}">
      <span class="ui-pcpu-port-badge${owned ? ' owned' : ' unowned'}">
        <span class="ui-pcpu-link-dot ${linkCls}"></span>
        <span class="ui-pcpu-port-lbl">${esc(port.label)}</span>
        ${port.has_dut_discard ? '<span class="ui-pcpu-dut-badge" title="DUT packet drops — click to view"></span>' : ''}
      </span>
      <div class="ui-pcpu-port-tput-panel">
        <div class="ui-pcpu-tput-metric rx">
          <span class="ui-pcpu-tput-dir">↓ Rx <span class="ui-pcpu-tput-tag">now</span></span>
          <span class="ui-pcpu-tput-val">${esc(rx)}</span>
          ${renderTputPeakSub(port, 'rx')}
        </div>
        <div class="ui-pcpu-tput-metric tx">
          <span class="ui-pcpu-tput-dir">↑ Tx <span class="ui-pcpu-tput-tag">now</span></span>
          <span class="ui-pcpu-tput-val">${esc(tx)}</span>
          ${renderTputPeakSub(port, 'tx')}
        </div>
      </div>
    </div>`;
  }

  function layoutPcpuCompactGrid() {
    if (!els.pcpu || pcpuView !== 'compact') return;
    const grid = els.pcpu;
    const cards = [...grid.querySelectorAll('.ui-pcpu-chassis--compact')];
    if (!cards.length) return;

    const mainEl = document.getElementById('ui-main');
    const availH = Math.max(320, (mainEl?.clientHeight || 520) - 4);
    const availW = Math.max(320, grid.clientWidth || 800);
    const n = cards.length;

    let maxPorts = 0;
    cards.forEach((card) => {
      maxPorts = Math.max(maxPorts, Number(card.dataset.portCount || 0));
    });
    if (!maxPorts) maxPorts = 8;

    const headH = 32;
    const chassisGap = 8;
    const portRowH = 28;
    const gridPad = 16;

    let bestCols = 1;
    let bestPortMin = 76;
    let bestScore = Infinity;

    for (let cols = 1; cols <= n; cols += 1) {
      const chassisRows = Math.ceil(n / cols);
      const cardW = (availW - (cols - 1) * chassisGap) / cols;
      if (cardW < 220) break;

      for (let portMin = 92; portMin >= 68; portMin -= 2) {
        const portsPerRow = Math.max(3, Math.floor((cardW - gridPad) / portMin));
        const portRows = Math.max(1, Math.ceil(maxPorts / portsPerRow));
        const cardH = headH + gridPad + portRows * portRowH + Math.max(0, portRows - 1) * 4;
        const totalH = chassisRows * cardH + (chassisRows - 1) * chassisGap;
        const overflow = Math.max(0, totalH - availH);
        const score = overflow * 12 - cols * 4 + (92 - portMin) * 0.5;
        if (score < bestScore) {
          bestScore = score;
          bestCols = cols;
          bestPortMin = portMin;
        }
        if (overflow === 0) break;
      }
    }

    grid.style.setProperty('--pcpu-chassis-cols', String(bestCols));
    grid.style.setProperty('--pcpu-port-min', `${Math.round(bestPortMin)}px`);
  }

  function renderPcpuPortSlot(port, colorMap, { compact = false, dense = false, balanced = false, rgStart = false } = {}) {
    if (!port) {
      return dense ? '' : '<span class="ui-pcpu-port-slot empty" aria-hidden="true"></span>';
    }
    const ownerKey = portOwnerKey(port);
    const owned = ownerKey !== 'Free';
    const ownerColor = colorMap[ownerKey] || colorMap.Free;
    const linkCls = linkDotClass(port.link_up);
    const traffic = port.has_traffic || (port.bps_peak || 0) > 1000;
    const rx = compact ? fmtBpsShort(port.bps_in) : fmtBps(port.bps_in);
    const tx = compact ? fmtBpsShort(port.bps_out) : fmtBps(port.bps_out);
    const speed = port.speed_gbps;
    const tip = [
      port.label,
      owned ? ownerKey : 'Unowned',
      linkStateLabel(port.link_up),
      speed ? `${speed}G` : '',
      `↓ ${fmtBps(port.bps_in || 0)}`,
      `↑ ${fmtBps(port.bps_out || 0)}`,
    ].filter(Boolean).join(' · ');
    if (dense) {
      return `<span class="ui-pcpu-port-slot${owned ? ' owned' : ' unowned'}${traffic ? ' traffic' : ''} dense${rgStart ? ' rg-start' : ''}"
        style="--owner-color:${ownerColor}"
        title="${esc(tip)}">
        <span class="ui-pcpu-link-dot ${linkCls}"></span>
        <span class="ui-pcpu-port-lbl">${esc(port.label)}</span>
        ${port.has_dut_discard ? '<span class="ui-pcpu-dut-badge" title="DUT packet drops — click to view"></span>' : ''}
        <span class="ui-pcpu-port-tput"><span class="rx">↓${esc(rx)}</span><span class="tx">↑${esc(tx)}</span></span>
      </span>`;
    }
    if (balanced) {
      return `<span class="ui-pcpu-port-slot${owned ? ' owned' : ' unowned'}${traffic ? ' traffic' : ''} compact balanced${rgStart ? ' rg-start' : ''}"
        style="--owner-color:${ownerColor}"
        title="${esc(tip)}">
        <span class="ui-pcpu-link-dot ${linkCls}"></span>
        <span class="ui-pcpu-port-lbl">${esc(port.label)}</span>
        ${port.has_dut_discard ? '<span class="ui-pcpu-dut-badge" title="DUT packet drops — click to view"></span>' : ''}
      </span>`;
    }
    return `<span class="ui-pcpu-port-slot${owned ? ' owned' : ' unowned'}${traffic ? ' traffic' : ''}${compact ? ' compact' : ''}${rgStart ? ' rg-start' : ''}"
      style="--owner-color:${ownerColor}"
      title="${esc(tip)}">
      <span class="ui-pcpu-port-head">
        <span class="ui-pcpu-link-dot ${linkCls}"></span>
        <span class="ui-pcpu-port-lbl">${esc(port.label)}</span>
        ${port.has_dut_discard ? '<span class="ui-pcpu-dut-badge" title="DUT packet drops — click to view"></span>' : ''}
      </span>
      <span class="ui-pcpu-port-tput"><span class="rx" title="Rx">↓${esc(rx)}</span><span class="tx" title="Tx">↑${esc(tx)}</span></span>
    </span>`;
  }

  function chassisVersionPayload(ch) {
    const apps = { ...(ch.applications || {}) };
    let ixos = String(ch.ixos_version || '').trim();
    let ixn = String(ch.ixnetwork_version || '').trim();
    for (const rg of ch.groups || []) {
      const ra = rg.applications || {};
      if (!ixos) ixos = String(rg.ixos_version || ra.IxOS || ra['IxOS Core'] || '').trim();
      if (!ixn) ixn = String(rg.ixnetwork_version || ra.IxNetwork || ra['IxNetwork API Server'] || '').trim();
      for (const [name, ver] of Object.entries(ra)) {
        if (!apps[name]) apps[name] = ver;
      }
    }
    return { ixos_version: ixos, ixnetwork_version: ixn, applications: apps };
  }

  function renderPcpuVersionChips(ver) {
    const apps = ver.applications || {};
    const chips = [];
    const ixos = String(ver.ixos_version || apps.IxOS || apps['IxOS Core'] || '').trim();
    const ixn = String(ver.ixnetwork_version || apps.IxNetwork || apps['IxNetwork API Server'] || '').trim();
    if (ixos) chips.push({ name: 'IxOS', version: ixos, primary: true });
    if (ixn) chips.push({ name: 'IxNetwork', version: ixn, primary: true });
    for (const [name, ver] of Object.entries(apps)) {
      const n = String(name || '').trim();
      const v = String(ver || '').trim();
      if (!n || !v) continue;
      if (/^ixos\b/i.test(n) || /^ixnetwork/i.test(n)) continue;
      chips.push({ name: n, version: v, primary: false });
    }
    if (!chips.length) return '';
    return `<div class="ui-pcpu-rg-versions">${chips.map((c) => (
      `<span class="ui-pcpu-ver-chip${c.primary ? ' primary' : ''}" title="${esc(c.name)} ${esc(c.version)}">`
      + `<span class="ui-pcpu-ver-name">${esc(c.name)}</span>`
      + `<span class="ui-pcpu-ver-val">${esc(c.version)}</span>`
      + '</span>'
    )).join('')}</div>`;
  }

  function renderPcpuRgGroup(rg, colorMap) {
    const poll = !!rg.poll_mode;
    const hot = !poll && ((rg.cpu_pct || 0) >= 75 || (rg.mem_pct || 0) >= 80);
    const ports = (rg.ports && rg.ports.length)
      ? [...rg.ports].sort(comparePortLabels)
      : (rg.sample_ports || []).map((lbl) => ({ label: lbl, owner: 'Free', owned: false, link_up: false }));
    const ownerKey = dominantOwnerKey(ports);
    const groupColor = colorMap[ownerKey] || colorMap.Free;
    const portCount = rg.port_count || ports.length;
    const rgLabel = rg.mgmt_ip ? `RG · PCPU ${rg.mgmt_ip}` : `RG · ${portCount} port${portCount === 1 ? '' : 's'}`;
    const cpuColor = poll ? '#64748b' : pctColor(rg.cpu_pct);
    const cpuLabel = poll
      ? `CPU ${rg.cpu_pct ?? '—'}% <span class="ui-pcpu-poll">idle poll</span>`
      : `CPU ${rg.cpu_pct ?? '—'}%`;
    const listClass = portCount > 6 ? 'ui-pcpu-port-list many' : 'ui-pcpu-port-list';
    const portHtml = ports.length
      ? ports.map((p) => renderPcpuPortCellDetail(p, colorMap)).join('')
      : '<p class="ui-hint inline">No port samples</p>';
    return `<div class="ui-pcpu-rg${hot ? ' hot' : ''}${poll ? ' poll' : ''}" style="--rg-color:${groupColor}">
      <div class="ui-pcpu-rg-head">
        <div class="ui-pcpu-rg-title">
          <span class="ui-pcpu-rg-count">${portCount} port${portCount === 1 ? '' : 's'}</span>
          <span class="ui-pcpu-rg-name">${esc(rgLabel)}</span>
        </div>
        <div class="ui-pcpu-rg-meters">
          <div class="ui-meter compact"><label>${cpuLabel}</label>
            <div class="ui-meter-track"><div class="ui-meter-fill" style="width:${Math.min(100, rg.cpu_pct || 0)}%;background:${cpuColor}"></div></div></div>
          <div class="ui-meter compact"><label>MEM ${rg.mem_pct ?? '—'}%</label>
            <div class="ui-meter-track"><div class="ui-meter-fill" style="width:${Math.min(100, rg.mem_pct || 0)}%;background:${pctColor(rg.mem_pct, 80)}"></div></div></div>
        </div>
      </div>
      <div class="${listClass}">${portHtml}</div>
    </div>`;
  }

  function renderPcpuChassisCompact(ch, colorMap) {
    const allPorts = [];
    for (const rg of ch.groups) {
      for (const p of (rg.ports || [])) {
        allPorts.push({ ...p, _rg: rg.mgmt_ip || '' });
      }
    }
    allPorts.sort(comparePortLabels);
    let lastRg = null;
    const marked = allPorts.map((p) => {
      const rgStart = p._rg !== lastRg;
      lastRg = p._rg;
      return { ...p, rgStart };
    });
    const ip = ch.chassis_ip ? `<span class="ui-ip">${esc(ch.chassis_ip)}</span>` : '';
    const portTotal = marked.length;
    const versionHtml = renderPcpuVersionChips(chassisVersionPayload(ch));
    return `<section class="ui-pcpu-chassis ui-pcpu-chassis--compact" data-port-count="${portTotal}">
      <header class="ui-pcpu-chassis-head ui-pcpu-chassis-head--compact">
        <h3 class="ui-pcpu-chassis-title">${esc(ch.chassis)} ${ip}<span class="ui-pcpu-chassis-meta">${ch.groups.length} RG · ${portTotal}p</span>${versionHtml}</h3>
      </header>
      <div class="ui-pcpu-compact-grid balanced">
        <div class="ui-pcpu-port-flow">
          ${marked.map((p) => renderPcpuPortSlot(p, colorMap, { compact: true, balanced: true, rgStart: !!p.rgStart })).join('')}
        </div>
      </div>
    </section>`;
  }

  function renderPcpu(fleet) {
    if (!fleet.length) {
      if (els.pcpuLegend) els.pcpuLegend.innerHTML = '';
      if (els.pcpuOwnerFilter) els.pcpuOwnerFilter.innerHTML = '<option value="">All owners</option>';
      els.pcpu.innerHTML = '<p class="ui-hint">No PCPU samples yet. AresONE root SSH collection populates port CPU/mem from 10.0.x.x hosts.</p>';
      return;
    }
    syncPcpuOwnerFilterSelect(fleet);
    const filtered = applyOwnerFilterToFleet(fleet, pcpuOwnerFilter);
    const colorMap = buildOwnerColorMap(collectOwnersFromFleet(fleet));
    const detail = pcpuView === 'detail';
    renderPcpuLegend(fleet, colorMap);
    const chassisBlocks = groupFleetByChassis(filtered);
    els.pcpu.classList.toggle('ui-pcpu-grid--detail', detail);
    els.pcpu.classList.toggle('ui-pcpu-grid--compact', !detail);
    if (!filtered.length) {
      els.pcpu.innerHTML = `<p class="ui-hint">No ports match owner filter${pcpuOwnerFilter ? ` (“${esc(pcpuOwnerFilter)}”)` : ''}. <button type="button" class="ui-pcpu-clear-filter" id="ui-pcpu-clear-filter">Show all</button></p>`;
      const clearBtn = document.getElementById('ui-pcpu-clear-filter');
      clearBtn?.addEventListener('click', () => {
        pcpuOwnerFilter = '';
        sessionStorage.setItem(PCPU_OWNER_FILTER_KEY, '');
        if (els.pcpuOwnerFilter) els.pcpuOwnerFilter.value = '';
        if (lastData) renderPcpu(lastData.pcpu_fleet || []);
      });
      return;
    }
    els.pcpu.innerHTML = chassisBlocks.map((ch) => {
      if (!detail) return renderPcpuChassisCompact(ch, colorMap);
      const ip = ch.chassis_ip ? `<span class="ui-ip">${esc(ch.chassis_ip)}</span>` : '';
      const versionHtml = renderPcpuVersionChips(chassisVersionPayload(ch));
      const rgHtml = ch.groups.map((rg) => renderPcpuRgGroup(rg, colorMap)).join('');
      return `<section class="ui-pcpu-chassis">
        <header class="ui-pcpu-chassis-head">
          <div class="ui-pcpu-chassis-head-main">
            <h3 class="ui-pcpu-chassis-title">${esc(ch.chassis)} ${ip}</h3>
            ${versionHtml}
          </div>
          <span class="ui-pcpu-chassis-meta">${ch.groups.length} RG · ${ch.groups.reduce((n, g) => n + (g.port_count || 0), 0)} ports</span>
        </header>
        <div class="ui-pcpu-rg-row">${rgHtml}</div>
      </section>`;
    }).join('');
    if (!detail) requestAnimationFrame(() => layoutPcpuCompactGrid());
  }

  function rankIfaceIp(it) {
    return String(it.iface_ip || it.pcpu_ip || it.chassis_ip || '').trim();
  }

  function renderRankRow(it, i, fmt, { traffic = false } = {}) {
    const ifaceIp = rankIfaceIp(it);
    const ipHtml = (traffic || ifaceIp)
      ? `<span class="ui-ip">${esc(ifaceIp || '—')}</span>`
      : '';
    const own = it.owner && it.owner !== 'Free'
      ? `<span class="ui-owner" title="${esc(it.owner)}">${esc(it.owner)}</span>`
      : '';
    return `<li>
      <span class="ui-rank-idx">${i + 1}</span>
      <span class="ui-rank-main" title="${esc([it.label, ifaceIp, it.owner !== 'Free' ? it.owner : ''].filter(Boolean).join(' · '))}">
        <span class="ui-rank-lbl">${esc(it.label)}</span>${ipHtml}${own}
      </span>
      <span class="ui-rank-val">${esc(fmt(it.value))}</span>
    </li>`;
  }

  function renderRankings(rankings) {
    const blocks = [
      { key: 'cpu_pct', title: 'CPU % (peak)', fmt: (v) => `${v}%` },
      { key: 'mem_pct', title: 'Memory % (peak)', fmt: (v) => `${v}%` },
      { key: 'bps_total', title: 'Traffic (peak bps)', fmt: fmtBps, traffic: true },
    ];
    els.rankings.innerHTML = blocks.map(({ key, title, fmt, traffic = false }) => {
      const items = rankings[key] || [];
      return `<article class="ui-card ui-rank-card"><h2>${esc(title)}</h2>
        <ol class="ui-rank-list">${items.length ? items.map((it, i) => renderRankRow(it, i, fmt, { traffic })).join('') : '<li><span class="ui-rank-idx">—</span><span class="ui-rank-main">No data</span><span></span></li>'}
        </ol></article>`;
    }).join('');
  }

  function renderWaste(signals, summary) {
    if (!signals.length) {
      els.waste.innerHTML = `<p class="ui-hint">No owned-idle ports detected in this window — good sign, or collector has not run long enough.</p>`;
      return;
    }
    els.waste.innerHTML = `
      <p class="ui-hint"><strong>${summary.waste_owned_idle ?? signals.length}</strong> ports reserved with negligible traffic.</p>
      ${signals.slice(0, 40).map((w) => {
        const ip = w.chassis_ip ? ` <span class="ui-ip">${esc(w.chassis_ip)}</span>` : '';
        const own = w.owner ? ` <span class="ui-owner">${esc(w.owner)}</span>` : '';
        return `<div class="ui-waste-row">
          <div><strong>${esc(w.label)}</strong>${ip}${own}<br><code>${esc(w.resource_key)}</code><br>${esc(w.detail)}</div>
          <div>CPU ${w.cpu_pct ?? '—'}% · MEM ${w.mem_pct ?? '—'}%</div>
        </div>`;
      }).join('')}`;
  }

  function renderActivity(act) {
    const types = act.by_type || {};
    const entries = Object.entries(types);
    const max = Math.max(1, ...entries.map(([, v]) => v));
    els.activity.innerHTML = entries.length ? `
      <p class="ui-hint">${act.total_events ?? 0} resource events in window</p>
      ${entries.map(([t, n]) => `
        <div class="ui-act-bar"><span>${esc(t)}</span>
          <div class="ui-bar"><i style="width:${Math.round(100 * n / max)}%"></i></div>
          <span>${n}</span></div>`).join('')}
    ` : '<p class="ui-hint">No events recorded in this window.</p>';
  }

  function drawHeatMatrix(rows) {
    const canvas = els.heatCanvas;
    if (!canvas || !rows.length) return;
    const cols = [
      { key: 'cpu_pct', label: 'CPU max' },
      { key: 'mem_pct', label: 'MEM max' },
      { key: 'owned_pct', label: 'Owned %' },
      { key: 'traffic_pct', label: 'Traffic %' },
    ];
    const rowH = 22;
    const colW = 72;
    const labelW = 210;
    const pad = 8;
    const w = labelW + cols.length * colW + pad * 2;
    const h = pad * 2 + 24 + rows.length * rowH;
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#0a1628';
    ctx.fillRect(0, 0, w, h);
    ctx.font = '11px Segoe UI, system-ui, sans-serif';
    ctx.fillStyle = '#64748b';
    cols.forEach((c, i) => {
      ctx.fillText(c.label, labelW + i * colW + 4, pad + 12);
    });
    rows.forEach((row, ri) => {
      const y = pad + 24 + ri * rowH;
      ctx.fillStyle = '#94a3b8';
      const lbl = row.mgmt_ip ? `${String(row.label).slice(0, 16)} · ${row.mgmt_ip}` : String(row.label).slice(0, 18);
      ctx.fillText(lbl.slice(0, 30), 4, y + 14);
      cols.forEach((c, ci) => {
        const v = row[c.key];
        const x = labelW + ci * colW;
        ctx.fillStyle = heatColor(v);
        ctx.fillRect(x + 2, y + 2, colW - 4, rowH - 4);
        ctx.fillStyle = v != null && v > 40 ? '#fff' : '#cbd5e1';
        ctx.fillText(v != null ? String(v) : '—', x + 8, y + 14);
      });
    });
    els.heatLegend.innerHTML = `
      <span><i class="ui-heat-swatch" style="background:#22c55e"></i> low</span>
      <span><i class="ui-heat-swatch" style="background:#3b82f6"></i> moderate</span>
      <span><i class="ui-heat-swatch" style="background:#f59e0b"></i> elevated</span>
      <span><i class="ui-heat-swatch" style="background:#ef4444"></i> hot</span>`;
  }

  els.refresh.addEventListener('click', () => load({ force: true }));
  els.window.addEventListener('change', () => load({ force: true }));
  load();
  setInterval(() => {
    if (!document.hidden) load({ force: true });
  }, 30000);
}
