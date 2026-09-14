/**
 * Per-topology np_timeseries collection pause/resume.
 */
(function () {
  'use strict';

  function getCsrf(cfg) {
    if (cfg && cfg.csrfToken) return cfg.csrfToken;
    const m = document.cookie.match(/(^|;\s*)csrftoken\s*=\s*([^;]+)/);
    return m ? decodeURIComponent(m.pop()) : '';
  }

  function toast(msg, type) {
    const t = document.createElement('div');
    t.className = 'alert alert-' + (type || 'info') + ' position-fixed bottom-0 end-0 m-3 shadow';
    t.style.cssText = 'z-index:10050;max-width:420px;font-size:0.82rem';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 3800);
  }

  function parseEnabled(val) {
    return val === true || val === 1 || val === '1' || val === 'true';
  }

  function initMetricsCollectionToggle(cfg) {
    const btn = document.getElementById('lv-metrics-collect-btn');
    const lbl = document.getElementById('lv-metrics-collect-lbl');
    const icon = document.getElementById('lv-metrics-collect-icon');
    if (!btn || !cfg || !cfg.toggleUrl) return;

    let enabled = parseEnabled(btn.dataset.enabled);
    let saving = false;

    function setUi(on, { busy } = {}) {
      enabled = !!on;
      btn.dataset.enabled = enabled ? '1' : '0';
      btn.dataset.active = enabled ? '1' : '0';
      btn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
      if (lbl) lbl.textContent = enabled ? 'Collecting' : 'Paused';
      if (icon) {
        icon.className = busy
          ? 'fas fa-spinner fa-spin'
          : (enabled ? 'fas fa-chart-line' : 'fas fa-pause-circle');
      }
      btn.title = enabled
        ? 'Traffic and status metrics are being stored. Click to pause collection and save disk.'
        : 'Collection paused — usage charts show historical data only. Click to resume.';
      btn.classList.toggle('lv-tb-busy', !!busy);
      document.body.classList.toggle('lv-metrics-paused', !enabled);
    }

    setUi(enabled);

    btn.addEventListener('click', async function () {
      if (saving) return;
      const prev = enabled;
      const next = !prev;
      saving = true;
      setUi(next, { busy: true });
      try {
        const r = await fetch(cfg.toggleUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrf(cfg),
          },
          body: JSON.stringify({ enabled: next }),
        });
        let data = {};
        const ct = (r.headers.get('content-type') || '').toLowerCase();
        if (ct.includes('application/json')) {
          data = await r.json();
        }
        if (!r.ok || data.ok === false) {
          throw new Error(data.error || ('HTTP ' + r.status));
        }
        setUi(!!data.metrics_collection_enabled);
        toast(
          data.metrics_collection_enabled
            ? 'Metrics collection resumed for this topology.'
            : 'Metrics collection paused — new samples will not be stored.',
          data.metrics_collection_enabled ? 'success' : 'warning',
        );
      } catch (e) {
        setUi(prev);
        toast('Could not update metrics collection: ' + e.message, 'danger');
      } finally {
        saving = false;
        setUi(enabled);
      }
    });
  }

  window.initMetricsCollectionToggle = initMetricsCollectionToggle;
})();
