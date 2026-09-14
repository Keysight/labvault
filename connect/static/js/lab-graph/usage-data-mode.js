/**
 * Shared data mode toggle: instantaneous | timeseries | timeseries_avg
 * Used by Lab Pulse, Usage timeline, and graph views.
 */

export const DATA_MODES = [
  { id: 'instant', label: 'Instant', title: 'Live ports from the latest collector poll' },
  { id: 'timeseries', label: 'Series', title: 'Bucketed time-series' },
  { id: 'timeseries_avg', label: 'Avg', title: 'Coarser buckets (client-side average)' },
];

const STORAGE_KEY = 'lv_usage_data_mode';

export function getStoredDataMode() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (DATA_MODES.some((m) => m.id === v)) return v;
  } catch (_) { /* ignore */ }
  return 'timeseries';
}

export function setStoredDataMode(mode) {
  try { localStorage.setItem(STORAGE_KEY, mode); } catch (_) { /* ignore */ }
}

export function mountDataModeToggle(container, { initial, onChange }) {
  if (!container) return null;
  let current = initial || getStoredDataMode();
  container.innerHTML = DATA_MODES.map((m) => (
    `<button type="button" class="ugv-mode-btn${m.id === current ? ' on' : ''}" data-mode="${m.id}" title="${m.title}">${m.label}</button>`
  )).join('');
  container.querySelectorAll('.ugv-mode-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      current = btn.dataset.mode;
      container.querySelectorAll('.ugv-mode-btn').forEach((b) => b.classList.toggle('on', b === btn));
      setStoredDataMode(current);
      onChange?.(current);
    });
  });
  return {
    getMode: () => current,
    setMode: (mode) => {
      if (!DATA_MODES.some((m) => m.id === mode)) return;
      current = mode;
      container.querySelectorAll('.ugv-mode-btn').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
      setStoredDataMode(mode);
    },
  };
}

export function parseTs(ts) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? null : d.getTime();
}

/** Transform [[iso, val], ...] according to data mode. */
export function transformSeries(points, mode, { mergeFactor = 3 } = {}) {
  const raw = (points || [])
    .map((p) => ({ ts: parseTs(p[0]), v: Number(p[1]) }))
    .filter((p) => p.ts != null && !Number.isNaN(p.v));
  if (!raw.length) return [];

  if (mode === 'instant') {
    const last = raw[raw.length - 1];
    return [[new Date(last.ts).toISOString(), last.v]];
  }
  if (mode === 'timeseries') {
    return raw.map((p) => [new Date(p.ts).toISOString(), p.v]);
  }
  // timeseries_avg — merge adjacent buckets
  const out = [];
  for (let i = 0; i < raw.length; i += mergeFactor) {
    const chunk = raw.slice(i, i + mergeFactor);
    const avg = chunk.reduce((s, p) => s + p.v, 0) / chunk.length;
    const mid = chunk[Math.floor(chunk.length / 2)];
    out.push([new Date(mid.ts).toISOString(), avg]);
  }
  return out;
}

export function transformFleetSeries(fleet, mode) {
  if (!fleet) return {};
  const keys = ['cpu_pct', 'mem_pct', 'ports_owned_pct', 'bps_total'];
  const out = {};
  keys.forEach((k) => { out[k] = transformSeries(fleet[k], mode); });
  return out;
}

export function latestValue(points) {
  const arr = transformSeries(points, 'instant');
  return arr.length ? arr[0][1] : null;
}

export function mergeFactorForWindow(windowPreset) {
  if (windowPreset === '7d') return 6;
  if (windowPreset === '24h') return 3;
  return 2;
}
