/**
 * Granular topology/timeline filter key resolution (multi device, slot, owner, port).
 */

import { intersectKeySets, resourcesForOwners, resourcesHighUsage } from './timeline-filters.js';
import { iterDevicePorts, listDeviceSlots, parseRgGroupLabel } from './chassis-layout.js';

export { intersectKeySets, resourcesForOwners, resourcesHighUsage };

/** All resource keys (device + ports) for selected chassis node ids. */
export function resourceKeysForDevices(graph, deviceIdSet) {
  if (!deviceIdSet?.size) return null;
  const keys = new Set();
  for (const dev of graph?.devices || []) {
    if (!deviceIdSet.has(dev.id)) continue;
    keys.add(dev.id);
    for (const p of iterDevicePorts(dev)) {
      const rk = p.resource_key || p.id;
      if (rk) keys.add(rk);
    }
  }
  return keys;
}

function slotKeyForPortGroup(pg) {
  const p = parseRgGroupLabel(pg.label);
  return p ? p.slot : (pg.label || 'Other');
}

/** Port keys on selected devices whose fanout/slot label is in slotSet. */
export function resourceKeysForSlots(graph, deviceIdSet, slotSet) {
  if (!slotSet?.size) return null;
  const keys = new Set();
  let devs = graph?.devices || [];
  if (deviceIdSet?.size) devs = devs.filter((d) => deviceIdSet.has(d.id));
  for (const dev of devs) {
    keys.add(dev.id);
    if (dev.port_groups?.length) {
      for (const pg of dev.port_groups) {
        if (!slotSet.has(slotKeyForPortGroup(pg))) continue;
        for (const p of pg.ports || []) {
          const rk = p.resource_key || p.id;
          if (rk) keys.add(rk);
        }
      }
    } else {
      for (const p of iterDevicePorts(dev)) {
        const rk = p.resource_key || p.id;
        if (rk) keys.add(rk);
      }
    }
  }
  return keys;
}

export function resourceKeysForPorts(portKeySet) {
  if (!portKeySet?.size) return null;
  const keys = new Set(portKeySet);
  for (const k of portKeySet) {
    if (k.includes('__')) keys.add(k.split('__', 1)[0]);
  }
  return keys;
}

/** Union of slot labels across devices (optionally restricted to deviceIdSet). */
export function listSlotsForDevices(graph, deviceIdSet) {
  const slots = [];
  const seen = new Set();
  let devs = graph?.devices || [];
  if (deviceIdSet?.size) devs = devs.filter((d) => deviceIdSet.has(d.id));
  for (const dev of devs) {
    for (const s of listDeviceSlots(dev)) {
      if (!seen.has(s)) {
        seen.add(s);
        slots.push(s);
      }
    }
  }
  return slots;
}

/**
 * Intersect all active granular filters.
 * @param {object} opts
 * @param {object} opts.graph - fabric graph
 * @param {object} opts.timeline - timeline JSON
 * @param {number} opts.fromMs
 * @param {number} opts.toMs
 * @param {Set<string>} opts.deviceIds
 * @param {Set<string>} opts.slots
 * @param {Set<string>} opts.owners
 * @param {Set<string>} opts.portKeys
 * @param {boolean} opts.highUsageOnly
 * @param {number} opts.highUsageThreshold
 */
export function computeGranularFilterKeys({
  graph,
  timeline,
  fromMs,
  toMs,
  deviceIds,
  slots,
  owners,
  portKeys,
  highUsageOnly,
  highUsageThreshold,
}) {
  let keys = null;
  if (deviceIds?.size) {
    keys = intersectKeySets(keys, resourceKeysForDevices(graph, deviceIds));
  }
  if (slots?.size) {
    keys = intersectKeySets(keys, resourceKeysForSlots(graph, deviceIds, slots));
  }
  if (owners?.size && timeline) {
    keys = intersectKeySets(keys, resourcesForOwners(timeline, owners, fromMs, toMs));
  }
  if (portKeys?.size) {
    keys = intersectKeySets(keys, resourceKeysForPorts(portKeys));
  }
  if (highUsageOnly && timeline) {
    keys = intersectKeySets(keys, resourcesHighUsage(timeline, fromMs, toMs, highUsageThreshold));
  }
  return keys;
}

export function hasGranularFilters(state) {
  return !!(
    state.deviceIds?.size
    || state.slots?.size
    || state.owners?.size
    || state.portKeys?.size
    || state.highUsageOnly
  );
}
