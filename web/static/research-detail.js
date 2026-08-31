(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const json = async (url, options) => { const response = await fetch(url, options); const body = await response.json(); if (!response.ok || body.status === 'error' || body.error) throw new Error(body.error?.message || body.error || '请求失败'); return body.data ?? body; };
  const statusText = (key, context = 'default') => window.UI_STATUS_LABEL?.(key, context) || key || '状态未知';
  const value = item => item == null || item === '' ? '暂无' : (typeof item === 'object' ? JSON.stringify(item) : item);

  function renderAssessment(id, assessment) {
    const data = assessment || {};
    const rows = Object.entries(data).filter(([key]) => !['status','reason','note'].includes(key)).map(([key, val]) => `<dt>${esc(key)}</dt><dd>${esc(value(val))}</dd>`).join('');
    $(id).innerHTML = `<div class="assessment-status">${esc(statusText(data.status || 'empty'))}</div>${data.reason ? `<p class="note">${esc(data.reason)}</p>` : ''}${data.note ? `<p class="note">${esc(data.note)}</p>` : ''}<dl>${rows}</dl>`;
  }

  function render(data) {
    const result = data.result || {};
    $('empty-state').hidden = true; $('research-content').hidden = false;
    $('run-title').textContent = data.symbol || result.symbol || '个股研究';
    $('run-meta').textContent = `运行 ${data.research_run_id || data.id || '--'} · 数据截至 ${data.data_as_of || data.data_context?.max_date || '--'}`;
    renderContext(data.data_context || {});
    const status = data.status || 'success'; $('run-status').textContent = statusText(status, 'run'); $('run-status').className = `status-badge ${esc(window.UI_STATUS_CLS?.(status) || status)}`;
    renderAssessment('technical', result.technical_assessment); renderAssessment('market', result.market_assessment); renderAssessment('valuation', result.valuation_assessment); renderAssessment('fundamental', result.fundamental_assessment);
    const decisions = result.strategy_decision_ids || [];
    $('decision').innerHTML = decisions.length ? `<div class="decision-line"><strong>${esc(decisions[0])}</strong><span>${esc(result.entry_plan?.action || result.exit_plan?.action || '已生成决策')}</span></div><p class="note">${esc(result.entry_plan?.reason || result.exit_plan?.reason || '详见决策记录')}</p>` : '<div class="note">本次研究未绑定策略，未生成 StrategyDecision。</div>';
    const evidence = data.evidence || [];
    $('evidence').innerHTML = evidence.map(item => `<tr><td>${esc(item.evidence_type)}</td><td>${esc(item.metric_name)}</td><td>${esc(value(item.actual_value))}</td><td>${esc(value(item.threshold_value))}</td><td>${esc(statusText(item.assessment || 'empty'))}</td><td class="evidence-explanation">${esc(item.explanation)}</td><td>${esc(item.data_as_of || '--')}</td></tr>`).join('') || '<tr><td colspan="7">暂无证据</td></tr>';
    $('raw-result').textContent = JSON.stringify(data, null, 2);
  }

  function renderContext(context) {
    const versions = context.dataset_versions || context.partition_versions || context.dataset_refs;
    const versionText = versions && typeof versions === 'object' ? JSON.stringify(versions) : versions;
    const rows = [
      ['数据日期', context.data_as_of || context.max_date || context.returned_end],
      ['数据版本', versionText],
      ['质量状态', context.quality_status],
      ['数据来源', context.source],
      ['使用降级', context.fallback_used == null ? null : context.fallback_used ? '是' : '否'],
      ['是否滞后', context.is_stale == null ? null : context.is_stale ? '是' : '否'],
    ];
    $('data-context').innerHTML = rows.map(([label, item]) => `<dt>${esc(label)}</dt><dd>${esc(value(item))}</dd>`).join('');
  }

  async function loadRun(id) { try { const data = await json(`/api/research-runs/${encodeURIComponent(id)}`); render(data); $('form-message').textContent = '研究运行已加载。'; } catch (error) { $('form-message').textContent = `加载失败：${error.message}`; } }
  async function poll(runId) { for (let i = 0; i < 120; i += 1) { const run = await json(`/api/business-runs/${encodeURIComponent(runId)}`); if (run.status === 'success') { if (run.output_versions?.research_run_id) await loadRun(run.output_versions.research_run_id); return; } if (['failed','cancelled'].includes(run.status)) throw new Error(run.error_message || run.status); await new Promise(resolve => setTimeout(resolve, 1000)); } throw new Error('研究运行超时'); }
  $('research-form').addEventListener('submit', async event => { event.preventDefault(); const payload = {symbol:$('symbol').value, start_date:$('start-date').value, as_of:$('as-of').value, strategy_version_id:$('strategy-version-id').value}; try { $('form-message').textContent = '研究任务已提交，等待 Worker 执行…'; const data = await json('/api/research-runs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); await poll(data.run_id); $('form-message').textContent = '研究完成。'; } catch (error) { $('form-message').textContent = `研究失败：${error.message}`; } });
  const runId = window.RESEARCH_PREFILL_RUN_ID; if (runId) { $('load-run')?.addEventListener('click', () => loadRun(runId)); loadRun(runId); }
})();
