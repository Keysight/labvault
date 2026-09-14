/**
 * Shared Tier / Rack (DC) / Free layout for topology views.
 * sizeOf(d) -> { w, h } in canvas center coordinates.
 */
(function (global) {
  'use strict';

  const DEFAULT_TIER_Y = { chassis: 180, ocs: 480, switch: 780, server: 1080 };

  function tierOf(d) {
    const t = d.node_type || d.type || 'generic';
    if (t === 'chassis') return 'chassis';
    if (t === 'ocs') return 'ocs';
    if (t === 'switch') return 'switch';
    return 'server';
  }

  function computeTierLayout(devices, opts) {
    opts = opts || {};
    const tierY = opts.tierY || DEFAULT_TIER_Y;
    const sizeOf = opts.sizeOf || (() => ({ w: 120, h: 52 }));
    const spacing = opts.spacing != null ? opts.spacing : 40;
    const tiers = {};
    (devices || []).forEach((d) => {
      const t = tierOf(d);
      (tiers[t] = tiers[t] || []).push(d);
    });
    const pos = {};
    Object.entries(tiers).forEach(([tier, devs]) => {
      const y = tierY[tier] || 700;
      const totalW = devs.reduce((s, d) => s + sizeOf(d).w + spacing, 0) - spacing;
      let x = -totalW / 2;
      devs.forEach((d) => {
        const { w } = sizeOf(d);
        pos[d.id] = { x: x + w / 2, y };
        x += w + spacing;
      });
    });
    return pos;
  }

  function computeDCLayout(devices, connections, opts) {
    opts = opts || {};
    const sizeOf = opts.sizeOf || (() => ({ w: 120, h: 52 }));
    const GAP = opts.gap != null ? opts.gap : 16;
    const RACK_X_L = opts.rackXL != null ? opts.rackXL : -520;
    const RACK_X_C = opts.rackXC != null ? opts.rackXC : 0;
    const RACK_X_R = opts.rackXR != null ? opts.rackXR : 520;
    const START_Y = opts.startY != null ? opts.startY : 60;
    const PAIR_X_OFFSET = opts.pairXOffset != null ? opts.pairXOffset : -220;

    const pos = {};
    const byId = Object.fromEntries((devices || []).map((d) => [d.id, d]));
    const devH = (d) => sizeOf(d).h;

    const chassis = devices.filter((d) => d.node_type === 'chassis')
      .sort((a, b) => String(a.label || '').localeCompare(String(b.label || '')));
    const switches = devices.filter((d) => d.node_type === 'switch')
      .sort((a, b) => String(a.label || '').localeCompare(String(b.label || '')));
    const ocs = devices.filter((d) => d.node_type === 'ocs');
    const servers = devices.filter((d) => d.node_type === 'server');

    const chassisPeer = {};
    (connections || []).forEach((c) => {
      const src = c.src_device != null ? c.src_device : c.source;
      const dst = c.dst_device != null ? c.dst_device : c.target;
      const a = byId[src];
      const b = byId[dst];
      if (!a || !b) return;
      if (a.node_type === 'chassis' && b.node_type === 'switch') chassisPeer[a.id] = b.id;
      if (b.node_type === 'chassis' && a.node_type === 'switch') chassisPeer[b.id] = a.id;
    });

    let y = START_Y;
    const switchTotalH = switches.reduce((s, d) => s + devH(d) + GAP, 0) - GAP;
    y = START_Y;
    switches.forEach((d) => {
      const dh = devH(d);
      pos[d.id] = { x: RACK_X_R, y: y + dh / 2 };
      y += dh + GAP;
    });

    const linked = chassis.filter((c) => chassisPeer[c.id]);
    const unlinked = chassis.filter((c) => !chassisPeer[c.id]);

    linked.forEach((c) => {
      const swId = chassisPeer[c.id];
      const swPos = pos[swId];
      const dh = devH(c);
      if (swPos) {
        pos[c.id] = { x: swPos.x + PAIR_X_OFFSET, y: swPos.y };
      } else {
        pos[c.id] = { x: RACK_X_L, y: START_Y + dh / 2 };
      }
    });

    let uy = START_Y + switchTotalH + 40;
    unlinked.forEach((d) => {
      const dh = devH(d);
      pos[d.id] = { x: RACK_X_L, y: uy + dh / 2 };
      uy += dh + GAP;
    });
    uy += 20;
    servers.forEach((d) => {
      const dh = devH(d);
      pos[d.id] = { x: RACK_X_L, y: uy + dh / 2 };
      uy += dh + GAP;
    });

    const rackSpan = Math.max(
      switchTotalH,
      linked.reduce((s, d) => s + devH(d) + GAP, 0),
    );
    const ocsCenterY = START_Y + rackSpan / 2 + 20;
    ocs.forEach((d) => { pos[d.id] = { x: RACK_X_C, y: ocsCenterY }; });

    return pos;
  }

  function mergeSavedPos(items, computedPos, isFree) {
    const pos = {};
    (items || []).forEach((d) => {
      if (isFree && d.x != null && d.y != null && (d.x !== 0 || d.y !== 0)) {
        pos[d.id] = { x: d.x, y: d.y };
      } else if (!isFree && computedPos[d.id]) {
        pos[d.id] = computedPos[d.id];
      } else if (d.x != null && d.y != null && (d.x !== 0 || d.y !== 0)) {
        pos[d.id] = { x: d.x, y: d.y };
      } else {
        pos[d.id] = computedPos[d.id] || { x: 0, y: 0 };
      }
    });
    return pos;
  }

  function setSegActive(mode) {
    ['tier', 'rack', 'free'].forEach((m) => {
      const btn = document.getElementById('lv-layout-' + m);
      if (btn) btn.classList.toggle('lv-active-toggle', mode === m);
    });
  }

  function lsKey(topoId, view) {
    return 'lv_layout_' + topoId + '_' + (view || 'default');
  }

  function loadMode(topoId, view, fallback) {
    try {
      return localStorage.getItem(lsKey(topoId, view)) || fallback || 'rack';
    } catch (e) {
      return fallback || 'rack';
    }
  }

  function saveMode(topoId, view, mode) {
    try {
      localStorage.setItem(lsKey(topoId, view), mode);
    } catch (e) { /* ignore */ }
  }

  function bindLayoutToolbar(opts) {
    opts = opts || {};
    const topoId = opts.topoId;
    const view = opts.view || 'default';
    const onSelect = typeof opts.onSelect === 'function' ? opts.onSelect : function () {};
    const getMode = typeof opts.getMode === 'function' ? opts.getMode : function () { return 'rack'; };

    function activate(uiMode) {
      saveMode(topoId, view, uiMode);
      setSegActive(uiMode);
      try {
        onSelect(uiMode);
      } catch (e) {
        console.error('layout onSelect failed', e);
      }
    }

    ['tier', 'rack', 'free'].forEach((uiMode) => {
      const btn = document.getElementById('lv-layout-' + uiMode);
      if (!btn || btn.dataset.lvLayoutBound === '1') return;
      btn.dataset.lvLayoutBound = '1';
      btn.addEventListener('click', () => activate(uiMode));
    });

    setSegActive(getMode());
  }

  const api = {
    tierOf,
    computeTierLayout,
    computeDCLayout,
    mergeSavedPos,
    setSegActive,
    loadMode,
    saveMode,
    bindLayoutToolbar,
    DEFAULT_TIER_Y,
  };

  global.TopoLayoutModes = api;
})(typeof window !== 'undefined' ? window : global);
