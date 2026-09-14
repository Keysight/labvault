/**
 * Topology JSON import (.labtopo / labvault.lab_topology) — all views.
 */
(function (global) {
  'use strict';

  function initTopoImport(opts) {
    const btn = document.getElementById('lv-import-btn');
    const input = document.getElementById('lv-import-file');
    if (!btn || !input || !opts || !opts.importUrl) return;

    const topoName = opts.topoName || '';
    const csrf = opts.csrfToken || '';
    const onStatus = typeof opts.onStatus === 'function' ? opts.onStatus : function () {};

    btn.addEventListener('click', function () {
      const tname = prompt(
        'This will REPLACE all nodes and links (view layouts are preserved from consolidated .labtopo.v3.json exports). Type the topology name to confirm:\n' + topoName,
      );
      if (tname !== topoName) {
        if (tname !== null) alert('Name did not match. Import cancelled.');
        return;
      }
      input.click();
    });

    input.addEventListener('change', function (e) {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file);
      onStatus('Importing…');
      fetch(opts.importUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrf },
        body: fd,
      })
        .then(function (r) {
          if (r.ok) {
            onStatus('Imported. Reloading…');
            setTimeout(function () { location.reload(); }, 800);
          } else {
            return r.text().then(function (t) {
              onStatus('Import failed: ' + t);
              alert('Import failed: ' + t);
            });
          }
        })
        .catch(function (err) {
          onStatus('Import error: ' + err.message);
          alert('Import error: ' + err.message);
        });
      e.target.value = '';
    });
  }

  global.initTopoImport = initTopoImport;
})(typeof window !== 'undefined' ? window : global);
