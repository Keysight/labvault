/**
 * TopoToolbar — status bar + source freshness LEDs.
 * @see docs/TOPOLOGY_VIEWS_IMPLEMENTATION_PLAN.md §7.1
 */
export class TopoToolbar {
  constructor({ statusBarId } = {}) {
    this._el = document.getElementById(statusBarId);
  }

  /**
   * @param {object} meta - graph.meta from NormalizedGraph
   * @param {number} conflictCount
   */
  update(meta, conflictCount = 0) {
    if (!this._el || !meta) return;
    const now = Date.now();
    const genAt = meta.generated_at ? new Date(meta.generated_at) : null;
    const ageSec = genAt ? Math.round((now - genAt) / 1000) : null;
    const ageStr = ageSec == null ? '' : ageSec < 60 ? `${ageSec}s ago` : `${Math.floor(ageSec/60)}m ago`;

    const sources = meta.sources || {};
    const leds = [
      { key: 'lldp_db',      label: 'LLDP' },
      { key: 'ocs_live',     label: 'OCS'  },
      { key: 'planned',      label: 'Plan' },
      { key: 'chassis_links',label: 'Chs'  },
    ].map(({ key, label }) => {
      const s = sources[key] || {};
      const color = s.ok === false ? '#ef4444' : s.ok === true ? '#22c55e' : '#64748b';
      const tip = s.error ? s.error : (s.ok ? 'OK' : 'disabled');
      return `<span title="${label}: ${tip}" style="display:inline-flex;align-items:center;gap:3px;margin-right:8px;font-size:.7rem;cursor:help;">
        <span style="width:7px;height:7px;border-radius:50%;background:${color};display:inline-block;"></span>${label}</span>`;
    }).join('');

    const conflictHtml = conflictCount > 0
      ? `<span style="color:#ef4444;margin-left:8px;">⚠ ${conflictCount} conflict${conflictCount>1?'s':''}</span>`
      : `<span style="color:#22c55e;margin-left:8px;">✓ no conflicts</span>`;

    this._el.innerHTML = `<span style="color:#94a3b8;font-size:.72rem;">
      ${ageStr ? `Last refresh: <strong>${ageStr}</strong>  ·  ` : ''}
      ${leds} ${conflictHtml}
      ${meta.cache_hit ? '<span style="color:#64748b;font-size:.68rem;margin-left:6px;">(cached)</span>' : ''}
    </span>`;
  }

  setLoading(loading) {
    if (!this._el) return;
    if (loading) this._el.innerHTML = `<span style="color:#64748b;font-size:.72rem;">
      <i class="fas fa-spinner fa-spin me-1"></i>Loading…</span>`;
  }

  setError(msg) {
    if (!this._el) return;
    this._el.innerHTML = `<span style="color:#ef4444;font-size:.72rem;">✗ ${msg}</span>`;
  }
}
