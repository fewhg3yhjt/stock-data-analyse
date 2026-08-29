(() => {
  const state = {tasks: []};
  const stageNames = {CAPTURE:'数据采集',BUILD:'数据构建',QUALITY:'数据质量',PUBLISH:'数据发布',DERIVED:'派生计算'};
  const typeNames = {SOURCE_CAPTURE:'源数据采集',DATA_BUILD:'标准数据构建',QUALITY_CHECK:'数据质量检查',DATA_PUBLISH:'数据正式发布',INDICATOR_BUILD:'技术指标计算',FACTOR_BUILD:'研究因子计算',DERIVED_BUILD:'派生数据计算'};
  const statusNames = {running:'执行中',success:'已完成',partial_success:'部分完成',failed:'执行失败',skipped:'已跳过',scheduled:'已排期',disabled:'未启用'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const statusTag = value => {
    const key = value || 'disabled';
    const cls = key === 'success' ? 'healthy' : key === 'failed' ? 'critical' : key === 'running' ? 'stale' : key === 'partial_success' ? 'partial' : 'unknown';
    return `<span class="dm-health ${cls}">${esc(statusNames[key] || key)}</span>`;
  };
  const taskStatus = task => task.running_run?.status || task.latest_run?.status || (task.enabled ? 'scheduled' : 'disabled');
  const formatRange = run => run?.period_start && run?.period_end ? `${run.period_start} ~ ${run.period_end}` : '暂无执行记录';

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
        ${tasks.map(task => { const run = task.running_run || task.latest_run; const schedule = task.schedule || {}; return `<tr data-task-key="${esc(task.task_key)}"><td><strong>${esc(task.display_name)}</strong><div class="dm-code">${esc(task.task_key)}</div></td><td>${esc(task.task_type_label || typeNames[task.task_type] || task.task_type)}</td><td>${esc(schedule.frequency || '手动')}<div class="dm-code">${schedule.time ? esc(schedule.time) : '依赖上游'} · ${schedule.enabled ? '已启用' : '未启用'}</div></td><td>${esc(formatRange(run))}</td><td>${statusTag(taskStatus(task))}</td><td>${run?.progress == null ? '—' : `${run.progress}%`}</td><td><div class="dm-task-actions"><button class="dm-mini-btn detail-task">查看详情</button><button class="dm-mini-btn run-task">立即执行</button></div></td></tr>`; }).join('')}
        </tbody></table>
      </section>`).join('') || '<div class="dm-empty">没有匹配的任务</div>';
    document.querySelectorAll('#task-list [data-task-key]').forEach(row => {
      const task = state.tasks.find(item => item.task_key === row.dataset.taskKey);
      row.querySelector('.detail-task').onclick = () => showTask(task);
      row.querySelector('.run-task').onclick = () => runTask(task);
    });
  }

  function showTask(task) {
    if (!task) return;
    const run = task.running_run || task.latest_run;
    const config = task.config || {};
    document.getElementById('task-drawer-title').textContent = task.display_name;
    document.getElementById('task-drawer-sub').textContent = `${stageNames[task.stage] || task.stage} · ${task.task_type_label || typeNames[task.task_type] || task.task_type}`;
    document.getElementById('task-drawer-body').innerHTML = `<section class="dm-detail-section"><h3>执行概览</h3><dl class="dm-kv"><dt>任务代码</dt><dd>${esc(task.task_key)}</dd><dt>状态</dt><dd>${statusTag(taskStatus(task))}</dd><dt>配置版本</dt><dd>v${esc(task.active_config_version || '—')}</dd><dt>默认周期</dt><dd>${esc(task.schedule?.frequency || '手动')} ${esc(task.schedule?.time || '')}</dd><dt>本次范围</dt><dd>${esc(formatRange(run))}</dd><dt>输入数据</dt><dd>${esc((config.task?.input_datasets || []).join('、') || '—')}</dd><dt>输出数据</dt><dd>${esc((config.task?.output_datasets || []).join('、') || '—')}</dd></dl></section><section class="dm-detail-section"><h3>运行进度</h3><dl class="dm-kv"><dt>运行记录</dt><dd>${run?.run_id ? `#${run.run_id}` : '暂无'}</dd><dt>当前阶段</dt><dd>${esc(run?.phase || '—')}</dd><dt>进度</dt><dd>${run?.progress == null ? '—' : `${run.progress}% (${run.processed ?? '—'}/${run.total ?? '—'})`}</dd><dt>最近结果</dt><dd>${esc(run?.error || '—')}</dd></dl></section><section class="dm-detail-section"><h3>任务管理</h3><div class="dm-toolbar"><button class="dm-btn" onclick="window.taskCenterToggle('${esc(task.task_key)}',${!task.enabled})">${task.enabled ? '停用调度' : '启用调度'}</button><button class="dm-btn" onclick="window.taskCenterEdit('${esc(task.task_key)}')">编辑配置</button>${(task.config_versions || []).filter(v => v.status === 'validated').map(v => `<button class="dm-btn" onclick="window.taskCenterActivate('${esc(task.task_key)}',${v.version})">生效 v${v.version}</button>`).join('')}${run?.status === 'failed' || run?.status === 'partial_success' ? `<button class="dm-btn" onclick="window.taskCenterRetry(${run.run_id})">重试</button>` : ''}</div></section><section class="dm-detail-section"><h3>相关操作</h3><div class="dm-toolbar"><button class="dm-btn" onclick="window.taskCenterShowLogs(${run?.run_id || 0})">查看日志</button><button class="dm-btn" onclick="window.taskCenterShowArtifacts(${run?.run_id || 0})">查看产物</button><button class="dm-btn primary" onclick="window.taskCenterExecute('${esc(task.task_key)}')">立即执行</button></div></section>`;
    document.getElementById('task-mask').classList.add('open');
  }

  function runTask(task) {
    const map = {stock_daily_capture:'daily-sync', indicators_build:'rebuild-indicators', factors_build:'rebuild-factors'};
    if (!task || !map[task.task_key]) return alert('该任务已登记，但执行处理器尚未接入');
    fetch(`/api/data/jobs/${map[task.task_key]}`, {method:'POST'}).then(response => response.json()).then(data => {
      if (data.status !== 'success') alert(data.error || '启动失败'); else loadTasks();
    }).catch(error => alert(`启动失败：${error.message}`));
  }

  const postJson = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
  window.taskCenterToggle = (key, enabled) => postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/enabled`, {enabled}).then(d => { if (d.status !== 'success') throw new Error(d.error); loadTasks(); }).catch(e => alert(e.message));
  window.taskCenterActivate = (key, version) => { if (!confirm(`确认生效任务配置 v${version}？`)) return; postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/config/${version}/activate`, {}).then(d => { if (d.status !== 'success') throw new Error(d.error); alert('配置已生效'); loadTasks(); }).catch(e => alert(e.message)); };
  window.taskCenterEdit = key => {
    const task = state.tasks.find(item => item.task_key === key);
    if (!task) return;
    const raw = prompt('编辑任务配置 JSON（确认后保存为草稿）', JSON.stringify(task.config || {}, null, 2));
    if (raw == null) return;
    let config;
    try { config = JSON.parse(raw); } catch (e) { alert(`JSON 无效：${e.message}`); return; }
    postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/config`, {config, activate: false}).then(d => { if (d.status !== 'success') throw new Error(d.error); alert(`配置草稿 v${d.version} 已保存`); loadTasks(); }).catch(e => alert(e.message));
  };
  window.taskCenterExecute = key => postJson(`/api/task-center/tasks/${encodeURIComponent(key)}/execute`, {}).then(d => { if (d.status !== 'success') throw new Error(d.error); alert('任务已提交'); loadTasks(); }).catch(e => alert(e.message));
  window.taskCenterRetry = runId => postJson(`/api/task-center/runs/${runId}/retry`, {}).then(d => { if (d.status !== 'success') throw new Error(d.error); alert('重试已提交'); loadTasks(); }).catch(e => alert(e.message));

  function loadTasks() {
    fetch('/api/task-center/overview').then(response => response.json()).then(data => {
      if (data.status !== 'success') throw new Error(data.error || '任务数据读取失败');
      state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
      const overview = data.overview || {};
      const counts = overview.run_counts || {};
      document.getElementById('task-kpis').innerHTML = [['任务定义',state.tasks.length,'已配置任务'],['今日完成',overview.today_completed || 0,'执行实例'],['执行中',overview.running || 0,'执行实例'],['执行失败',overview.failed || 0,'需要关注'],['今日执行',overview.today_total || 0,'执行实例']].map(item => `<div class="dm-kpi"><div class="dm-kpi-label">${item[0]}</div><div class="dm-kpi-value">${item[1]}</div><div class="dm-kpi-note">${item[2]}</div></div>`).join('');
      renderTasks();
      document.getElementById('task-refresh').textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
    }).catch(error => { document.getElementById('task-list').innerHTML = `<div class="dm-empty">${esc(error.message)}，请刷新重试</div>`; });
  }

  window.taskCenterShowLogs = runId => { if (!runId) return alert('当前任务还没有执行记录'); fetch(`/api/task-center/runs/${runId}/logs`).then(r => r.json()).then(d => alert(d.text || '暂无日志')); };
  window.taskCenterShowArtifacts = runId => { if (!runId) return alert('当前任务还没有执行产物'); fetch(`/api/tasks/runs/${runId}/artifacts`).then(r => r.json()).then(d => alert((d.artifacts || []).map(x => x.artifact_type_label || x.file_name).join('\n') || '暂无产物')); };
  document.getElementById('task-stage').onchange = renderTasks;
  document.getElementById('task-status').onchange = renderTasks;
  document.getElementById('task-search').oninput = renderTasks;
  document.getElementById('refresh-tasks').onclick = loadTasks;
  document.getElementById('new-task').onclick = () => alert('新建任务需先注册任务定义；当前支持已有任务的配置管理');
  document.getElementById('close-task').onclick = () => document.getElementById('task-mask').classList.remove('open');
  document.getElementById('task-mask').onclick = event => { if (event.target.id === 'task-mask') document.getElementById('task-mask').classList.remove('open'); };
  loadTasks();
  setInterval(loadTasks, 30000);
})();
