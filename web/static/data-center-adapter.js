(() => {
  const state = {assets: [], attention: [], pipeline: [], tasks: [], operation: null};
  const taskLabel = key => window.UI_STATUS_LABEL?.(key, 'run') || ({scheduled: '已排期', disabled: '未启用'}[key] || key);
  const typeLabels = {stock: '股票', etf: 'ETF', index: '指数'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const json = async (url, options) => { const response = await fetch(url, options); const data = await response.json(); if (!response.ok || data.status !== 'success') throw new Error(data.error || '请求失败'); return data; };
  const statusLabels = window.UI_STATUS ? Object.fromEntries(Object.keys(window.UI_STATUS).map(k => [k, window.UI_STATUS[k].label])) : {healthy:'正常', partial:'部分可用', stale:'待更新', critical:'异常', failed:'失败', unknown:'状态未知'};
  const statusCls = window.UI_STATUS_CLS || (key => key || 'unknown');
  const health = value => { const key = value || 'unknown'; return `<span class="dm-health ${esc(statusCls(key))}">${esc(statusLabels[key] || key)}</span>`; };
  const taskStatus = task => task.running_run?.status || task.latest_run?.status || (task.enabled ? 'scheduled' : 'disabled');

  // Keep page rendering independent from the two existing API response shapes.
  function adaptOverview(data) {
    const counts = data.counts || {};
    const task = data.task_summary || {};
    return {
      assets: data.core_assets || [],
      attention: data.attention || [],
      pipeline: data.pipeline || [],
      kpis: [
        ['数据项', counts.total || 0, '数据集和指标定义'],
        ['正常', counts.healthy || 0, '健康状态正常'],
        ['待关注', (counts.attention || 0) + (counts.critical || 0), '待更新、部分可用或异常'],
        ['运行中任务', task.running || 0, '当前执行实例'],
        ['今日完成', task.today_completed || 0, '今日执行实例'],
      ],
    };
  }

  function renderKpis(items) {
    document.getElementById('asset-kpis').innerHTML = items.map(item => `<div class="dm-kpi"><div class="dm-kpi-label">${esc(item[0])}</div><div class="dm-kpi-value">${esc(item[1])}</div><div class="dm-kpi-note">${esc(item[2])}</div></div>`).join('');
  }

  function renderAssets() {
    const target = document.getElementById('asset-rows');
    target.innerHTML = state.assets.map(asset => {
      const coverage = asset.coverage || {};
      const ratio = coverage.ratio;
      const covered = coverage.covered == null ? '暂无覆盖数据' : `${coverage.covered} / ${coverage.expected ?? '—'}`;
      return `<tr data-key="${esc(asset.asset_key)}"><td><div class="dm-name">${esc(asset.display_name)}</div><div class="dm-code">${esc(asset.asset_key)}</div></td><td>${esc(asset.category || '数据集')}</td><td>${esc(asset.latest_actual_date || '暂无')}</td><td class="dm-cover">${esc(covered)}${ratio != null ? `<div class="dm-cover-bar"><i style="width:${Math.min(100, Math.max(0, ratio * 100))}%"></i></div>` : ''}</td><td>${health(asset.health?.status)}</td><td class="dm-muted">${esc(asset.producer_task || '—')}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="dm-empty">暂无重点数据项</td></tr>';
    target.querySelectorAll('tr[data-key]').forEach(row => row.onclick = () => openAsset(row.dataset.key));
  }

  function renderAttention() {
    const target = document.getElementById('attention-list');
    target.innerHTML = state.attention.map(asset => `<button class="dm-artifact dm-artifact-button" data-key="${esc(asset.asset_key)}"><span><strong>${esc(asset.display_name)}</strong><span>最新实际日期：${esc(asset.latest_actual_date || '暂无')}</span><span>${esc(asset.health?.message || '需要检查数据状态')}</span></span>${health(asset.health?.status)}</button>`).join('') || '<div class="dm-detail-empty">当前没有需要关注的数据项</div>';
    target.querySelectorAll('[data-key]').forEach(item => item.onclick = () => openAsset(item.dataset.key));
  }

  function renderPipeline() {
    document.getElementById('pipeline-flow').innerHTML = state.pipeline.map(task => {
      const run = task.running_run || task.latest_run;
      const status = task.current_status || taskStatus(task);
      const done = status === 'success' || status === 'partial_success';
      const range = run?.actual_period_start && run?.actual_period_end ? `${run.actual_period_start} ~ ${run.actual_period_end}` : '';
      return `<div class="dm-flow-step ${done ? 'done' : status === 'running' ? 'running' : ''}"><div class="dm-flow-dot">${done ? '✓' : status === 'running' ? '…' : '○'}</div><div class="dm-flow-title">${esc(task.display_name)}</div><div class="dm-flow-sub">${esc(taskLabel(status))}${range ? `<br>${esc(range)}` : ''}</div></div>`;
    }).join('') || '<div class="dm-empty">暂无生产链路任务</div>';
  }

  function renderDetail(asset) {
    const coverage = asset.coverage || {};
    const byType = coverage.by_asset_type || {};
    const typeCards = ['stock', 'etf', 'index'].map(type => {
      const value = byType[type];
      const applicable = asset.applicability?.[type];
      const text = applicable === 'not_applicable' ? '不适用' : value == null ? '暂无覆盖数据' : typeof value === 'object' ? `${value.covered ?? 0} / ${value.expected ?? '—'}` : value;
      return `<div class="dm-type ${text === '不适用' ? 'na' : ''}"><span>${typeLabels[type]}</span><b>${esc(text)}</b></div>`;
    }).join('');
    const sources = (asset.sources || []).map(item => `<span class="dm-chip">${esc(item.source_name || item.name || '未知来源')}</span>`).join('') || '<span class="dm-muted">暂无来源记录</span>';
    const consumers = (asset.consumers || []).map(item => `<span class="dm-chip">${esc(item.consumer_name || item.consumer || '未知消费者')}</span>`).join('') || '<span class="dm-muted">暂无消费记录</span>';
    const impact = (asset.business_impact || []).map(item => `<li>${esc(item.consumer)}：${esc(item.blocked_actions || item.purpose || '')}</li>`).join('') || '<li>暂无业务影响记录</li>';
    const versions = (asset.versions || []).slice(0, 5).map(item => `<li>${esc(item.partition_key)} · ${esc(item.version_id)} · ${esc(item.quality_status || '暂无质量状态')}</li>`).join('') || '<li>暂无版本记录</li>';
    document.getElementById('drawer-title').textContent = asset.display_name || asset.asset_key;
    document.getElementById('drawer-sub').textContent = `${asset.category || '数据项'} · ${asset.asset_key}`;
    document.getElementById('asset-detail').innerHTML = `<div class="dm-detail"><section class="dm-detail-section"><h3>定义与统一口径</h3><dl class="dm-kv"><dt>定义</dt><dd>${esc(asset.definition || '暂无')}</dd><dt>单位</dt><dd>${esc(asset.unit || '—')}</dd><dt>关联任务</dt><dd>${esc(asset.producer_task || '—')}</dd></dl></section><section class="dm-detail-section"><h3>健康情况</h3><dl class="dm-kv"><dt>状态</dt><dd>${health(asset.health?.status)}</dd><dt>最新实际日期</dt><dd>${esc(asset.latest_actual_date || '暂无')}</dd><dt>预期日期</dt><dd>${esc(asset.expected_date || '暂无')}</dd><dt>覆盖</dt><dd>${coverage.covered == null ? '暂无覆盖数据' : `${coverage.covered} / ${coverage.expected ?? '—'} (${((coverage.ratio || 0) * 100).toFixed(1)}%)`}</dd><dt>说明</dt><dd>${esc(asset.health?.message || '—')}</dd></dl></section><section class="dm-detail-section"><h3>证券类型覆盖</h3><div class="dm-type-grid">${typeCards}</div></section><section class="dm-detail-section"><h3>来源与消费</h3><div class="dm-kv"><dt>数据来源</dt><dd>${sources}</dd><dt>消费模块</dt><dd>${consumers}</dd></div></section><section class="dm-detail-section"><h3>业务影响</h3><ul>${impact}</ul></section><section class="dm-detail-section"><h3>最近版本</h3><ul>${versions}</ul></section><section class="dm-detail-section"><h3>当前结果预览</h3><p class="dm-muted">当前接口未提供安全的只读样例预览。</p></section></div>`;
    document.getElementById('asset-mask').classList.add('open');
  }

  async function openAsset(key) { try { const data = await json(`/api/data-center/assets/${encodeURIComponent(key)}`); renderDetail(data.asset || {}); } catch (error) { showOperation(`读取数据项失败：${error.message}`, 'error'); } }

  function showOperation(message, type = '') {
    const target = document.getElementById('data-operation');
    if (!target) return;
    target.className = `dm-operation ${type}`;
    target.textContent = message;
    target.hidden = false;
  }

  async function startOperation(label, url) {
    const buttons = document.querySelectorAll('[data-operation-url]');
    buttons.forEach(button => { button.disabled = true; });
    showOperation(`${label}已提交，正在读取运行状态…`);
    try {
      const data = await json(url, {method: 'POST'});
      state.operation = {label, runId: data.run_id || null};
      showOperation(data.run_id ? `${label}已提交，运行记录 #${data.run_id}` : `${label}已提交，后台正在处理。` , 'ok');
      await loadOverview();
    } catch (error) {
      showOperation(`${label}失败：${error.message}`, 'error');
    } finally { buttons.forEach(button => { button.disabled = false; }); }
  }

  async function loadOverview() {
    try {
      const data = await json('/api/data-center/overview');
      const adapted = adaptOverview(data);
      state.assets = adapted.assets; state.attention = adapted.attention; state.pipeline = adapted.pipeline;
      renderKpis(adapted.kpis); renderAssets(); renderAttention(); renderPipeline();
      document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
    } catch (error) { document.getElementById('asset-rows').innerHTML = `<tr><td colspan="6" class="dm-empty">${esc(error.message)}，请刷新重试</td></tr>`; }
  }

  window.loadDataOverview = loadOverview;
  window.openAsset = openAsset;
  window.closeAsset = () => document.getElementById('asset-mask')?.classList.remove('open');
  window.dataCenterStartOperation = startOperation;
  document.querySelectorAll('[data-operation-url]').forEach(button => button.onclick = () => startOperation(button.dataset.operationLabel, button.dataset.operationUrl));
  document.getElementById('asset-mask').onclick = event => { if (event.target.id === 'asset-mask') window.closeAsset(); };
  loadOverview();
  setInterval(loadOverview, 30000);
})();
