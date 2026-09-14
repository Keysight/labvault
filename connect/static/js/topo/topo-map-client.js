/**
 * Poll global Network Topology (/topology/data/) with Page Visibility backoff.
 */
import { linkColorForHealth } from './topo-theme.js';

export class TopoMapGraphClient {
  constructor({ dataUrl, pollIntervalMs = 30000, onUpdate, onError } = {}) {
    this.dataUrl = dataUrl;
    this.pollIntervalMs = pollIntervalMs;
    this.onUpdate = onUpdate;
    this.onError = onError;
    this._timer = null;
    this._last = null;
    this._abort = null;
  }

  async fetch() {
    if (this._abort) this._abort.abort();
    this._abort = new AbortController();
    const res = await fetch(this.dataUrl, {
      credentials: 'same-origin',
      signal: this._abort.signal,
    });
    if (!res.ok) throw new Error(`topology_data HTTP ${res.status}`);
    const data = await res.json();
    this._last = data;
    if (this.onUpdate) this.onUpdate(data);
    return data;
  }

  startPolling() {
    this.stopPolling();
    const tick = async () => {
      if (document.visibilityState !== 'visible') return;
      try {
        await this.fetch();
      } catch (e) {
        if (this.onError) this.onError(e);
      }
    };
    const jitter = Math.floor(Math.random() * 4000);
    this._timer = setInterval(tick, this.pollIntervalMs + jitter);
    tick();
  }

  stopPolling() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  getLast() {
    return this._last;
  }
}

export { linkColorForHealth };
