/* Shared escaping and small formatting helpers for page adapters. */
(() => {
  window.UI_UTILS = {
    _busy: new Set(),
    async once(key, task) {
      if (this._busy.has(key)) return null;
      this._busy.add(key);
      try { return await task(); }
      finally { this._busy.delete(key); }
    },
    esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    },
    number(value, digits = 2) {
      if (value === null || value === undefined || value === '') return '—';
      const number = Number(value);
      return Number.isFinite(number) ? number.toFixed(digits) : String(value);
    },
    async fetchJson(url, options = {}, timeoutMs = 30000) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, {...options, signal: controller.signal});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
        return data;
      } catch (error) {
        if (error.name === 'AbortError') {
          const unknown = new Error('请求超时，结果未知');
          unknown.resultUnknown = true;
          throw unknown;
        }
        throw error;
      } finally {
        clearTimeout(timer);
      }
    }
  };
})();
