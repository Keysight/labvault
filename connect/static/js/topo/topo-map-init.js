/**
 * Bootstrap Checkmk-style controls for /topology/ map view.
 */
import { TOPO_THEME, linkColorForHealth } from './topo-theme.js';
import { TopoToolbar } from './topo-toolbar.js';
import { TopoMapLegend } from './topo-legend.js';
import { TopoInspector } from './topo-inspector.js';
import { TopoMapGraphClient } from './topo-map-client.js';

const PREFS_KEY = 'labvault_topology_display_v1';

const defaultPrefs = () => ({
  hostLabels: true,
  ifaceLabels: false,
  showLegend: true,
  layerLldp: true,
  layerChassis: true,
  layerExternal: false,
  layerPlanned: false,
});

export function readDisplayPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return defaultPrefs();
    return { ...defaultPrefs(), ...JSON.parse(raw) };
  } catch {
    return defaultPrefs();
  }
}

export function writeDisplayPrefs(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch { /* ignore */ }
}

export async function initTopoMapChrome({ dataUrl, pollIntervalMs = 45000 }) {
  const prefs = readDisplayPrefs();
  const toolbar = new TopoToolbar({ statusBarId: 'topo-status-bar' });
  const legend = new TopoMapLegend({ containerId: 'topo-legend-panel' });
  const inspector = new TopoInspector({ containerId: 'topo-inspector-panel' });

  legend.renderForMap(prefs);
  legend.toggle(prefs.showLegend);

  toolbar.setLoading(true);

  const client = new TopoMapGraphClient({
    dataUrl,
    pollIntervalMs,
    onUpdate: (data) => {
      toolbar.setLoading(false);
      const conflicts = (data.conflicts || []).length;
      if (data.meta) toolbar.update(data.meta, conflicts);
      if (window.labvaultTopoMap && window.labvaultTopoMap.onGraphData) {
        window.labvaultTopoMap.onGraphData(data);
      }
    },
    onError: (err) => {
      toolbar.setLoading(false);
      toolbar.setError(err.message || String(err));
      if (window.labvaultTopoMap && window.labvaultTopoMap.onGraphData) {
        window.labvaultTopoMap.onGraphData({ nodes: [], links: [], errors: [String(err)] });
      }
    },
  });

  function bindToggle(id, key) {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = !!prefs[key];
    el.addEventListener('change', () => {
      prefs[key] = el.checked;
      writeDisplayPrefs(prefs);
      legend.renderForMap(prefs);
      if (key === 'showLegend') legend.toggle(prefs.showLegend);
      if (window.labvaultTopoMap && window.labvaultTopoMap.onDisplayPrefsChange) {
        window.labvaultTopoMap.onDisplayPrefsChange(prefs);
      }
    });
  }

  bindToggle('topo-pref-host-labels', 'hostLabels');
  bindToggle('topo-pref-iface-labels', 'ifaceLabels');
  bindToggle('topo-pref-legend', 'showLegend');
  bindToggle('topo-pref-layer-lldp', 'layerLldp');
  bindToggle('topo-pref-layer-chassis', 'layerChassis');
  bindToggle('topo-pref-layer-external', 'layerExternal');

  const api = {
    prefs,
    toolbar,
    legend,
    inspector,
    client,
    TOPO_THEME,
    linkColorForHealth,
    readDisplayPrefs,
    writeDisplayPrefs,
    currentZoom: () => 1,
    setCurrentZoom: (z) => { api._zoom = z; },
    labelsVisible: () => {
      const p = readDisplayPrefs();
      const z = api._zoom || 1;
      return { host: p.hostLabels && z >= 0.35, iface: p.ifaceLabels && z >= 0.55 };
    },
    layerVisible: (layer) => {
      const p = readDisplayPrefs();
      if (layer === 'external') return p.layerExternal;
      if (layer === 'chassis') return p.layerChassis;
      if (layer === 'lldp') return p.layerLldp;
      if (layer === 'planned') return p.layerPlanned;
      return true;
    },
    showNodeInspector: (node) => inspector.renderMapNode(node),
    showLinkInspector: (link) => inspector.renderMapLink(link),
    clearInspector: () => inspector.clear(),
  };

  window.labvaultTopoMap = api;
  client.startPolling();
  return api;
}
