const stageLabels = {observe: '候选', simulate: '观察中', buy: '已模拟', review: '已持仓', none: '候选'};
const {esc, fetchJson} = window.UI_UTILS;
let poolItems = {};
let currentItem = null;
let dataHealth = {};

function render(items) {
  poolItems = {};
  items.forEach(item => { poolItems[item.code] = item; });
  const blocked = ['critical', 'empty', 'failed'].includes(dataHealth.status);
  document.getElementById('pool-rows').innerHTML = items.length ? items.map(item => {
    let action = '';
    if (item.next_action === 'observe') {
      action = '<button class="ui-button ui-button-primary ui-button-sm observe-btn" data-action="observe">加入观察</button>';
    } else if (item.next_action === 'simulate') {
      action = `<button class="ui-button ui-button-primary ui-button-sm simulate-btn" data-action="simulate" ${blocked ? 'disabled title="日线数据严重滞后"' : ''}>选择策略模拟</button>`;
    } else if (item.next_action === 'buy') {
      action = `<a class="ui-button ui-button-secondary ui-button-sm" href="/simulation">查看模拟详情</a> ${blocked ? '<span class="tag red">数据滞后，禁止建仓</span>' : `<a class="ui-button ui-button-primary ui-button-sm" href="/portfolio/add?code=${encodeURIComponent(item.code)}&name=${encodeURIComponent(item.name)}&scheme=${encodeURIComponent(item.simulation.scheme_name)}">确认建仓</a>`}`;
    } else if (item.next_action === 'review') {
      action = '<a class="ui-button ui-button-primary ui-button-sm" href="/dashboard/warroom">管理持仓</a>';
    }
    return `<tr class="pool-row" data-code="${esc(item.code)}"><td><b>${esc(item.name)}</b><br><span class="note">${esc(item.code)}</span><br><button class="ui-button ui-button-secondary ui-button-sm detail-toggle" data-action="detail">展开K线</button></td><td><span class="tag blue">${stageLabels[item.next_action] || item.next_action}</span></td><td>${(item.sources || []).map(source => `<span class="tag gray">${esc({manual: '手动', strategy: '策略', holding: '持仓'}[source] || source)}</span>`).join(' ')}<br><span class="note">${esc(item.watch && item.watch.notes || item.observation && item.observation.instruction || '暂无原因')}</span></td><td>${item.simulation ? `方案 <b>${esc(item.simulation.scheme_name)}</b><br>模拟价 ${esc(item.simulation.current_price || '—')}<br>止损 ${esc(item.simulation.hard_stop || '—')}` : '尚未模拟'}</td><td>${item.holding ? `${esc(item.holding.total_shares)} 股<br>盈亏 ${esc(item.holding.unrealized_pnl_pct)}%` : '—'}</td><td>${action || '—'}</td></tr><tr data-detail-for="${esc(item.code)}"><td colspan="6"><div class="pool-detail"><div class="pool-detail-box">加载中...</div></div></td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">观察池暂无记录</td></tr>';
}

function toggleDetail(code) {
  const item = poolItems[code];
  const detail = document.querySelector(`[data-detail-for="${CSS.escape(code)}"] .pool-detail`);
  detail.classList.toggle('open');
  if (!detail.classList.contains('open') || detail.dataset.loaded) return;
  detail.dataset.loaded = '1';
  window.StockDetail.load(code, item.holding ? 'position' : 'watch', detail.querySelector('.pool-detail-box'), {})
    .catch(error => { detail.querySelector('.pool-detail-box').innerHTML = `<div class="error">${esc(error.message)}</div>`; });
}

async function addObservation(code) {
  const item = poolItems[code];
  const reason = await showUiInput({title: '加入观察池', message: '记录加入观察的原因。', label: '观察原因', value: item.observation && item.observation.instruction || '策略候选'});
  if (reason === null) return;
  const data = await fetchJson('/watchlist', {method: 'POST', body: new URLSearchParams({code: item.code, name: item.name, reason})});
  if (data.status === 'success') loadPool();
  else showUiMessage(data.error || '加入观察失败', 'error');
}

async function openSimulation(code) {
  currentItem = poolItems[code];
  document.getElementById('sim-title').textContent = currentItem.name + ' · 策略模拟';
  document.getElementById('sim-result').style.display = 'none';
  document.getElementById('sim-form').style.display = 'block';
  const data = await fetchJson('/api/schemes/list');
  const schemes = (data.schemes || []).filter(scheme => scheme.enabled !== false && (scheme.is_builtin || scheme.state === 'published'));
  document.getElementById('sim-scheme').innerHTML = schemes.map(scheme => `<option value="${esc(scheme.name)}">${esc(scheme.name)} · v${esc(scheme.version)}${scheme.state ? ' · ' + esc(scheme.state) : ''}</option>`).join('');
  document.getElementById('sim-health').textContent = `日线数据：${dataHealth.latest_value || '暂无'}，状态 ${dataHealth.status}`;
  document.getElementById('simulation-modal').style.display = 'flex';
}

function closeModal() { document.getElementById('simulation-modal').style.display = 'none'; }

async function runSimulation() {
  const scheme = document.getElementById('sim-scheme').value;
  if (!scheme) return showUiMessage('请选择策略', 'error');
  const button = document.getElementById('sim-run');
  button.disabled = true;
  button.textContent = '模拟计算中...';
  try {
    const data = await fetchJson('/api/simulation', {method: 'POST', body: new URLSearchParams({code: currentItem.code, name: currentItem.name, scheme})});
    if (data.status !== 'success') return showUiMessage(data.error || '模拟失败', 'error');
    const snapshot = data.snapshot || {};
    document.getElementById('sim-form').style.display = 'none';
    const result = document.getElementById('sim-result');
    result.style.display = 'block';
    result.innerHTML = `<div class="notice">模拟已完成，方案 ${esc(scheme)}</div><div class="result-grid"><div>当前价格<br><b>${esc(snapshot.current_price || '—')}</b></div><div>市场状态<br><b>${esc(snapshot.market_state || '—')}</b></div><div>硬止损<br><b>${esc(snapshot.hard_stop || '—')}</b></div><div>弱/强支撑<br><b>${esc(snapshot.weak_support || '—')} / ${esc(snapshot.strong_support || '—')}</b></div><div>年内高点<br><b>${esc(snapshot.year_high || '—')}</b></div><div>分析时间<br><b>${esc(snapshot.analyzed_at || '—')}</b></div></div><div class="simulation-actions"><a class="ui-button ui-button-primary" href="/simulation">进入模拟详情页</a> <button class="ui-button ui-button-secondary" data-action="back-to-pool">返回观察池</button></div>`;
  } catch (error) {
    showUiMessage(error.message || '模拟失败', 'error');
  } finally {
    button.disabled = false;
    button.textContent = '运行策略模拟';
  }
}

async function loadPool(refresh = false) {
  try {
    const data = await fetchJson('/api/watch-pool' + (refresh ? '?refresh=1' : ''));
    if (data.status !== 'success') {
      document.getElementById('pool-rows').innerHTML = `<tr><td colspan="6" class="error">${esc(data.error)}</td></tr>`;
      return;
    }
    dataHealth = data.data_health || {};
    const health = document.getElementById('health-note');
    const blocked = ['critical', 'empty', 'failed'].includes(dataHealth.status);
    health.innerHTML = `日线数据截至 <b>${esc(dataHealth.latest_value || '暂无')}</b>，预期交易日 <b>${esc(dataHealth.expected_trade_day)}</b>，状态 <b>${esc(dataHealth.status)}</b>。${blocked ? '数据严重滞后，策略模拟和建仓已暂停，请先到数据中心同步。' : '可继续观察和策略模拟。'} <a href="/data-center">查看数据任务</a>`;
    health.className = blocked ? 'error health-note' : 'notice health-note';
    render(data.items || []);
  } catch (error) {
    document.getElementById('pool-rows').innerHTML = `<tr><td colspan="6" class="error">${esc(error.message)}</td></tr>`;
  }
}

document.addEventListener('click', event => {
  const element = event.target.closest('[data-action]');
  if (!element) return;
  const code = element.closest('[data-code]')?.dataset.code;
  if (element.dataset.action === 'refresh') loadPool(true);
  else if (element.dataset.action === 'detail') { event.stopPropagation(); toggleDetail(code); }
  else if (element.dataset.action === 'observe') { event.stopPropagation(); addObservation(code); }
  else if (element.dataset.action === 'simulate') { event.stopPropagation(); openSimulation(code); }
  else if (element.dataset.action === 'close-modal') closeModal();
  else if (element.dataset.action === 'run-simulation') runSimulation();
  else if (element.dataset.action === 'back-to-pool') { closeModal(); loadPool(); }
});

loadPool();
