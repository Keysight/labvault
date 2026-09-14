/**
 * Lab Pulse — experimental usage insights dashboards.
 * Consumes usage-insights.json; does not touch the main Usage graph/timeline.
 */

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

export function initUsageInsights({ topoId, apiUrl }) {
  const els = {
    window: document.getElementById('ui-window'),
    refresh: document.getElementById('ui-refresh'),
    loading: document.getElementById('ui-loading'),
    error: document.getElementById('ui-error'),
    kpis: document.getElementById('ui-kpis'),
    research: document.getElementById('ui-research-list'),
    devices: document.getElementById('ui-device-snapshot'),
    pcpu: document.getElementById('ui-pcpu-grid'),
    rankings: document.getElementById('ui-rankings'),
    waste: document.getElementById('ui-waste-list'),
    activity: document.getElementById('ui-activity'),
    heatCanvas: document.getElementById('ui-heat-canvas'),
    heatLegend: document.getElementById('ui-heat-legend'),
    tabs: document.querySelectorAll('.ui-view-tab'),
    panels: document.querySelectorAll('.ui-panel'),
  };

  let lastData = null;

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
    });
  });

  async function load() {
    els.error.hidden = true;
    els.loading.hidden = false;
    try {
      const w = els.window.value || '24h';
      const res = await fetch(`${apiUrl}?window=${encodeURIComponent(w)}`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      lastData = await res.json();
      renderAll(lastData);
    } catch (e) {
      els.error.textContent = `Failed to load insights: ${e.message}`;
      els.error.hidden = false;
    } finally {
      els.loading.hidden = true;
    }
  }

  function renderAll(data) {
    renderKpis(data.summary || {});
    renderResearch(data.research_notes || []);
    renderDevices(data.devices || []);
    renderPcpu(data.pcpu_fleet || []);
    renderRankings(data.rankings || {});
    renderWaste(data.waste_signals || [], data.summary || {});
    renderActivity(data.activity || {});
    const active = document.querySelector('.ui-panel.on');
    if (active?.dataset.panel === 'heatmap') drawHeatMatrix(data.heat_matrix || []);
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

  function renderDevices(devices) {
    const rows = devices
      .filter((d) => d.ports?.total > 0 || d.metrics?.cpu_pct?.latest != null)
      .slice(0, 12);
    els.devices.innerHTML = rows.length ? rows.map((d) => {
      const cpu = d.metrics?.cpu_pct?.latest;
      const mem = d.metrics?.mem_pct?.latest;
      const owned = d.ports?.total ? Math.round(100 * d.ports.owned / d.ports.total) : 0;
      return `<div class="ui-device-row">
        <span>${esc(d.label)}</span>
        <span title="CPU">${cpu != null ? cpu + '%' : '—'} CPU</span>
        <span title="Owned ports">${owned}% own</span>
        <div class="ui-bar" title="Memory"><i style="width:${mem ?? 0}%;background:${pctColor(mem, 80)}"></i></div>
      </div>`;
    }).join('') : '<p class="ui-hint">No device metrics in this window yet. Run the metrics collector.</p>';
  }

  function renderPcpu(fleet) {
    if (!fleet.length) {
      els.pcpu.innerHTML = '<p class="ui-hint">No PCPU samples yet. AresONE root SSH collection populates port CPU/mem from 10.0.x.x hosts.</p>';
      return;
    }
    els.pcpu.innerHTML = fleet.map((p) => {
      const hot = (p.cpu_pct || 0) >= 75 || (p.mem_pct || 0) >= 80;
      return `<div class="ui-pcpu-tile${hot ? ' hot' : ''}">
        <div class="ui-pcpu-chassis">${esc(p.chassis)} · ${p.port_count} ports</div>
        <div class="ui-pcpu-meters">
          <div class="ui-meter"><label>CPU ${p.cpu_pct ?? '—'}%</label>
            <div class="ui-meter-track"><div class="ui-meter-fill" style="width:${p.cpu_pct || 0}%;background:${pctColor(p.cpu_pct)}"></div></div></div>
          <div class="ui-meter"><label>MEM ${p.mem_pct ?? '—'}%</label>
            <div class="ui-meter-track"><div class="ui-meter-fill" style="width:${p.mem_pct || 0}%;background:${pctColor(p.mem_pct, 80)}"></div></div></div>
        </div>
        <div class="ui-pcpu-ports">${esc((p.sample_ports || []).join(', '))}</div>
      </div>`;
    }).join('');
  }

  function renderRankings(rankings) {
    const blocks = [
      { key: 'cpu_pct', title: 'CPU % (peak)', fmt: (v) => `${v}%` },
      { key: 'mem_pct', title: 'Memory % (peak)', fmt: (v) => `${v}%` },
      { key: 'bps_total', title: 'Traffic (peak bps)', fmt: fmtBps },
    ];
    els.rankings.innerHTML = blocks.map(({ key, title, fmt }) => {
      const items = rankings[key] || [];
      return `<article class="ui-card"><h2>${esc(title)}</h2>
        <ol class="ui-rank-list">${items.length ? items.map((it, i) => `
          <li><span class="ui-rank-idx">${i + 1}</span><span>${esc(it.label)}</span><span class="ui-rank-val">${esc(fmt(it.value))}</span></li>
        `).join('') : '<li><span class="ui-rank-idx">—</span><span>No data</span><span></span></li>'}
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
      ${signals.slice(0, 40).map((w) => `
        <div class="ui-waste-row">
          <div><strong>${esc(w.label)}</strong><br><code>${esc(w.resource_key)}</code><br>${esc(w.detail)}</div>
          <div>CPU ${w.cpu_pct ?? '—'}% · MEM ${w.mem_pct ?? '—'}%</div>
        </div>`).join('')}`;
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
    const labelW = 140;
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
      ctx.fillText(String(row.label).slice(0, 18), 4, y + 14);
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

  els.refresh.addEventListener('click', load);
  els.window.addEventListener('change', load);
  load();
}
