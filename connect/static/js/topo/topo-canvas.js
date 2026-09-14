/**
 * TopoCanvas — pan/zoom, minimap, node selection wrapper (Phase 3+).
 * Wraps existing D3 zoom behaviour and emits events.
 * @see docs/TOPOLOGY_VIEWS_IMPLEMENTATION_PLAN.md §6.3
 */
export class TopoCanvas {
  constructor({ svgId, theme } = {}) {
    this._svgEl = document.getElementById(svgId);
    this._theme = theme || {};
    this._selected = { node: null, link: null };
    this._callbacks = {};
  }

  on(event, cb) {
    this._callbacks[event] = cb;
    return this;
  }

  _emit(event, data) {
    if (this._callbacks[event]) this._callbacks[event](data);
  }

  selectNode(nodeId) {
    this._selected.node = nodeId;
    this._selected.link = null;
    this._emit('nodeSelected', nodeId);
  }

  selectLink(linkId) {
    this._selected.link = linkId;
    this._selected.node = null;
    this._emit('linkSelected', linkId);
  }

  clearSelection() {
    this._selected = { node: null, link: null };
    this._emit('canvasClick', null);
  }

  filterByText(query) {
    if (!this._svgEl) return;
    const q = (query || '').toLowerCase();
    this._svgEl.querySelectorAll('.fm-node, .pf-dev').forEach(el => {
      const label = (el.dataset.label || el.textContent || '').toLowerCase();
      el.style.opacity = (!q || label.includes(q)) ? '1' : '0.2';
    });
  }

  fitToView() {
    // Delegate to existing fm-fit / pf-fit buttons if present
    const btn = document.getElementById('fm-fit') || document.getElementById('pf-fit');
    if (btn) btn.click();
  }

  getPositions() {
    const out = [];
    if (!this._svgEl) return out;
    this._svgEl.querySelectorAll('[data-node-pk]').forEach(el => {
      const t = el.getAttribute('transform') || '';
      const m = t.match(/translate\(([^,]+),([^)]+)\)/);
      if (m) out.push({ id: el.dataset.nodePk, x: parseFloat(m[1]), y: parseFloat(m[2]) });
    });
    return out;
  }

  toggleFullscreen() {
    const wrap = document.getElementById('fm-wrap') || document.getElementById('pf-wrap');
    if (!wrap) return;
    if (!document.fullscreenElement) {
      wrap.requestFullscreen?.();
    } else {
      document.exitFullscreen?.();
    }
  }
}
