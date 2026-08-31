(() => {
  const dataStatusText = new Proxy({}, {get: (_, key) => window.UI_STATUS_LABEL?.(key) || key || '状态未知'});
  const actionLabels = {simulate:'待模拟', buy:'待建仓', review:'已持仓', none:'候选'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const json = async url => { const response = await fetch(url); const data = await response.json(); if (!response.ok || data.status !== 'success') throw new Error(data.error || '请求失败'); return data; };
  const statusTag = status => `<span class="wb-state ${esc(status || 'unknown')}">${esc(dataStatusText[status] || status || '状态未知')}</span>`;
  const setText = (id, value) => { const element = document.getElementById(id); if (element) element.textContent = value; };

  function renderHealth(health) {
    const datasets = health?.datasets || [];
    const names = {daily:'日线数据', indicators:'技术指标', factors:'研究因子', minute:'分钟数据', online:'在线快照'};
    setText('health-summary', `预计交易日 ${health?.expected_trade_day || '暂无'} · ${datasets.length} 项数据状态已检查`);
    document.getElementById('health-list').innerHTML = datasets.slice(0, 6).map(item => `<div class="wb-health-row"><span><strong>${esc(names[item.dataset] || item.dataset)}</strong><span class="wb-muted"> · ${esc(item.latest_value || '暂无实际日期')}</span></span>${statusTag(item.status)}</div>`).join('') || '<div class="wb-empty">暂无数据状态</div>';
    const overall = health?.overall_status || 'unknown';
    document.getElementById('overall-state').innerHTML = statusTag(overall);
    setText('summary-line', overall === 'healthy' ? '核心数据状态正常，可以继续进行今天的研究和跟踪。' : '数据链路存在需要关注的事项，建议先查看异常原因再继续研究。');
    setText('data-metric', dataStatusText[overall] || '状态未知');
  }

  function renderPositions(items) {
    setText('position-metric', items.length);
    document.getElementById('position-list').innerHTML = items.slice(0, 6).map(item => `<div class="wb-item"><span><strong>${esc(item.stock_name || item.name || item.stock_code)}</strong><span>${esc(item.total_shares || 0)} 股 · 浮动盈亏 ${esc(item.unrealized_pnl_pct ?? '—')}%</span></span><a class="wb-btn" href="/dashboard/warroom">查看</a></div>`).join('') || '<div class="wb-empty">当前没有持仓</div>';
  }

  function renderPool(items) {
    setText('pool-metric', items.length);
    document.getElementById('pool-list').innerHTML = items.slice(0, 6).map(item => `<div class="wb-item"><span><strong>${esc(item.name || item.code)}</strong><span>${esc((item.sources || []).join('、') || '观察池')} · ${esc(actionLabels[item.next_action] || item.next_action || '候选')}</span></span><a class="wb-btn" href="/watch-pool">查看</a></div>`).join('') || '<div class="wb-empty">当前没有观察对象</div>';
  }

  function renderNotifications(data) {
    const counts = data.counts || {};
    setText('notify-metric', counts.pending || 0);
    document.getElementById('notify-list').innerHTML = `<div class="wb-health-row"><strong>待投递</strong><span>${esc(counts.pending || 0)}</span></div><div class="wb-health-row"><strong>已发送</strong><span>${esc(counts.sent || 0)}</span></div><div class="wb-health-row"><strong>多次失败</strong><span class="${counts.dead ? 'wb-state critical' : ''}">${esc(counts.dead || 0)}</span></div>`;
    const alerts = [];
    if (counts.pending) alerts.push(['通知', `有 ${counts.pending} 条通知待投递`, '/system']);
    if (counts.dead) alerts.push(['告警', `有 ${counts.dead} 条通知多次投递失败`, '/system']);
    return alerts;
  }

  function renderTodos(summary, notificationAlerts) {
    const todos = [];
    const health = summary.dataset_health || {};
    if (!['healthy', 'unknown'].includes(health.overall_status)) todos.push(['高', '数据中心', '数据状态需要关注', health.datasets?.find(x => x.status !== 'healthy')?.last_error || '请查看数据中心', '/data-center']);
    (summary.actionable_positions || []).slice(0, 5).forEach(item => todos.push(['高', '持仓', `${item.stock_name || item.stock_code} 有待处理建议`, item.advice, '/dashboard/warroom']));
    (summary.watchlist_changes || []).slice(0, 5).forEach(item => todos.push(['中', '观察池', `${item.name || item.code} 需要跟踪`, actionLabels[item.next_action] || '查看观察池', '/watch-pool']));
    notificationAlerts.forEach(item => todos.push(['中', item[0], item[1], '进入通知中心处理', item[2]]));
    const body = document.getElementById('todo-body');
    body.innerHTML = todos.length ? `<table class="wb-table"><thead><tr><th>优先级</th><th>来源</th><th>事项</th><th>原因</th><th>下一步</th></tr></thead><tbody>${todos.map(item => `<tr><td>${esc(item[0])}</td><td>${esc(item[1])}</td><td><strong>${esc(item[2])}</strong></td><td class="wb-muted">${esc(item[3] || '—')}</td><td><a class="wb-btn" href="${esc(item[4])}">查看</a></td></tr>`).join('')}</tbody></table>` : '<div class="wb-empty">当前没有需要优先处理的事项，可以继续推进市场发现或研究。</div>';
    setText('todo-count', `${todos.length} 项`);
  }

  async function load() {
    try {
      const [summary, positions, notifications, pool] = await Promise.all([json('/api/workbench/summary'), json('/api/workbench/positions'), json('/api/workbench/notifications'), json('/api/watch-pool')]);
      renderHealth(summary.dataset_health || {}); renderPositions(positions.positions || []); renderPool(pool.items || []); const alerts = renderNotifications(notifications); renderTodos(summary, alerts);
      setText('refresh-time', new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'}));
    } catch (error) { document.getElementById('todo-body').innerHTML = `<div class="wb-alert">工作台读取失败：${esc(error.message)}，请刷新重试。</div>`; }
  }
  window.loadWorkbench = load; load(); setInterval(load, 30000);
})();
