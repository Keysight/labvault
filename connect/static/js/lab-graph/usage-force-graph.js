/**
 * Lab topology utilization view — chassis-faithful port layout + timeline sync.
 */

import {
  PW,
  PH,
  PPITCH,
  HDR,
  GLBL,
  DPAD,
  measureDevice,
  buildPortLayoutMap,
  parseRgGroupLabel,
  clusterChassisPortGroups,
  useChassisRgLayout,
  portsPerRow,
  filterDeviceBySlot,
  filterDeviceBySlots,
} from './chassis-layout.js';

const SLOT_ROW_HDR = 11;

const TIER_COLORS = {
  switch: '#4ade80',
  chassis: '#a78bfa',
  ocs: '#fb923c',
  firewall: '#f87171',
  server: '#eab308',
  generic: '#64748b',
};

function heatColor(duty, inUse) {
  if (inUse) return '#ef4444';
  if (duty >= 0.75) return '#f97316';
  if (duty >= 0.5) return '#f59e0b';
  if (duty >= 0.25) return '#3b82f6';
  if (duty > 0.05) return '#22c55e';
  return '#334155';
}

function resourceKeyForPort(port) {
  return port.resource_key || port.id || '';
}

function deviceIdFromResourceKey(rkey) {
  if (!rkey) return null;
  if (!rkey.includes('__')) return rkey;
  return rkey.split('__', 1)[0];
}

function layoutDevices(devices, nodes, width, height) {
  const posById = new Map((nodes || []).map((n) => [n.id, { x: n.x, y: n.y }]));
  const count = Math.max(1, devices.length);
  const cols = count <= 1 ? 1 : count <= 4 ? 2 : 3;
  const pad = 40;
  const cellW = (width - pad * 2) / cols;
  const cellH = (height - pad * 2) / Math.ceil(count / cols);
  return devices.map((dev, i) => {
    const saved = posById.get(dev.id);
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      return { dev, x: saved.x, y: saved.y };
    }
    const col = i % cols;
    const row = Math.floor(i / cols);
    return {
      dev,
      x: pad + cellW * (col + 0.5),
      y: pad + cellH * (row + 0.5),
    };
  });
}

function drawPortSquare(g, port, dev, px, py, onSelect, onHover) {
  const pg = g.append('g')
    .attr('class', 'ug-port')
    .attr('transform', `translate(${px},${py})`)
    .attr('cursor', 'pointer')
    .datum(port);

  const u = port.utilization || {};
  const fill = u.color || heatColor(u.duty_cycle_31d || 0, u.in_use);
  const rkey = resourceKeyForPort(port);

  pg.append('rect')
    .attr('width', PW)
    .attr('height', PH)
    .attr('rx', 2)
    .attr('fill', fill)
    .attr('stroke', u.reserved ? '#1d4ed8' : '#0f172a')
    .attr('stroke-width', u.reserved ? 1.5 : 0.5);

  pg.append('text')
    .attr('x', PW / 2)
    .attr('y', PH + 9)
    .attr('text-anchor', 'middle')
    .attr('fill', '#64748b')
    .attr('font-size', 6)
    .attr('pointer-events', 'none')
    .text(String(port.port_display || port.label || '').slice(0, 8));

  pg.on('click', (ev) => {
    ev.stopPropagation();
    if (onSelect) {
      onSelect({ kind: 'port', id: `port:${rkey}`, label: port.label, parent: dev.id }, dev);
    }
  })
    .on('mouseenter', (ev) => {
      ev.stopPropagation();
      if (onHover) onHover(rkey);
    })
    .on('mouseleave', () => { if (onHover) onHover(null); });

  return pg;
}

export function renderUsageGraph(container, graph, {
  onSelect,
  onHover,
  highlightKey,
  activeKeys,
  focusDeviceId,
  focusDeviceIds,
  focusSlot,
  focusSlots,
} = {}) {
  const el = typeof container === 'string' ? document.querySelector(container) : container;
  if (!el || !window.d3) return null;

  const d3 = window.d3;
  el.innerHTML = '';
  const width = el.clientWidth || 960;
  const height = el.clientHeight || 640;

  const svg = d3.select(el).append('svg')
    .attr('width', '100%')
    .attr('height', '100%')
    .attr('viewBox', [0, 0, width, height])
    .attr('class', 'ug-topology-svg');

  const gRoot = svg.append('g');
  const gLinks = gRoot.append('g').attr('class', 'ug-links');
  const gDevices = gRoot.append('g').attr('class', 'ug-devices');

  const zoom = d3.zoom().scaleExtent([0.25, 3]).on('zoom', (e) => {
    gRoot.attr('transform', e.transform);
  });
  svg.call(zoom);

  let devices = (graph.devices || []).slice();
  if (focusDeviceIds?.size) {
    devices = devices.filter((d) => focusDeviceIds.has(d.id));
  } else if (focusDeviceId && focusDeviceId !== 'all') {
    devices = devices.filter((d) => d.id === focusDeviceId);
  }
  if (focusSlots?.size) {
    devices = devices.map((d) => filterDeviceBySlots(d, focusSlots));
  } else if (focusSlot && focusSlot !== 'all') {
    devices = devices.map((d) => filterDeviceBySlot(d, focusSlot));
  }
  const nodeById = new Map((graph.nodes || []).map((n) => [n.id, n]));
  const layouts = layoutDevices(devices, graph.nodes, width, height);

  const portGlobalPos = new Map();
  const deviceLayoutMeta = new Map();

  layouts.forEach(({ dev, x, y }) => {
    const { w, h } = measureDevice(dev);
    deviceLayoutMeta.set(dev.id, { x, y, w, h });
    const local = buildPortLayoutMap(dev);
    local.forEach((pos, rkey) => {
      portGlobalPos.set(rkey, { x: x - w / 2 + pos.x, y: y - h / 2 + pos.y });
    });
  });

  const portLinks = graph.port_links || [];
  gLinks.selectAll('line.ug-port-link')
    .data(portLinks.filter((l) => {
      const a = l.resource_key_a || (l.source || '').replace(/^port:/, '');
      const b = l.resource_key_b || (l.target || '').replace(/^port:/, '');
      return portGlobalPos.has(a) && portGlobalPos.has(b);
    }))
    .join('line')
    .attr('class', 'ug-port-link')
    .attr('x1', (l) => portGlobalPos.get(l.resource_key_a || l.source.replace(/^port:/, '')).x)
    .attr('y1', (l) => portGlobalPos.get(l.resource_key_a || l.source.replace(/^port:/, '')).y)
    .attr('x2', (l) => portGlobalPos.get(l.resource_key_b || l.target.replace(/^port:/, '')).x)
    .attr('y2', (l) => portGlobalPos.get(l.resource_key_b || l.target.replace(/^port:/, '')).y)
    .attr('stroke', (l) => l.color || '#475569')
    .attr('stroke-width', 1.5)
    .attr('stroke-opacity', 0.55);

  const devLinks = (graph.links || []).filter(
    (l) => deviceLayoutMeta.has(l.source) && deviceLayoutMeta.has(l.target),
  );
  gLinks.selectAll('line.ug-dev-link')
    .data(devLinks)
    .join('line')
    .attr('class', 'ug-dev-link')
    .attr('x1', (l) => deviceLayoutMeta.get(l.source).x)
    .attr('y1', (l) => deviceLayoutMeta.get(l.source).y)
    .attr('x2', (l) => deviceLayoutMeta.get(l.target).x)
    .attr('y2', (l) => deviceLayoutMeta.get(l.target).y)
    .attr('stroke', (l) => l.color || '#334155')
    .attr('stroke-width', 1)
    .attr('stroke-dasharray', '4 3')
    .attr('stroke-opacity', 0.35);

  const devSel = gDevices.selectAll('g.ug-device')
    .data(layouts, (d) => d.dev.id)
    .join('g')
    .attr('class', 'ug-device');

  devSel.attr('transform', (d) => `translate(${d.x},${d.y})`);

  devSel.each(function (entry) {
    const g = d3.select(this);
    g.selectAll('*').remove();
    const dev = entry.dev;
    const meta = nodeById.get(dev.id) || {};
    const { w, h } = measureDevice(dev);
    const tier = dev.node_type || meta.kind || 'generic';
    const stroke = TIER_COLORS[tier] || TIER_COLORS.generic;
    const duty = dev.utilization?.duty_cycle_31d || 0;

    g.append('rect')
      .attr('class', 'ug-dev-bg')
      .attr('x', -w / 2)
      .attr('y', -h / 2)
      .attr('width', w)
      .attr('height', h)
      .attr('rx', 8)
      .attr('fill', '#0c1829')
      .attr('stroke', stroke)
      .attr('stroke-width', 2);

    g.append('rect')
      .attr('x', -w / 2)
      .attr('y', h / 2 - 6)
      .attr('width', w * Math.min(1, duty))
      .attr('height', 4)
      .attr('fill', heatColor(duty, false))
      .attr('opacity', 0.85);

    g.append('text')
      .attr('text-anchor', 'middle')
      .attr('y', -h / 2 + 16)
      .attr('fill', '#e2e8f0')
      .attr('font-size', 11)
      .attr('font-weight', 600)
      .text((dev.label || dev.id).slice(0, 28));

    g.append('text')
      .attr('text-anchor', 'middle')
      .attr('y', -h / 2 + 30)
      .attr('fill', '#64748b')
      .attr('font-size', 9)
      .text(`${dev.utilization?.ports_in_use || 0}/${dev.utilization?.ports_total || dev.ports?.length || 0} active · ${formatDuty(duty)}`);

    const portG = g.append('g').attr('class', 'ug-ports')
      .attr('transform', `translate(${-w / 2},${-h / 2})`);

    if (useChassisRgLayout(dev)) {
      let y = HDR;
      for (const row of clusterChassisPortGroups(dev.port_groups)) {
        if (row.type === 'rg_row') {
          portG.append('text')
            .attr('x', DPAD)
            .attr('y', y + 9)
            .attr('fill', '#64748b')
            .attr('font-size', 7)
            .text(row.slot.slice(0, 40));
          y += SLOT_ROW_HDR;
          let bx = DPAD;
          for (const grp of row.groups) {
            const rgLbl = (parseRgGroupLabel(grp.label) || {}).rg || '';
            portG.append('text')
              .attr('x', bx + 2)
              .attr('y', y + 8)
              .attr('fill', '#475569')
              .attr('font-size', 6.5)
              .text(rgLbl.slice(0, 8));
            const ports = grp.ports || [];
            const pr = portsPerRow(dev, { ...grp, layout: 'rg_horizontal' });
            const portY = y + GLBL;
            ports.forEach((port, i) => {
              const col = i % pr;
              const r = Math.floor(i / pr);
              drawPortSquare(portG, port, dev, bx + col * PPITCH, portY + r * (PH + 6), onSelect, onHover);
            });
            const cols = Math.min(ports.length, 4) || 1;
            bx += cols * PPITCH + 8;
          }
          const maxN = Math.max(...row.groups.map((grp) => (grp.ports || []).length), 1);
          y += GLBL + Math.ceil(maxN / 4) * (PH + 6) + 14 + 7;
        } else {
          const grp = row.group;
          portG.append('text')
            .attr('x', DPAD)
            .attr('y', y + GLBL - 3)
            .attr('fill', '#475569')
            .attr('font-size', 7)
            .text((grp.label || '').slice(0, 36));
          y += GLBL;
          const pr = portsPerRow(dev, grp);
          (grp.ports || []).forEach((port, i) => {
            const col = i % pr;
            const r = Math.floor(i / pr);
            drawPortSquare(portG, port, dev, DPAD + col * PPITCH, y + r * (PH + 6), onSelect, onHover);
          });
          y += Math.ceil(((grp.ports || []).length) / pr) * (PH + 6) + 14;
        }
      }
    } else if ((dev.ocs_shelves || []).length) {
      let y = HDR;
      for (const shelf of dev.ocs_shelves) {
        portG.append('text')
          .attr('x', DPAD)
          .attr('y', y + 10)
          .attr('fill', '#64748b')
          .attr('font-size', 7)
          .text((shelf.label || 'Shelf').slice(0, 24));
        y += 14;
        let bx = DPAD;
        for (const bank of shelf.banks || []) {
          (bank.ports || []).forEach((port, i) => {
            const col = i % 4;
            const row = Math.floor(i / 4);
            drawPortSquare(portG, port, dev, bx + col * 12, y + row * 14, onSelect, onHover);
          });
          bx += 62;
        }
        y += 36;
      }
    } else if (dev.port_groups?.length) {
      let y = HDR;
      for (const grp of dev.port_groups) {
        portG.append('text')
          .attr('x', DPAD)
          .attr('y', y + GLBL - 3)
          .attr('fill', '#475569')
          .attr('font-size', 7)
          .text((grp.label || '').slice(0, 36));
        y += GLBL;
        const pr = portsPerRow(dev, grp);
        (grp.ports || []).forEach((port, i) => {
          const col = i % pr;
          const r = Math.floor(i / pr);
          drawPortSquare(portG, port, dev, DPAD + col * PPITCH, y + r * (PH + 6), onSelect, onHover);
        });
        y += Math.ceil(((grp.ports || []).length) / pr) * (PH + 6) + 14;
      }
    } else {
      (dev.ports || []).forEach((port, i) => {
        const cols = Math.min(8, Math.max(4, Math.ceil(Math.sqrt((dev.ports || []).length))));
        const col = i % cols;
        const row = Math.floor(i / cols);
        drawPortSquare(portG, port, dev, DPAD + col * PPITCH, HDR + 4 + row * (PH + 6), onSelect, onHover);
      });
    }
  });

  devSel.on('click', (_, entry) => {
    if (onSelect) {
      onSelect({
        kind: 'device',
        id: entry.dev.id,
        label: entry.dev.label,
        duty: entry.dev.utilization?.duty_cycle_31d || 0,
      }, entry.dev);
    }
  });

  let filterKeys = activeKeys;
  let currentHighlight = highlightKey;

  function portOpacity(rkey) {
    if (!filterKeys || !filterKeys.size) return 1;
    if (filterKeys.has(rkey)) return 1;
    const devId = deviceIdFromResourceKey(rkey);
    if (devId && filterKeys.has(devId)) return 1;
    return 0.12;
  }

  function deviceOpacity(devId) {
    if (!filterKeys || !filterKeys.size) return 1;
    if (filterKeys.has(devId)) return 1;
    for (const k of filterKeys) {
      if (k.startsWith(`${devId}__`)) return 1;
    }
    return 0.2;
  }

  function portMatchesHighlight(hk, rkey) {
    if (!hk) return true;
    if (hk === rkey) return true;
    const devId = deviceIdFromResourceKey(rkey);
    if (hk === devId) return true;
    if (hk.startsWith(`${devId}__`)) return hk === rkey;
    return false;
  }

  function applyHighlight(key) {
    const hk = key ?? currentHighlight;
    currentHighlight = hk;
    gDevices.selectAll('g.ug-device').attr('opacity', (d) => {
      const base = deviceOpacity(d.dev.id);
      if (!hk) return base;
      const match = hk === d.dev.id || hk.startsWith(`${d.dev.id}__`);
      return match ? base : Math.min(base, 0.18);
    });
    gDevices.selectAll('g.ug-port').attr('opacity', (p) => {
      const rkey = resourceKeyForPort(p);
      const base = portOpacity(rkey);
      if (!hk) return base;
      return portMatchesHighlight(hk, rkey) ? base : Math.min(base, 0.15);
    });
    gLinks.selectAll('line').attr('stroke-opacity', function opacityFn() {
      const cls = d3.select(this).attr('class') || '';
      const isPort = cls.includes('ug-port-link');
      if (!hk && (!filterKeys || !filterKeys.size)) return isPort ? 0.55 : 0.35;
      if (isPort) {
        const l = d3.select(this).datum();
        const a = l.resource_key_a || '';
        const b = l.resource_key_b || '';
        if (hk && (hk === a || hk === b)) return 0.9;
        if (filterKeys?.size && (filterKeys.has(a) || filterKeys.has(b))) return 0.75;
        return 0.06;
      }
      return 0.12;
    });
  }

  applyHighlight(currentHighlight);

  function fitView() {
    const pad = (focusDeviceIds?.size === 1 || (focusDeviceId && focusDeviceId !== 'all')) ? 32 : 48;
    let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
    layouts.forEach(({ x, y, dev }) => {
      const { w, h } = measureDevice(dev);
      minX = Math.min(minX, x - w / 2);
      maxX = Math.max(maxX, x + w / 2);
      minY = Math.min(minY, y - h / 2);
      maxY = Math.max(maxY, y + h / 2);
    });
    if (!Number.isFinite(minX)) return;
    const bw = maxX - minX + pad * 2;
    const bh = maxY - minY + pad * 2;
    const maxScale = devices.length <= 1 ? 3.5 : 2.5;
    const scale = Math.min(maxScale, Math.max(0.35, Math.min(width / bw, height / bh)));
    const tx = width / 2 - ((minX + maxX) / 2) * scale;
    const ty = height / 2 - ((minY + maxY) / 2) * scale;
    svg.transition().duration(400).call(
      zoom.transform,
      d3.zoomIdentity.translate(tx, ty).scale(scale),
    );
  }
  fitView();

  return {
    svg,
    setHighlight: (k) => applyHighlight(k),
    setActiveFilter: (keys) => {
      filterKeys = keys;
      applyHighlight(currentHighlight);
    },
    resetView: fitView,
    destroy: () => { el.innerHTML = ''; },
  };
}

export function formatDuty(pct) {
  return `${Math.round((pct || 0) * 100)}%`;
}
