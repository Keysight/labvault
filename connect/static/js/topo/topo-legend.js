/**
 * TopoLegend — floating bottom-left legend panel.
 */
import { TOPO_THEME } from './topo-theme.js';

export class TopoLegend {
  constructor({ containerId } = {}) {
    this._el = document.getElementById(containerId);
    if (this._el) this._render();
  }

  _render() {
    const items = [
      { color: TOPO_THEME.link_up,      label: 'Live (LLDP ≤15m)' },
      { color: TOPO_THEME.link_stale,   label: 'Stale (24h cache)' },
      { color: TOPO_THEME.link_down,    label: 'Down / no LLDP' },
      { color: TOPO_THEME.link_planned, label: 'Planned only',  dashed: true },
      { color: TOPO_THEME.link_ocs,     label: 'OCS active' },
      { color: TOPO_THEME.link_conflict,label: 'Port conflict' },
    ];
    this._el.innerHTML = `<div style="background:#0d1b2a;border:1px solid #1e3a5f;border-radius:5px;padding:6px 10px;font-size:.7rem;color:#94a3b8;">
      ${items.map(i => `<div class="d-flex align-items-center mb-1">
        <span style="display:inline-block;width:22px;height:3px;background:${i.color};
          ${i.dashed ? 'border-top:2px dashed '+i.color+';background:transparent;' : ''}
          margin-right:6px;border-radius:2px;"></span>${i.label}</div>`).join('')}
    </div>`;
  }

  toggle(visible) {
    if (this._el) this._el.style.display = visible ? '' : 'none';
  }
}
