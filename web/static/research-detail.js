(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const json = async (url, options) => { const response = await fetch(url, options); const body = await response.json(); if (!response.ok || body.status === 'error' || body.error) throw new Error(body.error?.message || body.error || '请求失败'); return body.data ?? body; };
  const statusText = {requested:'等待执行', running:'运行中', success:'正常', partial_success:'部分可用', failed:'失败', cancelled:'已取消', deferred:'延期', unavailable:'暂无数据', degraded:'部分可用', ok:'正常'};
  const value = item => item == null || item === '' ? '暂无' : (typeof item === 'object' ? JSON.stringify(item) : item);

  function renderAssessment(id, assessment) {
    const data = assessment || {};
    const rows = Object.entries(data).filter(([key]) => !['status','reason','note'].includes(key)).map(([key, val]) => `<dt>${esc(key)}</dt><dd>${esc(value(val))}</dd>`).join('');
    $(id).innerHTML = `<div class="assessment-status">${esc(statusText[data.status] || data.status || '暂无数据')}</div>${data.reason ? `<p class="note">${esc(data.reason)}</p>` : ''}${data.note ? `<p class="note">${esc(data.note)}</p>` : ''}<dl>${rows}</dl>`;
  }

  function render(data) {
    const result = data.result || {};
    $('empty-state').hidden = true; $('research-content').hidden = false;
    $('run-title').textContent = data.symbol || result.symbol || '个股研究';
    $('run-meta').textContent = `运行 ${data.research_run_id || data.id || '--'} · 数据截至 ${data.data_as_of || data.data_context?.max_date || '--'}`;
    const status = data.status || 'success'; $('run-status').textContent = statusText[status] || status; $('run-status').className = `status-badge ${esc(status)}`;
    renderAssessment('technical', result.technical_assessment); renderAssessment('market', result.market_assessment); renderAssessment('valuation', result.valuation_assessment); renderAssessment('fundamental', result.fundamental_assessment);
    const decisions = result.strategy_decision_ids || [];
    $('decision').innerHTML = decisions.length ? `<div class="decision-line"><strong>${esc(decisions[0])}</strong><span>${esc(result.entry_plan?.action || result.exit_plan?.action || '已生成决策')}</span></div><p class="note">${esc(result.entry_plan?.reason || result.exit_plan?.reason || '详见决策记录')}</p>` : '<div class="note">本次研究未绑定策略，未生成 StrategyDecision。</div>';
    const evidence = data.evidence || [];
    $('evidence').innerHTML = evidence.map(item => `<tr><td>${esc(item.evidence_type)}</td><td>${esc(item.metric_name)}</td><td>${esc(value(item.actual_value))}</td><td>${esc(value(item.threshold_value))}</td><td>${esc(statusText[item.assessment] || item.assessment || '暂无')}</td><td class="evidence-explanation">${esc(item.explanation)}</td><td>${esc(item.data_as_of || '--')}</td></tr>`).join('') || '<tr><td colspan="7">暂无证据</td></tr>';
    $('raw-result').textContent = JSON.stringify(data, null, 2);
  }

  async function loadRun(id) { try { render(await json(`/api/research-runs/${encodeURIComponent(id)}`)); $('form-message').textContent = '研究运行已加载。'; } catch (error) { $('form-message').textContent = `加载失败：${error.message}`; } }
  $('research-form').addEventListener('submit', async event => { event.preventDefault(); const payload = {symbol:$('symbol').value, start_date:$('start-date').value, as_of:$('as-of').value, strategy_version_id:$('strategy-version-id').value}; try { $('form-message').textContent = '正在创建研究运行…'; const data = await json('/api/research-runs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); render(data); $('form-message').textContent = '研究完成。'; } catch (error) { $('form-message').textContent = `研究失败：${error.message}`; } });
  const runId = window.RESEARCH_PREFILL_RUN_ID; if (runId) { $('load-run')?.addEventListener('click', () => loadRun(runId)); loadRun(runId); }
})();
