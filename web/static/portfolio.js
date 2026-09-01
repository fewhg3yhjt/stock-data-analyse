(() => {
  const $ = selector => document.querySelector(selector);
  const {esc, once, fetchJson} = window.UI_UTILS;
  let txnPosId = null;
  let txnType = 'buy';

  function formBody(data) {
    const body = new FormData();
    Object.entries(data || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null) body.append(key, value);
    });
    return body;
  }

  function api(url, data, method = 'POST') {
    return fetchJson(url, {method, body: formBody(data)});
  }

  function reloadAfter(message, delay = 600, target = null) {
    showUiMessage(message, 'success');
    setTimeout(() => { if (target) location.href = target; else location.reload(); }, delay);
  }

  function openModal(id) { $(`#modal-${id}`)?.classList.add('active'); }
  function closeModal(id) { $(`#modal-${id}`)?.classList.remove('active'); }

  async function addPosition() {
    return once('portfolio:add-position', async () => {
      const data = {code: $('#np-code').value.trim(), name: $('#np-name').value.trim(), shares: $('#np-shares').value, cost: $('#np-cost').value, buy_date: $('#np-date').value, stock_type: $('#np-type').value, scheme: $('#np-scheme').value, notes: $('#np-notes').value};
      if (!data.code || !data.name || !data.shares || !data.cost) return showUiMessage('请填写完整', 'error');
      const result = await api('/portfolio/add', data);
      if (result.status === 'success') reloadAfter('建仓成功 #' + result.position_id); else showUiMessage(result.error || '建仓失败', 'error');
    }).catch(error => showUiMessage(error.message || '建仓失败', 'error'));
  }

  async function addWatch() {
    return once('portfolio:add-watch', async () => {
      const result = await api('/watchlist', {code: $('#nw-code').value.trim(), name: $('#nw-name').value.trim(), target_capital: $('#nw-capital').value});
      if (result.status === 'success') reloadAfter('已加入自选'); else showUiMessage(result.error || '失败', 'error');
    }).catch(error => showUiMessage(error.message || '添加失败', 'error'));
  }

  async function delWatch(id) {
    return once(`portfolio:delete-watch:${id}`, async () => {
      if (!await showUiConfirm('删除后将移除该自选记录。', '删除自选')) return;
      const result = await fetchJson('/watchlist/' + id, {method: 'DELETE'});
      if (result.status === 'success') reloadAfter('已删除'); else showUiMessage(result.error || '删除失败', 'error');
    }).catch(error => showUiMessage(error.message || '删除失败', 'error'));
  }

  function openTxn(id, type, name) {
    txnPosId = id; txnType = type;
    const title = type === 'buy' ? '记录买入' : type === 'sell' ? '记录卖出' : '记录分红';
    $('#txn-title').replaceChildren(document.createTextNode(`${title} — ${name}`));
    const close = document.createElement('button');
    close.className = 'close'; close.dataset.action = 'closeModal'; close.dataset.id = 'txn'; close.innerHTML = '&times;';
    $('#txn-title').append(close);
    $('#txn-date').value = new Date().toISOString().slice(0, 10);
    openModal('txn');
  }

  async function submitTxn() {
    return once(`portfolio:transaction:${txnPosId}:${txnType}`, async () => {
      const data = {type: txnType, price: txnType === 'dividend' ? 0 : $('#txn-price').value, shares: $('#txn-shares').value, fee: $('#txn-fee').value, date: $('#txn-date').value, reason: txnType === 'dividend' ? '分红' : $('#txn-reason').value};
      const result = await api(`/portfolio/${txnPosId}/transaction`, data);
      if (result.status === 'success') reloadAfter('已记录'); else showUiMessage(result.error || '失败', 'error');
    }).catch(error => showUiMessage(error.message || '记录失败', 'error'));
  }

  async function closePos(id, name) {
    const price = await showUiInput({title: `平仓 ${name}`, message: '请输入成交价。', label: '成交价', type: 'number', placeholder: '成交价'});
    if (!price || Number.isNaN(Number(price))) return;
    return once(`portfolio:close:${id}`, async () => {
      const result = await api(`/portfolio/${id}/close`, {price, reason: '手动平仓'});
      if (result.status === 'success') reloadAfter('已平仓'); else showUiMessage(result.error || '平仓失败', 'error');
    }).catch(error => showUiMessage(error.message || '平仓失败', 'error'));
  }

  async function refresh(button) {
    return once('portfolio:refresh', async () => {
      button.disabled = true; button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 刷新中';
      try { const result = await api('/portfolio/refresh'); if (result.status === 'success') reloadAfter('行情已刷新', 800); else showUiMessage(result.error || '刷新失败', 'error'); }
      finally { button.disabled = false; }
    }).catch(error => showUiMessage(error.message || '刷新失败', 'error'));
  }

  async function genReport(button) {
    return once('portfolio:report', async () => {
      button.disabled = true;
      try { const result = await api('/morning-report'); if (result.status === 'success') reloadAfter('晨报已生成', 600, '/morning-report'); else showUiMessage(result.error || '生成失败', 'error'); }
      finally { button.disabled = false; }
    }).catch(error => showUiMessage(error.message || '生成失败', 'error'));
  }

  // ── 目标价提醒 ──────────────────────────────────────────
  let alertStack = [];
  async function openAlert(id, code, name) {
    alertStack = [id, code, name];
    $('#alert-title').replaceChildren(document.createTextNode(`目标价提醒 — ${name}`));
    const close = document.createElement('button');
    close.className = 'close'; close.dataset.action = 'closeModal'; close.dataset.id = 'alert'; close.innerHTML = '&times;';
    $('#alert-title').append(close);
    // 查询现有规则
    try {
      const res = await fetchJson(`/api/position/${encodeURIComponent(code)}/alert-rules`, {method: 'GET'});
      if (res.status === 'success' && res.rules && res.rules.length) {
        $('#alert-list').replaceChildren(...res.rules.map(r => {
          const li = document.createElement('li');
          const label = (r.rule_type === 'high_minus_cost_pct' ? '(后高-成本)×' : '后高×') + r.threshold_pct + '% · ' + (r.direction === 'below' ? '回撤(现价≤)' : '冲高(现价≥)');
          const btn = document.createElement('button');
          btn.className = 'ui-button ui-button-danger ui-button-sm'; btn.textContent = '删除'; btn.dataset.action = 'deleteAlert'; btn.dataset.ruleId = r.rule_id; btn.dataset.code = code;
          li.append(document.createTextNode(label), ' ', btn);
          return li;
        }));
      } else {
        $('#alert-list').replaceChildren();
      }
    } catch (e) { $('#alert-list').replaceChildren(); }
    openModal('alert');
  }

  async function submitAlert() {
    return once(`portfolio:submit-alert:${alertStack[0]}`, async () => {
      const code = alertStack[1];
      const data = {
        rule_type: $('#alert-rule-type').value,
        direction: $('#alert-direction').value,
        threshold_pct: $('#alert-pct').value,
      };
      if (!data.threshold_pct) return showUiMessage('请填写阈值百分比', 'error');
      const res = await fetchJson(`/api/position/${encodeURIComponent(code)}/alert-rules`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
      if (res.status === 'success') {
        showUiMessage('规则已添加', 'success');
        closeModal('alert');
      } else return showUiMessage(res.error || '添加失败', 'error');
    }).catch(error => showUiMessage(error.message || '添加失败', 'error'));
  }

  async function deleteAlert(ruleId, code) {
    return once(`portfolio:delete-alert:${ruleId}`, async () => {
      const res = await fetchJson(`/api/position/${encodeURIComponent(code)}/alert-rules/${ruleId}`, {method: 'DELETE'});
      if (res.status === 'success') { showUiMessage('规则已删除', 'success'); setTimeout(() => openAlert(null, code, $('#alert-title').textContent.replace('目标价提醒 — ', '')), 300); }
      else return showUiMessage(res.error || '删除失败', 'error');
    }).catch(error => showUiMessage(error.message || '删除失败', 'error'));
  }

  document.addEventListener('click', event => {
    const element = event.target.closest('[data-action]');
    if (!element) return;
    const {action, id, type, name} = element.dataset;
    if (action === 'openModal') openModal(id);
    else if (action === 'closeModal') closeModal(id);
    else if (action === 'addPosition') addPosition();
    else if (action === 'addWatch') addWatch();
    else if (action === 'openTxn') openTxn(Number(id), type, name);
    else if (action === 'submitTxn') submitTxn();
    else if (action === 'closePos') closePos(Number(id), name);
    else if (action === 'delWatch') delWatch(Number(id));
    else if (action === 'refresh') refresh(element);
    else if (action === 'report') genReport(element);
    else if (action === 'openAlert') openAlert(Number(id), code, name);
    else if (action === 'submitAlert') submitAlert();
    else if (action === 'deleteAlert') deleteAlert(element.dataset.ruleId, element.dataset.code);
  });
})();
