/**
 * Editable LabTopology.name — POST /lab-topology/<id>/data/ { name }
 */
(function () {
  'use strict';

  function getCsrf() {
    const m = document.cookie.match(/(^|;\s*)csrftoken\s*=\s*([^;]+)/);
    return m ? m.pop() : '';
  }

  function toast(msg, type) {
    const t = document.createElement('div');
    t.className = 'alert alert-' + (type || 'info') + ' position-fixed bottom-0 end-0 m-3 shadow';
    t.style.cssText = 'z-index:10050;max-width:400px;font-size:0.82rem';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 4500);
  }

  function initTopoNameEdit(cfg) {
    const input = document.getElementById('lv-topo-name-input');
    const saveBtn = document.getElementById('lv-topo-name-save');
    if (!input || !cfg || !cfg.topoId || !cfg.dataUrl) return;

    const original = (cfg.initialName || input.value || '').trim();

    function setDirty(dirty) {
      input.classList.toggle('lv-topo-name-dirty', dirty);
      if (saveBtn) saveBtn.classList.toggle('lv-show', dirty);
    }

    function markDirty() {
      setDirty(input.value.trim() !== original);
    }

    input.addEventListener('input', markDirty);

    async function saveName() {
      const name = input.value.trim();
      if (!name) {
        toast('Topology name cannot be empty.', 'warning');
        input.value = original;
        setDirty(false);
        return;
      }
      if (name === original) {
        setDirty(false);
        return;
      }
      if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving…';
      }
      try {
        const r = await fetch(cfg.dataUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrf(),
          },
          body: JSON.stringify({ name: name }),
        });
        const d = await r.json();
        if (!r.ok || d.ok === false) {
          throw new Error(d.error || 'Save failed');
        }
        cfg.initialName = name;
        input.dataset.savedName = name;
        setDirty(false);
        document.title = document.title.replace(/^[^—]+—/, name + ' —');
        const pt = document.querySelector('.page-title');
        if (pt) pt.textContent = name;
        toast('Topology renamed to “' + name + '”.', 'success');
      } catch (e) {
        toast('Rename failed: ' + e.message, 'danger');
        input.value = original;
        setDirty(false);
      } finally {
        if (saveBtn) {
          saveBtn.disabled = false;
          saveBtn.innerHTML = '<i class="fas fa-check me-1"></i>Save name';
        }
      }
    }

    if (saveBtn) {
      saveBtn.addEventListener('click', saveName);
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        saveName();
      }
      if (e.key === 'Escape') {
        input.value = original;
        setDirty(false);
        input.blur();
      }
    });
    input.addEventListener('blur', function () {
      if (input.classList.contains('lv-topo-name-dirty')) {
        saveName();
      }
    });
  }

  window.initTopoNameEdit = initTopoNameEdit;
})();
