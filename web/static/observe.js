(() => {
  async function addWatch(code, name) {
    const data = await UI_UTILS.fetchJson('/watchlist', {method: 'POST', body: new URLSearchParams({code, name})});
    data.status === 'success' ? showUiMessage('已加入自选', 'success') : showUiMessage(data.error || '失败', 'error');
  }
  async function simulate(code, name) {
    showUiMessage('模拟中，请稍候…', 'info');
    const data = await UI_UTILS.fetchJson('/api/simulation', {method: 'POST', body: new URLSearchParams({code, name})});
    if (data.status === 'success') {
      const snapshot = data.snapshot;
      showUiMessage(`模拟完成：止损${snapshot.hard_stop} 止盈预警${snapshot.left_side_zone ? snapshot.left_side_zone[1] : '-'}`, 'success');
    } else showUiMessage(data.error || '模拟失败', 'error');
  }
  document.addEventListener('click', event => {
    const actionButton = event.target.closest('[data-action]');
    if (actionButton) {
      const row = actionButton.closest('.obs-row');
      if (!row) return;
      UI_UTILS.once(`observe:${actionButton.dataset.action}:${row.dataset.code}`, () => {
        const task = actionButton.dataset.action === 'simulate' ? simulate : addWatch;
        return task(row.dataset.code, row.dataset.name);
      }).catch(error => showUiMessage(error.message || '操作失败', 'error'));
      return;
    }
    const row = event.target.closest('.obs-row');
    if (!row) return;
    const detailRow = document.getElementById(`detail-${row.dataset.code}`);
    if (!detailRow) return;
    if (!detailRow.classList.toggle('is-hidden')) {
      const box = detailRow.querySelector('.stock-detail-box');
      box.textContent = '加载中…';
      window.StockDetail.load(row.dataset.code, 'watch', box).catch(error => {
        box.innerHTML = `<span class="stock-detail-error">${UI_UTILS.esc(error.message)}</span>`;
      });
    }
  });
})();
