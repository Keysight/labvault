/**
 * Shared shell for graph view pages — fetch, data mode, refresh.
 */

import { mountDataModeToggle, getStoredDataMode } from './usage-data-mode.js';
import {
  fetchGraphData,
  applyDataModeToInsights,
  applyDataModeToTimeline,
} from './usage-graph-data.js';

export function initGraphViewShell({
  topoId,
  insightsUrl,
  timelineUrl,
  fabricUrl,
  viewKey,
  initView,
}) {
  const els = {
    window: document.getElementById('ugv-window'),
    refresh: document.getElementById('ugv-refresh'),
    dataMode: document.getElementById('ugv-data-mode'),
    loading: document.getElementById('ugv-loading'),
    error: document.getElementById('ugv-error'),
    root: document.getElementById('ugv-viz-root'),
    detail: document.getElementById('ugv-detail'),
  };

  let dataMode = getStoredDataMode();
  let lastRaw = null;
  let viewApi = null;

  const modeToggle = mountDataModeToggle(els.dataMode, {
    initial: dataMode,
    onChange: (m) => {
      dataMode = m;
      if (lastRaw) renderView(lastRaw);
    },
  });

  async function load() {
    els.error.hidden = true;
    els.loading.hidden = false;
    try {
      const preset = els.window?.value || '24h';
      lastRaw = await fetchGraphData({
        insightsUrl,
        timelineUrl,
        fabricUrl,
        windowPreset: preset,
      });
      renderView(lastRaw);
    } catch (e) {
      els.error.textContent = `Failed to load: ${e.message}`;
      els.error.hidden = false;
    } finally {
      els.loading.hidden = true;
    }
  }

  function renderView(raw) {
    const insights = applyDataModeToInsights(raw.insights, dataMode, raw.windowPreset);
    const timeline = applyDataModeToTimeline(raw.timeline, dataMode, raw.windowPreset);
    const payload = {
      topoId,
      viewKey,
      insights,
      timeline,
      fabric: raw.fabric,
      dataMode,
      windowPreset: raw.windowPreset,
      detailEl: els.detail,
      rootEl: els.root,
    };
    if (!viewApi) {
      viewApi = initView(payload);
    }
    if (viewApi?.render) {
      viewApi.render(payload);
    }
  }

  els.refresh?.addEventListener('click', load);
  els.window?.addEventListener('change', load);
  window.addEventListener('topo-theme-change', () => {
    if (lastRaw) renderView(lastRaw);
  });
  load();

  return { reload: load, getDataMode: () => modeToggle?.getMode() || dataMode };
}
