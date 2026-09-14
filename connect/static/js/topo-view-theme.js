/**
 * Shared topology canvas theme (Classic dark / Grey / Light).
 * Uses the same localStorage key as Port Fabric: pf_state_{topoId}_theme
 */
(function (global) {
  'use strict';

  function storageKey(topoId) {
    return 'pf_state_' + topoId;
  }

  function readRaw(topoId, key, def) {
    try {
      var v = localStorage.getItem(storageKey(topoId) + '_' + key);
      return v !== null ? JSON.parse(v) : def;
    } catch (e) {
      return def;
    }
  }

  function writeRaw(topoId, key, val) {
    try {
      localStorage.setItem(storageKey(topoId) + '_' + key, JSON.stringify(val));
    } catch (e) { /* ignore */ }
  }

  function normalizeTheme(theme) {
    return theme === 'grey' || theme === 'light' ? theme : 'dark';
  }

  function readTheme(topoId) {
    return normalizeTheme(readRaw(topoId, 'theme', 'dark'));
  }

  function cssVar(name, fallback) {
    var el = document.body;
    if (!el) return fallback;
    var v = getComputedStyle(el).getPropertyValue(name).trim();
    return v || fallback;
  }

  function applyTheme(theme, opts) {
    opts = opts || {};
    theme = normalizeTheme(theme);

    document.body.classList.remove('pf-theme-grey', 'pf-theme-light');
    if (theme === 'grey') document.body.classList.add('pf-theme-grey');
    if (theme === 'light') document.body.classList.add('pf-theme-light');
    document.documentElement.setAttribute('data-topo-theme', theme);

    document.querySelectorAll('.pf-theme-btn').forEach(function (b) {
      b.classList.toggle('pf-theme-active', b.dataset.theme === theme);
    });

    var wrap = document.getElementById('lt-canvas-wrap');
    if (wrap) {
      var bg = cssVar('--lt-canvas-bg', '');
      if (bg) wrap.style.background = bg;
    }

    applyPageSurfaces(theme);

    if (typeof opts.onApply === 'function') opts.onApply(theme);
    global.dispatchEvent(new CustomEvent('topo-theme-change', { detail: { theme: theme } }));
    return theme;
  }

  function applyPageSurfaces(theme) {
    var pfCanvas = document.getElementById('pf-canvas');
    if (pfCanvas) {
      var pfBg = cssVar('--pf-bg', '');
      if (pfBg) pfCanvas.style.background = pfBg;
    }
    var fmCol = document.getElementById('fm-canvas-col');
    if (fmCol) {
      var fmBg = theme === 'light' ? '#faf9f5' : (theme === 'grey' ? '#1a2030' : '#050d1a');
      fmCol.style.background = fmBg;
    }
  }

  function init(topoId, opts) {
    opts = opts || {};
    var theme = readTheme(topoId);
    applyTheme(theme, opts);

    document.querySelectorAll('.pf-theme-btn').forEach(function (btn) {
      if (btn._topoThemeBound) return;
      btn._topoThemeBound = true;
      btn.addEventListener('click', function () {
        var t = normalizeTheme(this.dataset.theme || 'dark');
        writeRaw(topoId, 'theme', t);
        applyTheme(t, opts);
      });
    });

    return theme;
  }

  global.TopoViewTheme = {
    init: init,
    apply: applyTheme,
    read: readTheme,
    write: function (topoId, theme) {
      writeRaw(topoId, 'theme', normalizeTheme(theme));
    },
    cssVar: cssVar,
    linkLabelHalo: function () {
      return cssVar('--lt-link-label-halo', '#0a0a1a');
    },
    linkSublabelFill: function () {
      return cssVar('--lt-link-sublabel', '#6e7681');
    },
  };
})(window);
