(() => {
  const state = {assets: [], attention: [], pipeline: [], selected: null};
  const healthLabels = {healthy: '正常', partial: '部分可用', stale: '待更新', critical: '异常', unknown: '暂无状态'};
  const taskLabels = {running: '执行中', success: '已完成', partial_success: '部分完成', failed: '执行失败', skipped: '已跳过', scheduled: '已排期', disabled: '未启用', waiting: '等待执行'};
  const typeLabels = {stock: '股票', etf: 'ETF', index: '指数'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const health = value => { const key = value || 'unknown'; return `<span class="dm-health ${esc(key)}">${esc(healthLabels[key] || key)}</span>`; };
  const json = url => fetch(url).then(response => response.json());

  function setKpis(counts, tasks) {
    const task = tasks || {};
    const attention = (counts.attention || 0) + (counts.critical || 0);
    document.getElementById('asset-kpis').innerHTML = [
      ['数据项', counts.total || 0, '数据集和指标定义'],
      ['正常', counts.healthy || 0, '健康状态正常'],
      ['待关注', attention, '待更新、部分可用或异常'],
      ['运行中任务', task.running || 0, '当前执行实例'],
      ['今日完成', task.today_completed || 0, '今日执行实例'],
    ].map(item => `<div class="dm-kpi"><div class="dm-kpi-label">${item[0]}</div><div class="dm-kpi-value">${item[1]}</div><div class="dm-kpi-note">${item[2]}</div></div>`).join('');
  }

  function coverageText(asset) {
    const coverage = asset.coverage || {};
    if (coverage.covered == null) return '暂无覆盖数据';
    return `${coverage.covered} / ${coverage.expected ?? '—'}`;
  }

  function renderCoreAssets() {
    const rows = state.assets || [];
    const target = document.getElementById('asset-rows');
    if (!target) return;
    target.innerHTML = rows.map(asset => {
      const ratio = asset.coverage?.ratio;
      return `<tr data-key="${esc(asset.asset_key)}"><td><div class="dm-name">${esc(asset.display_name)}</div><div class="dm-code">${esc(asset.asset_key)}</div></td><td>${esc(asset.category || '数据集')}</td><td>${esc(asset.latest_actual_date || '暂无')}</td><td class="dm-cover">${esc(coverageText(asset))}${ratio != null ? `<div class="dm-cover-bar"><i style="width:${Math.min(100, Math.max(0, ratio * 100))}%"></i></div>` : ''}</td><td>${health(asset.health?.status)}</td><td class="dm-muted">${esc(asset.producer_task || '—')}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="dm-empty">暂无重点数据项</td></tr>';
    target.querySelectorAll('tr[data-key]').forEach(row => row.onclick = () => openAsset(row.dataset.key));
  }

  function renderAttention() {
    const target = document.getElementById('attention-list');
    if (!target) return;
    target.innerHTML = state.attention.map(asset => `<div class="dm-artifact"><div><strong>${esc(asset.display_name)}</strong><span>最新实际日期：${esc(asset.latest_actual_date || '暂无')}</span><span>${esc(asset.health?.message || '需要检查数据状态')}</span></div><div>${health(asset.health?.status)}</div></div>`).join('') || '<div class="dm-detail-empty">当前没有需要关注的数据项</div>';
  }

  function renderPipeline() {
    const target = document.getElementById('pipeline-flow');
    if (!target) return;
    target.innerHTML = state.pipeline.map(task => {
      const run = task.running_run || task.latest_run;
      const status = task.current_status || (run && run.status) || (task.enabled ? 'scheduled' : 'disabled');
      const done = status === 'success' || status === 'partial_success';
      const running = status === 'running';
      const range = run?.actual_period_start && run?.actual_period_end ? `${run.actual_period_start} ~ ${run.actual_period_end}` : '';
      return `<div class="dm-flow-step ${done ? 'done' : running ? 'running' : ''}"><div class="dm-flow-dot">${done ? '✓' : running ? '…' : '○'}</div><div class="dm-flow-title">${esc(task.display_name)}</div><div class="dm-flow-sub">${esc(taskLabels[status] || status)}${range ? `<br>${esc(range)}` : ''}</div></div>`;
    }).join('') || '<div class="dm-empty">暂无生产链路任务</div>';
  }

  function renderAssetDetail(asset) {
    const coverage = asset.coverage || {};
    const byType = coverage.by_asset_type || {};
    const typeCards = ['stock', 'etf', 'index'].map(type => {
      const value = byType[type];
      const applicability = asset.applicability?.[type];
      let text = applicability === 'not_applicable' ? '不适用' : value == null ? '暂无' : typeof value === 'object' ? `${value.covered ?? 0} / ${value.expected ?? '—'}` : value;
      return `<div class="dm-type ${text === '不适用' ? 'na' : ''}"><span>${typeLabels[type]}</span><b>${esc(text)}</b></div>`;
    }).join('');
    const sources = (asset.sources || []).map(item => `<span class="dm-chip">${esc(item.source_name || item.name || '未知来源')}</span>`).join('') || '<span class="dm-muted">暂无来源记录</span>';
    const consumers = (asset.consumers || []).map(item => `<span class="dm-chip">${esc(item.consumer_name || item.consumer || '未知消费者')}</span>`).join('') || '<span class="dm-muted">暂无消费记录</span>';
    const impact = (asset.business_impact || []).map(item => `<li>${esc(item.consumer)}：${esc(item.blocked_actions || item.purpose || '')}</li>`).join('') || '<li>暂无业务影响记录</li>';
    const versions = (asset.versions || []).slice(0, 5).map(item => `<li>${esc(item.partition_key)} · ${esc(item.version_id)} · ${esc(item.quality_status || '暂无质量状态')}</li>`).join('') || '<li>暂无版本记录</li>';
    document.getElementById('asset-detail').innerHTML = `<div class="dm-detail"><h2 class="dm-detail-title">${esc(asset.display_name)}</h2><div class="dm-detail-sub">${esc(asset.category)} · ${esc(asset.asset_key)}</div><section class="dm-detail-section"><h3>定义与统一口径</h3><dl class="dm-kv"><dt>定义</dt><dd>${esc(asset.definition || '暂无')}</dd><dt>单位</dt><dd>${esc(asset.unit || '—')}</dd><dt>关联任务</dt><dd>${esc(asset.producer_task || '—')}</dd></dl></section><section class="dm-detail-section"><h3>健康情况</h3><dl class="dm-kv"><dt>状态</dt><dd>${health(asset.health?.status)}</dd><dt>最新实际日期</dt><dd>${esc(asset.latest_actual_date || '暂无')}</dd><dt>预期日期</dt><dd>${esc(asset.expected_date || '暂无')}</dd><dt>覆盖</dt><dd>${coverage.covered == null ? '暂无' : `${coverage.covered} / ${coverage.expected ?? '—'} (${((coverage.ratio || 0) * 100).toFixed(1)}%)`}</dd><dt>说明</dt><dd>${esc(asset.health?.message || '—')}</dd></dl></section><section class="dm-detail-section"><h3>证券类型覆盖</h3><div class="dm-type-grid">${typeCards}</div></section><section class="dm-detail-section"><h3>来源与消费</h3><div class="dm-kv"><dt>数据来源</dt><dd>${sources}</dd><dt>消费模块</dt><dd>${consumers}</dd></div></section><section class="dm-detail-section"><h3>业务影响</h3><ul>${impact}</ul></section><section class="dm-detail-section"><h3>最近版本</h3><ul>${versions}</ul></section></div>`;
    document.getElementById('asset-mask').classList.add('open');
  }

  async function openAsset(key) {
    const data = await json(`/api/data-center/assets/${encodeURIComponent(key)}`);
    if (data.status === 'success') renderAssetDetail(data.asset);
  }

  async function loadOverview() {
    try {
      const data = await json('/api/data-center/overview');
      if (data.status !== 'success') throw new Error(data.error || '数据总览读取失败');
      state.assets = data.core_assets || [];
      state.attention = data.attention || [];
      state.pipeline = data.pipeline || [];
      setKpis(data.counts || {}, data.task_summary || {});
      renderCoreAssets(); renderAttention(); renderPipeline();
    } catch (error) {
      const target = document.getElementById('asset-rows');
      if (target) target.innerHTML = `<tr><td colspan="6" class="dm-empty">${esc(error.message)}，请刷新重试</td></tr>`;
    }
  }

  window.loadDataOverview = loadOverview;
  window.openAsset = openAsset;
  window.closeAsset = () => document.getElementById('asset-mask')?.classList.remove('open');
  if (document.getElementById('asset-rows')) { loadOverview(); setInterval(loadOverview, 30000); }
})();
