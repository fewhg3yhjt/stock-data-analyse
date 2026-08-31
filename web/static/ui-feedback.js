/* Shared in-page feedback. Keep legacy alert callers usable without browser popups. */
(() => {
  window.showUiMessage = (message, type = '') => {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message || '';
    toast.dataset.type = type;
    toast.style.display = 'block';
    clearTimeout(window.__uiToastTimer);
    window.__uiToastTimer = setTimeout(() => { toast.style.display = 'none'; }, 2800);
  };
  window.alert = message => window.showUiMessage(message, 'error');
})();
