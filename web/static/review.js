(() => {
  const button = document.getElementById('add-transaction');
  button?.addEventListener('click', () => UI_UTILS.once('review:add-transaction', async () => {
    const posId = document.getElementById('tx-pos').value;
    const type = document.getElementById('tx-type').value;
    const price = parseFloat(document.getElementById('tx-price').value);
    const shares = parseFloat(document.getElementById('tx-shares').value || 0);
    const date = document.getElementById('tx-date').value || '';
    const reason = document.getElementById('tx-reason').value.trim();
    if (!posId) return showUiMessage('请选择持仓', 'error');
    if (!price || price <= 0) return showUiMessage('请填写价格', 'error');
    button.disabled = true;
    button.textContent = '提交中…';
    const body = new URLSearchParams({type, price, shares: String(shares), date, reason});
    const url = type === 'sell_all' ? `/portfolio/${posId}/close` : `/portfolio/${posId}/transaction`;
    try {
      const data = await UI_UTILS.fetchJson(url, {method: 'POST', body});
      if (data.status === 'success') {
        showUiMessage('已添加交易' + (data.pnl != null ? `，盈亏 ${data.pnl.toFixed(2)}` : ''), 'success');
        setTimeout(() => location.reload(), 600);
      } else {
        showUiMessage(data.error || '添加失败', 'error');
      }
    } catch (error) {
      showUiMessage(error.message || '添加失败', 'error');
    } finally {
      button.disabled = false;
      button.textContent = '添加';
    }
  }).catch(error => showUiMessage(error.message || '添加失败', 'error')));
})();
