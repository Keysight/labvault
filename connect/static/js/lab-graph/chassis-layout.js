/**
 * Chassis / port-group layout — mirrors lab_port_fabric.html geometry for usage view.
 */

export const PW = 14;
export const PH = 11;
export const PGAP = 3;
export const PPITCH = PW + PGAP;
export const PROW = 8;
export const HDR = 42;
export const GLBL = 13;
export const GROW = PH + PGAP + 3;
export const PLBL = 14;
export const DPAD = 7;
export const DMIN = 158;
export const OCS_BANK_W = 58;
export const OCS_BANK_H = 30;
export const OCS_SHELF_HDR = 14;
export const OCS_SHELF_GAP = 5;
export const OCS_PORT_W = 11;
export const OCS_PORT_H = 10;
export const OCS_PORT_GAP = 2;
export const RG_H_GAP = 8;
export const SLOT_ROW_HDR = 11;
export const FANOUT_STUB = 7;

export function parseRgGroupLabel(lbl) {
  const m = /^(.+?)\s*·\s*(RG\d+|Resource Group \d+)/i.exec(lbl || '');
  return m ? { slot: m[1].trim(), rg: m[2].trim() } : null;
}

export function clusterChassisPortGroups(portGroups) {
  const rows = [];
  let i = 0;
  while (i < (portGroups || []).length) {
    const g = portGroups[i];
    const p = parseRgGroupLabel(g.label);
    if (p && g.role !== 'ocs') {
      const groups = [g];
      let j = i + 1;
      while (j < portGroups.length) {
        const p2 = parseRgGroupLabel(portGroups[j].label);
        if (p2 && p2.slot === p.slot && portGroups[j].role !== 'ocs') groups.push(portGroups[j]);
        else break;
        j += 1;
      }
      rows.push({ type: 'rg_row', slot: p.slot, groups });
      i = j;
    } else {
      rows.push({ type: 'single', group: g });
      i += 1;
    }
  }
  return rows;
}

export function useChassisRgLayout(dev) {
  return dev.node_type === 'chassis' && (dev.port_groups || []).some((g) => parseRgGroupLabel(g.label));
}

export function portsPerRow(dev, g) {
  if (dev.node_type === 'ocs' || g.layout === 'ocs_bank') return 8;
  if (g.layout === 'rg_horizontal') return Math.min((g.ports || []).length, 4) || 1;
  return PROW;
}

function rgGroupWidth(grp) {
  const n = (grp.ports || []).length || 1;
  const cols = Math.min(n, 4);
  return cols * PPITCH + 6;
}

function groupGridH(g, dev) {
  if (g.layout === 'rg_horizontal') {
    const n = (g.ports || []).length || 1;
    return GLBL + Math.ceil(n / Math.min(n, 4)) * GROW + PLBL + FANOUT_STUB;
  }
  const pr = portsPerRow(dev, g);
  const rows = Math.ceil(((g.ports || []).length) / pr) || 0;
  return rows * GROW + PLBL;
}

function grpH(g, dev) {
  return GLBL + groupGridH(g, dev);
}

function ocsDevW(dev) {
  let maxW = DMIN;
  for (const sh of dev.ocs_shelves || []) {
    const bw = (sh.banks || []).length * (OCS_BANK_W + 4) + DPAD * 2;
    maxW = Math.max(maxW, bw);
  }
  return Math.min(maxW, 920);
}

function ocsDevH(dev) {
  let h = HDR;
  for (const _sh of dev.ocs_shelves || []) {
    h += OCS_SHELF_HDR + OCS_BANK_H + OCS_SHELF_GAP;
  }
  return h + DPAD;
}

function chassisDevH(dev) {
  let h = HDR;
  for (const row of clusterChassisPortGroups(dev.port_groups)) {
    if (row.type === 'rg_row') {
      h += SLOT_ROW_HDR;
      const maxN = Math.max(...row.groups.map((g) => (g.ports || []).length), 1);
      h += GLBL + Math.ceil(maxN / 4) * GROW + PLBL + FANOUT_STUB;
    } else {
      h += grpH(row.group, dev);
    }
  }
  return h + DPAD;
}

function chassisDevW(dev) {
  let maxW = DMIN;
  for (const row of clusterChassisPortGroups(dev.port_groups)) {
    if (row.type === 'rg_row') {
      const w = row.groups.reduce((s, g) => s + rgGroupWidth(g) + RG_H_GAP, DPAD * 2) - RG_H_GAP;
      maxW = Math.max(maxW, w);
    } else {
      const g = row.group;
      const cols = Math.min(portsPerRow(dev, g), (g.ports || []).length || 1);
      maxW = Math.max(maxW, cols * PPITCH + DPAD * 2);
    }
  }
  return maxW;
}

export function measureDevice(dev) {
  if ((dev.ocs_shelves || []).length) {
    return { w: ocsDevW(dev), h: ocsDevH(dev), mode: 'ocs' };
  }
  if (useChassisRgLayout(dev)) {
    return { w: chassisDevW(dev), h: chassisDevH(dev), mode: 'chassis_rg' };
  }
  let maxCols = PROW;
  for (const g of dev.port_groups || []) {
    maxCols = Math.max(maxCols, Math.min(portsPerRow(dev, g), (g.ports || []).length || portsPerRow(dev, g)));
  }
  let h = HDR + DPAD;
  for (const g of dev.port_groups || []) h += grpH(g, dev);
  if (!dev.port_groups?.length && dev.ports?.length) {
    const cols = Math.min(PROW, Math.max(4, Math.ceil(Math.sqrt(dev.ports.length))));
    const rows = Math.ceil(dev.ports.length / cols);
    return { w: Math.max(DMIN, cols * PPITCH + DPAD * 2), h: HDR + rows * GROW + PLBL + DPAD, mode: 'flat' };
  }
  return { w: Math.max(DMIN, maxCols * PPITCH + DPAD * 2), h: Math.max(HDR + DPAD, h), mode: 'groups' };
}

function portKey(port) {
  return port.resource_key || port.id || '';
}

function setLayoutPos(layout, key, x, y) {
  if (key) layout.set(key, { x, y });
}

function layoutOcsPorts(dev, layout) {
  let y = HDR;
  for (const shelf of dev.ocs_shelves || []) {
    y += OCS_SHELF_HDR;
    let bx = DPAD;
    for (const bank of shelf.banks || []) {
      const ports = bank.ports || [];
      for (let i = 0; i < ports.length; i += 1) {
        const col = i % 4;
        const row = Math.floor(i / 4);
        const px = bx + 4 + col * (OCS_PORT_W + OCS_PORT_GAP);
        const py = y + 10 + row * (OCS_PORT_H + OCS_PORT_GAP);
        setLayoutPos(layout, portKey(ports[i]), px + OCS_PORT_W / 2, py + OCS_PORT_H / 2);
      }
      bx += OCS_BANK_W + 4;
    }
    y += OCS_BANK_H + OCS_SHELF_GAP;
  }
}

function layoutChassisRg(dev, layout) {
  let y = HDR;
  for (const row of clusterChassisPortGroups(dev.port_groups)) {
    if (row.type === 'rg_row') {
      y += SLOT_ROW_HDR;
      let bx = DPAD;
      for (const grp of row.groups) {
        const ports = grp.ports || [];
        const pr = portsPerRow(dev, { ...grp, layout: 'rg_horizontal' });
        const portY = y + GLBL;
        ports.forEach((port, i) => {
          const col = i % pr;
          const r = Math.floor(i / pr);
          const px = bx + col * PPITCH;
          const py = portY + r * GROW;
          setLayoutPos(layout, portKey(port), px + PW / 2, py + PH / 2);
        });
        bx += rgGroupWidth(grp) + RG_H_GAP;
      }
      const maxN = Math.max(...row.groups.map((g) => (g.ports || []).length), 1);
      y += GLBL + Math.ceil(maxN / 4) * GROW + PLBL + FANOUT_STUB;
    } else {
      const g = row.group;
      const pr = portsPerRow(dev, g);
      y += GLBL;
      (g.ports || []).forEach((port, i) => {
        const col = i % pr;
        const r = Math.floor(i / pr);
        const px = DPAD + col * PPITCH;
        const py = y + r * GROW;
        setLayoutPos(layout, portKey(port), px + PW / 2, py + PH / 2);
      });
      y += groupGridH(g, dev) - GLBL;
    }
  }
}

function layoutStandardGroups(dev, layout) {
  let y = HDR;
  for (const g of dev.port_groups || []) {
    y += GLBL;
    const pr = portsPerRow(dev, g);
    (g.ports || []).forEach((port, i) => {
      const col = i % pr;
      const r = Math.floor(i / pr);
      const px = DPAD + col * PPITCH;
      const py = y + r * GROW;
      setLayoutPos(layout, portKey(port), px + PW / 2, py + PH / 2);
    });
    y += groupGridH(g, dev) - GLBL;
  }
}

function layoutFlatPorts(dev, layout) {
  const ports = dev.ports || [];
  const cols = Math.min(PROW, Math.max(4, Math.ceil(Math.sqrt(ports.length))));
  let y = HDR + 4;
  ports.forEach((port, i) => {
    const col = i % cols;
    const r = Math.floor(i / cols);
    const px = DPAD + col * PPITCH;
    const py = y + r * GROW;
    setLayoutPos(layout, portKey(port), px + PW / 2, py + PH / 2);
  });
}

/** resource_key → {x,y} in device-local coordinates (top-left origin). */
export function buildPortLayoutMap(dev) {
  const layout = new Map();
  if ((dev.ocs_shelves || []).length) layoutOcsPorts(dev, layout);
  else if (useChassisRgLayout(dev)) layoutChassisRg(dev, layout);
  else if (dev.port_groups?.length) layoutStandardGroups(dev, layout);
  else layoutFlatPorts(dev, layout);
  return layout;
}

export function listDeviceSlots(dev) {
  const slots = [];
  const seen = new Set();
  for (const pg of dev.port_groups || []) {
    const p = parseRgGroupLabel(pg.label);
    const key = p ? p.slot : (pg.label || 'Other');
    if (!seen.has(key)) {
      seen.add(key);
      slots.push(key);
    }
  }
  return slots;
}

/** Return shallow copy of device with port_groups filtered to one slot. */
export function filterDeviceBySlot(dev, slotKey) {
  if (!slotKey || slotKey === 'all') return dev;
  return filterDeviceBySlots(dev, new Set([slotKey]));
}

/** Return shallow copy with port_groups in any of slotKeys (empty set = unchanged). */
export function filterDeviceBySlots(dev, slotKeys) {
  if (!slotKeys?.size) return dev;
  const port_groups = (dev.port_groups || []).filter((pg) => slotKeys.has(slotKeyForPortGroup(pg)));
  const portKeys = new Set();
  for (const pg of port_groups) {
    for (const p of pg.ports || []) portKeys.add(p.resource_key || p.id);
  }
  const ports = (dev.ports || []).filter((p) => portKeys.has(p.resource_key));
  return { ...dev, port_groups, ports };
}

function slotKeyForPortGroup(pg) {
  const p = parseRgGroupLabel(pg.label);
  return p ? p.slot : (pg.label || 'Other');
}

export function iterDevicePorts(dev) {
  const out = [];
  for (const sh of dev.ocs_shelves || []) {
    for (const bank of sh.banks || []) {
      for (const p of bank.ports || []) out.push(p);
    }
  }
  if (dev.port_groups?.length) {
    for (const pg of dev.port_groups) {
      for (const p of pg.ports || []) out.push(p);
    }
    return out;
  }
  return dev.ports || [];
}
