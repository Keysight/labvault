/**
 * Usage page theme presets — inspired by getdesign.md (IBM Carbon, NVIDIA, Kraken, Linear, PostHog).
 */
const STORAGE_KEY = 'labvault-usage-theme';

export const USAGE_THEMES = [
  { id: 'carbon', label: 'Carbon', source: 'IBM' },
  { id: 'nvidia', label: 'NVIDIA', source: 'NVIDIA' },
  { id: 'kraken', label: 'Kraken', source: 'Kraken' },
  { id: 'linear', label: 'Linear', source: 'Linear' },
  { id: 'posthog', label: 'PostHog', source: 'PostHog' },
];

const VALID_IDS = new Set(USAGE_THEMES.map((t) => t.id));

export function normalizeUsageTheme(theme) {
  return VALID_IDS.has(theme) ? theme : 'carbon';
}

export function readUsageTheme() {
  try {
    return normalizeUsageTheme(localStorage.getItem(STORAGE_KEY) || 'carbon');
  } catch (_) {
    return 'carbon';
  }
}

export function writeUsageTheme(theme) {
  try {
    localStorage.setItem(STORAGE_KEY, normalizeUsageTheme(theme));
  } catch (_) { /* ignore */ }
}

export function applyUsageTheme(theme) {
  const id = normalizeUsageTheme(theme);
  const root = document.body;
  if (root && root.classList.contains('lv-topology-page')) {
    root.setAttribute('data-usage-theme', id);
  }
  document.querySelectorAll('.ug-theme-btn').forEach((btn) => {
    btn.classList.toggle('on', btn.dataset.usageTheme === id);
  });
  window.dispatchEvent(new CustomEvent('usage-theme-change', { detail: { theme: id } }));
  return id;
}

export function mountUsageThemePicker(host, { onChange } = {}) {
  if (!host) return readUsageTheme();
  if (!host.querySelector('.ug-theme-btn')) {
    host.innerHTML = USAGE_THEMES.map(
      (t) => `<button type="button" class="ug-theme-btn" data-usage-theme="${t.id}" title="${t.source} — getdesign.md">${t.label}</button>`,
    ).join('');
  }
  const current = applyUsageTheme(readUsageTheme());
  host.querySelectorAll('.ug-theme-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const next = normalizeUsageTheme(btn.dataset.usageTheme);
      writeUsageTheme(next);
      applyUsageTheme(next);
      if (typeof onChange === 'function') onChange(next);
    });
  });
  return current;
}
