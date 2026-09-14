/**
 * Fetch / poll NormalizedGraph v1 from LabVault.
 * Legacy fabric.json remains default until LABVAULT_GRAPH_FABRIC=1.
 */
import { linkColorForHealth } from './topo-theme.js';

export class GraphClient {
  constructor({ topoId, baseUrl = '', pollIntervalMs = 30000, onUpdate, onError } = {}) {
    this.topoId = topoId;
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.pollIntervalMs = pollIntervalMs;
    this.onUpdate = onUpdate;
    this.onError = onError;
    this._timer = null;
    this._lastGraph = null;
    this._abort = null;
  }

  graphUrl(params = {}) {
    const q = new URLSearchParams({
      live: params.live ?? '1',
      lldp: params.lldp ?? '1',
      ocs: params.ocs ?? '1',
      planned: params.planned ?? '1',
      reservations: params.reservations ?? '1',
    });
    return `${this.baseUrl}/lab-topology/${this.topoId}/graph.json?${q}`;
  }

  compareUrl(params = {}) {
    const q = new URLSearchParams({ live: params.live ?? '1', lldp: params.lldp ?? '1' });
    return `${this.baseUrl}/lab-topology/${this.topoId}/graph/compare.json?${q}`;
  }

  async fetch(opts = {}) {
    if (this._abort) this._abort.abort();
    this._abort = new AbortController();
    const res = await fetch(this.graphUrl(opts), {
      credentials: 'same-origin',
      signal: this._abort.signal,
    });
    if (!res.ok) throw new Error(`graph.json HTTP ${res.status}`);
    const graph = await res.json();
    this._lastGraph = graph;
    if (this.onUpdate) this.onUpdate(graph);
    return graph;
  }

  async compareWithLegacy(opts = {}) {
    const res = await fetch(this.compareUrl(opts), { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`compare.json HTTP ${res.status}`);
    return res.json();
  }

  async refresh(sources = ['lldp']) {
    const res = await fetch(`${this.baseUrl}/lab-topology/${this.topoId}/graph/refresh/`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sources }),
    });
    if (!res.ok) throw new Error(`graph/refresh HTTP ${res.status}`);
    return res.json();
  }

  startPolling(opts = {}) {
    this.stopPolling();
    const tick = async () => {
      if (document.visibilityState !== 'visible') return;
      try {
        await this.fetch(opts);
      } catch (e) {
        if (this.onError) this.onError(e);
      }
    };
    const jitter = Math.floor(Math.random() * 5000);
    this._timer = setInterval(tick, this.pollIntervalMs + jitter);
    tick();
  }

  stopPolling() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  getLastGraph() {
    return this._lastGraph;
  }

  getSourceFreshness() {
    return (this._lastGraph && this._lastGraph.meta && this._lastGraph.meta.sources) || {};
  }
}

/** Apply theme colors to graph links in-place (for canvas renderers). */
export function applyThemeToGraph(graph) {
  if (!graph || !graph.links) return graph;
  for (const lk of graph.links) {
    lk.color = lk.color || linkColorForHealth(lk.health, lk.type);
  }
  return graph;
}
