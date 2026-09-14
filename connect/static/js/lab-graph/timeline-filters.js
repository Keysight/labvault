/**
 * Timeline lane filtering: ownership, high CPU/mem usage, slice bounds.
 */

function parseTs(iso) {
  return iso ? new Date(iso).getTime() : 0;
}

function normalizeEvents(events) {
  if (!events) return {};
  if (Array.isArray(events)) {
    const grouped = {};
    for (const e of events) {
      const k = e.resource_key;
      if (!grouped[k]) grouped[k] = [];
      grouped[k].push(e);
    }
    return grouped;
  }
  return events;
}

export function listOwnersFromTimeline(data) {
  const owners = new Set();
  const grouped = normalizeEvents(data?.events);
  for (const evts of Object.values(grouped)) {
    for (const e of evts) {
      const pl = e.payload || {};
      if (e.event_type === 'port_owned' && pl.owner && pl.owner !== 'Free') {
        owners.add(pl.owner);
      }
    }
  }
  return [...owners].sort((a, b) => a.localeCompare(b));
}

/** Port resource keys with port_owned overlapping [fromMs, toMs) for given owners. */
export function resourcesForOwners(data, ownerSet, fromMs, toMs) {
  if (!ownerSet?.size) return null;
  const keys = new Set();
  const winEnd = parseTs(data?.window?.to);
  const grouped = normalizeEvents(data?.events);
  for (const [key, evts] of Object.entries(grouped)) {
    for (const e of evts) {
      if (e.event_type !== 'port_owned') continue;
      const owner = e.payload?.owner;
      if (!owner || !ownerSet.has(owner)) continue;
      const start = parseTs(e.started_at);
      const end = parseTs(e.ended_at || data?.window?.to) || winEnd;
      if (end > fromMs && start < toMs) {
        keys.add(key);
        break;
      }
    }
  }
  return keys;
}

const DEFAULT_USAGE_METRICS = ['cpu_pct', 'mem_pct'];

/** Resource keys where any usage metric exceeds threshold in [fromMs, toMs). */
export function resourcesHighUsage(data, fromMs, toMs, threshold, metrics = DEFAULT_USAGE_METRICS) {
  const keys = new Set();
  const metricSet = new Set(metrics);
  for (const [key, buckets] of Object.entries(data?.metric_buckets || {})) {
    for (const [metric, series] of Object.entries(buckets || {})) {
      if (!metricSet.has(metric)) continue;
      for (const [iso, val] of series || []) {
        const t = parseTs(iso);
        if (t >= fromMs && t < toMs && Number(val) >= threshold) {
          keys.add(key);
          break;
        }
      }
      if (keys.has(key)) break;
    }
  }
  return keys;
}

export function intersectKeySets(...sets) {
  const valid = sets.filter((s) => s && s.size);
  if (!valid.length) return null;
  let out = new Set(valid[0]);
  for (let i = 1; i < valid.length; i += 1) {
    out = new Set([...out].filter((k) => valid[i].has(k)));
  }
  return out;
}

export function unionKeySets(...sets) {
  const out = new Set();
  for (const s of sets) {
    if (!s) continue;
    for (const k of s) out.add(k);
  }
  return out.size ? out : null;
}

/** Keep device lanes when any child port matches; keep matching port lanes. */
export function filterLanes(lanes, allowedKeys) {
  if (!allowedKeys?.size) return lanes;
  const portMatches = new Set();
  const deviceMatches = new Set();
  for (const key of allowedKeys) {
    if (key.includes('__')) portMatches.add(key);
    else deviceMatches.add(key);
    const parent = key.includes('__') ? key.split('__', 1)[0] : null;
    if (parent) deviceMatches.add(parent);
  }
  return lanes.filter((lane) => {
    if (allowedKeys.has(lane.key)) return true;
    if (lane.laneType === 'device') {
      return [...portMatches].some((pk) => pk.startsWith(`${lane.key}__`))
        || deviceMatches.has(lane.key);
    }
    if (lane.laneType === 'port' && lane.parent) {
      return portMatches.has(lane.key);
    }
    return false;
  });
}

export function expandKeysWithParents(keys, catalog) {
  if (!keys?.size) return keys;
  const out = new Set(keys);
  for (const k of keys) {
    const parent = catalog?.[k]?.parent;
    if (parent) out.add(parent);
  }
  return out;
}
