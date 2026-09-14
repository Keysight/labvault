/**
 * TopoInspector — right-panel detail renderer for nodes and links.
 * @see docs/TOPOLOGY_VIEWS_IMPLEMENTATION_PLAN.md §4.5
 */
import { TOPO_THEME } from './topo-theme.js';

export class TopoInspector {
  constructor({ containerId, onAction } = {}) {
    this._el = document.getElementById(containerId);
    this._onAction = onAction;
    this._current = null;
  }

  renderNode(node) {
    if (!this._el) return;
    this._current = { type: 'node', data: node };
    const resv = node.reservation;
    const badges = (node.badges || []).map(b =>
      `<span class="badge bg-secondary me-1" style="font-size:.65rem">${b}</span>`).join('');
    const ports = (node.ports || []).slice(0, 12).map(p => {
      const name = typeof p === 'string' ? p : (p.name || '');
      const status = typeof p === 'object' ? (p.status || '') : '';
      const dot = status === 'up' ? TOPO_THEME.link_up : status === 'down' ? TOPO_THEME.link_down : '#64748b';
      return `<div class="d-flex justify-content-between" style="font-size:.7rem;padding:2px 0;border-bottom:1px solid #1e3a5f22">
        <span>${name}</span><span style="color:${dot}">●</span></div>`;
    }).join('');
    this._el.innerHTML = `
      <div class="fm-panel">
        <h6><i class="fas fa-${TOPO_THEME.icons[node.kind] || 'cube'} me-1"></i>${node.label || node.id}</h6>
        <div class="fm-detail-row"><span>Kind</span><span>${node.kind || '—'}</span></div>
        <div class="fm-detail-row"><span>Mgmt</span><span>${node.mgmt_display || node.mgmt_ipv4 || '—'}</span></div>
        <div class="fm-detail-row"><span>IPv4</span><span>${node.mgmt_ipv4 || '—'}</span></div>
        <div class="fm-detail-row"><span>IPv6</span><span>${node.mgmt_ipv6 || '—'}</span></div>
        <div class="fm-detail-row"><span>Vendor</span><span>${node.vendor || '—'}</span></div>
        <div class="fm-detail-row"><span>Model</span><span>${node.model || '—'}</span></div>
        <div class="fm-detail-row"><span>Status</span>
          <span style="color:${node.status==='online'?TOPO_THEME.status_online:TOPO_THEME.status_unknown}">${node.status || '—'}</span></div>
        ${resv ? `<div class="fm-detail-row" style="color:#f59e0b"><span>Reserved</span><span>${resv.team || ''} · ${resv.user || ''}</span></div>` : ''}
        ${badges ? `<div class="mt-2">${badges}</div>` : ''}
        ${ports ? `<h6 class="mt-2">PORTS (${(node.ports||[]).length})</h6>${ports}` : ''}
        ${node.detail_url ? `<a href="${node.detail_url}" class="btn btn-sm btn-outline-secondary mt-2" style="font-size:.72rem">
          <i class="fas fa-external-link-alt me-1"></i>Device page</a>` : ''}
      </div>`;
    this._el.classList.add('visible');
  }

  renderLink(link) {
    if (!this._el) return;
    this._current = { type: 'link', data: link };
    const healthColor = {up:'#22c55e',stale:'#f59e0b',down:'#ef4444',planned:'#64748b',conflict:'#ef4444',active:'#22c55e'}[link.health] || '#94a3b8';
    const prov = (link.provenance || []).join(', ');
    const ocs = (link.ocs_triplets || []).join(', ');
    this._el.innerHTML = `
      <div class="fm-panel">
        <h6><i class="fas fa-project-diagram me-1"></i>Link</h6>
        <div class="fm-detail-row"><span>Source</span><span>${link.source}</span></div>
        <div class="fm-detail-row"><span>Port A</span><span>${link.port_a || '—'}</span></div>
        <div class="fm-detail-row"><span>Target</span><span>${link.target}</span></div>
        <div class="fm-detail-row"><span>Port B</span><span>${link.port_b || '—'}</span></div>
        <div class="fm-detail-row"><span>Health</span>
          <span style="color:${healthColor}">● ${link.health || '—'}</span></div>
        <div class="fm-detail-row"><span>Type</span><span>${link.type || '—'}</span></div>
        <div class="fm-detail-row"><span>Cable</span><span>${link.cable_type || '—'}</span></div>
        ${prov ? `<div class="fm-detail-row"><span>Sources</span><span style="font-size:.68rem">${prov}</span></div>` : ''}
        ${ocs ? `<div class="fm-detail-row"><span>OCS</span><span style="font-size:.68rem">${ocs}</span></div>` : ''}
        ${link.loss_db != null ? `<div class="fm-detail-row"><span>Loss</span><span>${link.loss_db} dB</span></div>` : ''}
      </div>`;
    this._el.classList.add('visible');
  }

  clear() {
    if (!this._el) return;
    this._el.innerHTML = '';
    this._el.classList.remove('visible');
    this._current = null;
  }

  getCurrent() { return this._current; }
}
