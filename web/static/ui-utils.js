/* Shared escaping and small formatting helpers for page adapters. */
(() => {
  window.UI_UTILS = {
    esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    },
    number(value, digits = 2) {
      if (value === null || value === undefined || value === '') return '—';
      const number = Number(value);
      return Number.isFinite(number) ? number.toFixed(digits) : String(value);
    }
  };
})();
