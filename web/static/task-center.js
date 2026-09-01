(() => {
  const state = {tasks: []};
  const stageNames = {CAPTURE:'数据采集',BUILD:'数据构建',QUALITY:'数据质量',PUBLISH:'数据发布',DERIVED:'派生计算'};
  const typeNames = {SOURCE_CAPTURE:'源数据采集',DATA_BUILD:'标准数据构建',QUALITY_CHECK:'数据质量检查',DATA_PUBLISH:'数据正式发布',INDICATOR_BUILD:'技术指标计算',FACTOR_BUILD:'研究因子计算',DERIVED_BUILD:'派生数据计算'};
  const statusNames = new Proxy({}, {get: (_, key) => window.UI_STATUS_LABEL?.(key, 'run') || key || '状态未知'});
  const esc = window.UI_UTILS.esc;
  const statusTag = value => {
    const key = value || 'disabled';
    const cls = window.UI_STATUS_CLS?.(key) || 'status-unknown';
    return `<span class="status-tag ${esc(cls)}">${esc(statusNames[key] || key)}</span>`;
  };
  const taskStatus = task => task.running_run?.status || task.latest_run?.status || (task.enabled ? 'scheduled' : 'disabled');
  const formatRange = run => run?.period_start && run?.period_end ? `${run.period_start} ~ ${run.period_end}` : '暂无执行记录';
  const operation = (message, type = '') => { const target = document.getElementById('task-operation'); if (!target) return; target.className = `dm-operation ${type}`; target.textContent = message; target.hidden = false; };
  const operationError = error => operation(`操作失败：${error.message || error}`, 'error');
  const json = (url, options) => window.UI_UTILS.fetchJson(url, options);

  function renderTasks() {
    const stage = document.getElementById('task-stage').value;
    const status = document.getElementById('task-status').value;
    const query = document.getElementById('task-search').value.trim().toLowerCase();
    const list = state.tasks.filter(task => {
      const current = taskStatus(task);
      return (!stage || task.stage === stage) && (!status || current === status) &&
        (!query || `${task.display_name} ${task.task_key} ${task.task_type_label}`.toLowerCase().includes(query));
    });
    const groups = {};
    list.forEach(task => (groups[task.stage] ||= []).push(task));
    document.getElementById('task-list').innerHTML = Object.entries(groups).map(([stageKey, tasks]) => `
      <section class="dm-task-group">
        <div class="dm-task-group-head">${esc(stageNames[stageKey] || stageKey)} <b>${tasks.length}</b></div>
        <table class="dm-task-table"><thead><tr><th>任务名称</th><th>任务类型</th><th>默认周期</th><th>本次执行范围</th><th>状态</th><th>进度</th><th>操作</th></tr></thead><tbody>
         ${tasks.map(task => { const run = task.running_run || task.latest_run; const schedule = task.schedule || {}; const retry = run && ['failed','partial_success'].includes(run.status); return `<tr data-task-key="${esc(task.task_key)}"><td><strong>${esc(task.display_name)}</strong><div class="dm-code">${esc(task.task_key)}</div></td><td>${esc(task.task_type_label || typeNames[task.task_type] || task.task_type)}</td><td>${esc(schedule.frequency || '手动')}<div class="dm-code">${schedule.time ? esc(schedule.time) : '依赖上游'} · ${schedule.enabled ? '已启用' : '未启用'}</div></td><td>${esc(formatRange(run))}</td><td>${statusTag(taskStatus(task))}</td><td>${run?.progress == null ? '—' : `${run.progress}%`}</td><td><div class="dm-task-actions"><button class="dm-mini-btn detail-task">配置/详情</button><button class="dm-mini-btn toggle-task">${task.enabled ? '停用' : '启用'}</button><button class="dm-mini-btn run-task">执行</button>${retry ? '<button class="dm-mini-btn retry-task">重试</button>' : ''}</div></td></tr>`; }).join('')}
        </tbody></table>
      </section>`).join('') || '<div class="dm-empty">没有匹配的任务</div>';
    document.querySelectorAll('#task-list [data-task-key]').forEach(row => {
      const task = state.tasks.find(item => item.task_key === row.dataset.taskKey);
      row.querySelector('.detail-task').onclick = () => showTask(task);
      row.querySelector('.run-task').onclick = () => runTask(task);
      row.querySelector('.toggle-task')?.addEventListener('click', event => { event.stopPropagation(); window.taskCenterToggle(task.task_key, !task.enabled); });
      row.querySelector('.retry-task')?.addEventListener('click', event => { event.stopPropagation(); window.taskCenterRetry((task.running_run || task.latest_run).run_id); });
    });
  }

  function showTask(task) {
    if (!task) return;
    const run = task.running_run || task.latest_run;
    const config = task.config || {};
    document.getElementById('task-drawer-title').textContent = task.display_name;
    document.getElementById('task-drawer-sub').textContent = `${stageNames[task.stage] || task.stage} · ${task.task_type_label || typeNames[task.task_type] || task.task_type}`;
    document.getElementById('task-drawer-body').innerHTML = `<section class="dm-detail-section"><h3>执行概览</h3><dl class="dm-kv"><dt>任务代码</dt><dd>${esc(task.task_key)}</dd><dt>状态</dt><dd>${statusTag(taskStatus(task))}</dd><dt>配置版本</dt><dd>v${esc(task.active_config_version || '—')}</dd><dt>默认周期</dt><dd>${esc(task.schedule?.frequency || '手动')} ${esc(task.schedule?.time || '')}</dd><dt>本次范围</dt><dd>${esc(formatRange(run))}</dd><dt>输入数据</dt><dd>${esc((config.task?.input_datasets || []).join('、') || '—')}</dd><dt>输出数据</dt><dd>${esc((config.task?.output_datasets || []).join('、') || '—')}</dd></dl></section><section class="dm-detail-section"><h3>运行进度</h3><dl class="dm-kv"><dt>运行记录</dt><dd>${run?.run_id ? `#${run.run_id}` : '暂无'}</dd><dt>当前阶段</dt><dd>${esc(run?.phase || '—')}</dd><dt>进度</dt><dd>${run?.progress == null ? '—' : `${run.progress}% (${run.processed ?? '—'}/${run.total ?? '—'})`}</dd><dt>最近结果</dt><dd>${esc(run?.error || '—')}</dd></dl></section><section class="dm-detail-section"><h3>任务管理</h3><div class="dm-toolbar"><button class="dm-btn" data-task-action="toggle" data-task-key="${esc(task.task_key)}">${task.enabled ? '停用调度' : '启用调度'}</button><button class="dm-btn" data-task-action="edit" data-task-key="${esc(task.task_key)}">编辑配置</button>${(task.config_versions || []).filter(v => v.status === 'validated').map(v => `<button class="dm-btn" data-task-action="activate" data-task-key="${esc(task.task_key)}" data-version="${v.version}">生效 v${v.version}</button>`).join('')}${run?.status === 'failed' || run?.status === 'partial_success' ? `<button class="dm-btn" data-task-action="retry" data-run-id="${run.run_id}">重试</button>` : ''}</div></section><section class="dm-detail-section"><h3>相关操作</h3><div class="dm-toolbar"><button class="dm-btn" data-task-action="show-logs" data-run-id="${run?.run_id || 0}">查看日志</button><button class="dm-btn" data-task-action="show-artifacts" data-run-id="${run?.run_id || 0}">查看产物</button><button class="dm-btn primary" data-task-action="execute" data-task-key="${esc(task.task_key)}">立即执行</button></div></section>`;
    document.getElementById('task-mask').classList.add('open');
  }

  function runTask(task) {
    if (!task) return;
    return window.UI_UTILS.once(`task-center:execute:${task.task_key}`, async () => { operation(`${task.display_name}已提交，正在刷新运行状态…`); const data = await json(`/api/task-center/tasks/${encodeURIComponent(task.task_key)}/execute`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})}); if (data.status !== 'success') throw new Error(data.error || '启动失败'); operation(`${task.display_name}已提交，当前接口未返回运行编号，请在列表中查看状态。`, 'ok'); loadTasks(); }).catch(operationError);
  }

  const postJson = (url, body) => json(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  window.taskCenterToggle = (key, enabled) => postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/enabled`, {enabled}).then(d => { if (d.status !== 'success') throw new Error(d.error); operation(enabled ? '调度已启用' : '调度已停用', 'ok'); loadTasks(); }).catch(operationError);
  window.taskCenterActivate = async (key, version) => { if (!await window.showUiConfirm(`生效后将用于后续调度。`, `生效任务配置 v${version}`)) return; postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/config/${version}/activate`, {}).then(d => { if (d.status !== 'success') throw new Error(d.error); operation(`任务配置 v${version} 已生效`, 'ok'); loadTasks(); }).catch(operationError); };
  window.taskCenterEdit = key => {
    const task = state.tasks.find(item => item.task_key === key);
    if (!task) return;
    const config = JSON.parse(JSON.stringify(task.config || {}));
    const schedule = config.schedule ||= {};
    const execution = config.execution ||= {};
    const policy = config.policy ||= {};
    const scope = config.scope ||= {};
    const body = document.getElementById('task-drawer-body');
    const escapeValue = value => esc(value ?? '');
    body.innerHTML = `<section class="dm-detail-section"><h3>编辑任务配置</h3><div class="dm-config-form"><label>调度启用 <input id="cfg-enabled" type="checkbox" ${schedule.enabled ? 'checked' : ''}></label><label>频率 <select id="cfg-frequency"><option value="trading_day">交易日</option><option value="daily">每日</option><option value="weekly">每周</option><option value="monthly">每月</option><option value="quarterly">每季度</option><option value="after_upstream">依赖上游</option><option value="manual">手动</option></select></label><label>执行时间 <input id="cfg-time" type="time" value="${escapeValue(schedule.time || '')}"></label><label>资产类型 <input id="cfg-assets" value="${escapeValue((scope.asset_types || []).join(','))}" placeholder="stock,etf"></label><label>重试次数 <input id="cfg-retry" type="number" min="0" max="10" value="${Number(execution.retry_limit || 0)}"></label><label>失败策略 <select id="cfg-failure"><option value="block_downstream">阻断下游</option><option value="continue">继续执行</option></select></label><div class="dm-toolbar"><button class="dm-btn" id="cfg-save">保存草稿</button><button class="dm-btn primary" id="cfg-save-active">保存并生效</button></div><p class="dm-muted">保存后会生成新配置版本；“保存并生效”会立即用于后续调度。</p></div></section>`;
    document.getElementById('cfg-frequency').value = schedule.frequency || 'manual';
    document.getElementById('cfg-failure').value = policy.on_failure || 'block_downstream';
    const save = activate => {
      config.schedule.enabled = document.getElementById('cfg-enabled').checked;
      config.schedule.frequency = document.getElementById('cfg-frequency').value;
      config.schedule.time = document.getElementById('cfg-time').value || null;
      config.scope.asset_types = document.getElementById('cfg-assets').value.split(',').map(x => x.trim()).filter(Boolean);
      config.execution.retry_limit = Number(document.getElementById('cfg-retry').value || 0);
      config.policy.on_failure = document.getElementById('cfg-failure').value;
      postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/config`, {config, activate}).then(d => { if (d.status !== 'success') throw new Error(d.error); operation(activate ? '配置已保存并生效' : `配置草稿 v${d.version} 已保存`, 'ok'); loadTasks(); }).catch(operationError);
    };
    document.getElementById('cfg-save').onclick = () => save(false);
    document.getElementById('cfg-save-active').onclick = async () => { if (await window.showUiConfirm('保存后将立即用于后续调度。', '保存并立即生效')) save(true); };
  };
  window.taskCenterExecute = key => { operation('任务已提交，正在刷新运行状态…'); return postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/execute`, {}).then(d => { if (d.status !== 'success') throw new Error(d.error); operation('任务已提交，当前接口未返回运行编号，请在列表中查看状态。', 'ok'); loadTasks(); }).catch(operationError); };
  window.taskCenterRetry = runId => { operation(`运行记录 #${runId} 正在重试…`); return postJson(`/api/task-center/runs/${runId}/retry`, {}).then(d => { if (d.status !== 'success') throw new Error(d.error); operation(`运行记录 #${runId} 已提交重试`, 'ok'); loadTasks(); }).catch(operationError); };

  function loadTasks() {
    document.getElementById('task-list').innerHTML = '<div class="dm-empty">正在读取任务…</div>';
    json('/api/task-center/overview').then(data => {
      if (data.status !== 'success') throw new Error(data.error || '任务数据读取失败');
      state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
       const overview = data.overview || {};
       renderManagementSummary(overview);
      const counts = overview.run_counts || {};
      document.getElementById('task-kpis').innerHTML = [['任务定义',state.tasks.length,'已配置任务'],['今日完成',overview.today_completed || 0,'执行实例'],['执行中',overview.running || 0,'执行实例'],['执行失败',overview.failed || 0,'需要关注'],['今日执行',overview.today_total || 0,'执行实例']].map(item => `<div class="dm-kpi"><div class="dm-kpi-label">${item[0]}</div><div class="dm-kpi-value">${item[1]}</div><div class="dm-kpi-note">${item[2]}</div></div>`).join('');
      renderTasks();
      document.getElementById('task-refresh').textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
    }).catch(error => { document.getElementById('task-list').innerHTML = `<div class="dm-empty dm-error-state">${esc(error.message)}，请刷新重试</div>`; });
  }

  function renderManagementSummary(overview) {
    let banner = document.getElementById('task-management-summary');
    if (!banner) {
      banner = document.createElement('div'); banner.id = 'task-management-summary'; banner.className = 'dm-management-banner';
      document.getElementById('task-kpis').before(banner);
    }
    banner.innerHTML = `<strong>任务管理</strong><span>共 ${overview.task_definition_count || 0} 个任务，${overview.enabled_schedule_count || 0} 个已启用调度，${overview.registered || 0} 个已有运行记录。</span><span class="dm-management-hint">列表可直接启用/停用、执行和重试。</span>`;
  }

  window.taskCenterShowLogs = runId => { const target = document.getElementById('task-run-detail'); if (!runId) return operation('当前任务还没有执行记录', 'error'); target.innerHTML = '<span class="dm-muted">正在读取日志…</span>'; fetch(`/api/task-center/runs/${runId}/logs`).then(r => r.json()).then(d => { if (d.status !== 'success') throw new Error(d.error || '日志读取失败'); target.innerHTML = `<h4>运行日志 #${runId}</h4><pre class="dm-log">${esc(d.text || '暂无日志')}</pre>`; }).catch(operationError); };
  window.taskCenterShowArtifacts = runId => { const target = document.getElementById('task-run-detail'); if (!runId) return operation('当前任务还没有执行产物', 'error'); target.innerHTML = '<span class="dm-muted">正在读取产物…</span>'; fetch(`/api/tasks/runs/${runId}/artifacts`).then(r => r.json()).then(d => { if (d.status !== 'success') throw new Error(d.error || '产物读取失败'); const items = d.artifacts || []; const rows = items.map(x => '<div class="dm-artifact"><strong>' + esc(x.artifact_type_label || x.file_name || '未命名产物') + '</strong><span>' + esc(x.file_path || '') + '</span></div>').join(''); target.innerHTML = `<h4>运行产物 #${runId}</h4>${rows || '<span class="dm-muted">暂无产物</span>'}`; }).catch(operationError); };
  document.getElementById('task-drawer-body').addEventListener('click', event => {
    const button = event.target.closest('[data-task-action]');
    if (!button) return;
    const action = button.dataset.taskAction;
    const key = button.dataset.taskKey;
    const runId = Number(button.dataset.runId || 0);
    if (action === 'toggle') window.taskCenterToggle(key, !state.tasks.find(task => task.task_key === key)?.enabled);
    else if (action === 'edit') window.taskCenterEdit(key);
    else if (action === 'activate') window.taskCenterActivate(key, Number(button.dataset.version));
    else if (action === 'retry') window.taskCenterRetry(runId);
    else if (action === 'show-logs') window.taskCenterShowLogs(runId);
    else if (action === 'show-artifacts') window.taskCenterShowArtifacts(runId);
    else if (action === 'execute') window.taskCenterExecute(key);
  });
  document.getElementById('task-stage').onchange = renderTasks;
  document.getElementById('task-status').onchange = renderTasks;
  document.getElementById('task-search').oninput = renderTasks;
  document.getElementById('refresh-tasks').onclick = loadTasks;
  document.getElementById('new-task')?.addEventListener('click', () => operation('新建任务需先注册任务定义；当前支持已有任务的配置管理'));
  document.getElementById('close-task').onclick = () => document.getElementById('task-mask').classList.remove('open');
  document.getElementById('task-mask').onclick = event => { if (event.target.id === 'task-mask') document.getElementById('task-mask').classList.remove('open'); };
  loadTasks();
  setInterval(loadTasks, 30000);
})();
