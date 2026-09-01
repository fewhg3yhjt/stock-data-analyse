(() => {
  const content = document.getElementById('content');
  const raw = window.MORNING_REPORT_CONTENT;
  if (content && raw) {
    if (window.marked && typeof window.marked.parse === 'function') {
      content.innerHTML = window.sanitizeHtml(window.marked.parse(raw));
    } else {
      content.textContent = raw;
    }
  }

  const button = document.getElementById('generate-report');
  button?.addEventListener('click', async () => {
    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 生成中';
    try {
      const data = await UI_UTILS.fetchJson('/morning-report', {method: 'POST'});
      if (data.status === 'success') location.reload();
      else throw new Error(data.error || '生成失败');
    } catch (error) {
      showUiMessage(error.message || '请求失败', 'error');
      button.disabled = false;
      button.innerHTML = '<i class="fas fa-sync"></i> 重新生成';
    }
  });
})();
