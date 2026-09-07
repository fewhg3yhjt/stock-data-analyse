/* Shared escaping and small formatting helpers for page adapters. */
(() => {
  // All pages load this file before page scripts. Patch the native helper once
  // so legacy direct response.json() calls cannot expose an opaque parse error.
  if (window.Response && !window.Response.prototype.__stockSafeJson) {
    const nativeJson = window.Response.prototype.json;
    window.Response.prototype.json = async function safeJson() {
      const text = await this.text();
      if (!text.trim()) throw new Error(`接口返回空响应（HTTP ${this.status}）`);
      try { return JSON.parse(text); }
      catch (_) { throw new Error(`接口返回非 JSON 响应（HTTP ${this.status}）`); }
    };
    window.Response.prototype.__stockSafeJson = nativeJson;
  }
  window.UI_UTILS = {
    _busy: new Set(),
    async once(key, task) {
      if (window.UI_UTILS._busy.has(key)) return null;
      window.UI_UTILS._busy.add(key);
      try { return await task(); }
      finally { window.UI_UTILS._busy.delete(key); }
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
        const text = await response.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; }
        catch (parseError) { throw new Error(`接口返回了非 JSON 响应（HTTP ${response.status}）${text ? `：${text.slice(0,120)}` : ''}`); }
        if (!response.ok) throw new Error(data?.error || `请求失败（${response.status}）`);
        if (!data) throw new Error(`接口返回空响应（HTTP ${response.status}）`);
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
