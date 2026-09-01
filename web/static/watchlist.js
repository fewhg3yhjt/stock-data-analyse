function toggle(code, wl) {
  const detail = document.getElementById('detail-' + code);
  detail.classList.toggle('open');
  if (detail.classList.contains('open') && !detail.dataset.loaded) {
    detail.dataset.loaded = '1';
    const box = document.getElementById('detail-box-' + code);
    const sim = wl && wl.sim_entry ? wl.sim_entry : {};
    window.StockDetail.load(code, 'watch', box, {
      entryDate: sim.date || '',
      entryPrice: sim.price || '',
      onSimEntry(entry) {
        return fetch('/api/watchlist/' + wl.id + '/sim_entry', {method: 'POST', body: new URLSearchParams(entry)})
          .then(response => response.json())
          .then(data => {
            if (data.status === 'success') {
              showUiMessage('模拟入场点已保存', 'success');
              return true;
            }
            return false;
          })
          .catch(() => false);
      }
    }).catch(error => {
      box.innerHTML = '<span class="stock-detail-error">' + UI_UTILS.esc(error.message) + '</span>';
    });
  }
}

async function simulate(code, name) {
  return UI_UTILS.once('watchlist:simulate:' + code, async () => {
    showUiMessage('模拟中…', 'info');
    const data = await UI_UTILS.fetchJson('/api/simulation', {method: 'POST', body: new URLSearchParams({code, name})});
    if (data.status === 'success') location.href = '/simulation';
    else showUiMessage(data.error || '模拟失败', 'error');
  }).catch(error => showUiMessage(error.message || '模拟失败', 'error'));
}

async function delWatch(id) {
  return UI_UTILS.once('watchlist:delete:' + id, async () => {
    if (!await showUiConfirm('删除后将移除该自选记录。', '删除自选')) return;
    const data = await UI_UTILS.fetchJson('/watchlist/' + id, {method: 'DELETE'});
    if (data.status === 'success') location.reload();
    else showUiMessage(data.error || '删除失败', 'error');
  }).catch(error => showUiMessage(error.message || '删除失败', 'error'));
}

async function addManual() {
  return UI_UTILS.once('watchlist:add', async () => {
    const code = document.getElementById('wl-code').value.trim();
    let name = document.getElementById('wl-name').value.trim();
    if (!code) {
      showUiMessage('请填写代码', 'error');
      return;
    }
    if (!name) {
      try {
        const response = await fetch('/api/stock/lookup?code=' + encodeURIComponent(code));
        const data = await response.json();
        if (data.status === 'success' && data.name) {
          name = data.name;
          document.getElementById('wl-name').value = data.name;
        }
      } catch (error) { /* 查不到就用代码 */ }
    }
    const body = {code, name: name || code};
    const added = document.getElementById('wl-added').value;
    if (added) body.added_time = added;
    const data = await UI_UTILS.fetchJson('/watchlist', {method: 'POST', body: new URLSearchParams(body)});
    if (data.status === 'success') {
      showUiMessage('已加入自选', 'success');
      setTimeout(() => location.reload(), 500);
    } else showUiMessage(data.error || '添加失败', 'error');
  }).catch(error => showUiMessage(error.message || '添加失败', 'error'));
}

async function lookupStock() {
  const code = document.getElementById('wl-code').value.trim();
  const nameInput = document.getElementById('wl-name');
  if (!code || nameInput.value.trim()) return;
  try {
    const response = await fetch('/api/stock/lookup?code=' + encodeURIComponent(code));
    const data = await response.json();
    if (data.status === 'success' && data.name && data.name !== code) nameInput.value = data.name;
  } catch (error) { /* 静默 */ }
}

async function editAddedTime(id, current) {
  const value = await showUiInput({title: '设置观察起点日期', message: '可回看；晚于今天则待观察。', label: '日期（YYYY-MM-DD）', value: current || '', placeholder: 'YYYY-MM-DD'});
  if (!value) return;
  UI_UTILS.fetchJson('/api/watchlist/' + id + '/added_time', {method: 'POST', body: new URLSearchParams({added_time: value})})
    .then(data => {
      if (data.status === 'success') {
        showUiMessage('已更新', 'success');
        setTimeout(() => location.reload(), 400);
      } else showUiMessage(data.error || '更新失败', 'error');
    })
    .catch(error => showUiMessage(error.message || '更新失败', 'error'));
}

document.addEventListener('click', event => {
  const element = event.target.closest('[data-action]');
  if (!element) return;
  const action = element.dataset.action;
  if (action === 'toggle') {
    const sim = element.dataset.simDate || element.dataset.simPrice ? {date: element.dataset.simDate || '', price: element.dataset.simPrice || ''} : {};
    toggle(element.dataset.code, {id: Number(element.dataset.id), sim_entry: sim});
  } else if (action === 'simulate') {
    simulate(element.dataset.code, element.dataset.name);
  } else if (action === 'editAddedTime') {
    event.stopPropagation();
    editAddedTime(Number(element.dataset.id), element.dataset.added || '');
  } else if (action === 'delWatch') {
    delWatch(Number(element.dataset.id));
  } else if (action === 'addManual') {
    addManual();
  }
});

document.getElementById('wl-code')?.addEventListener('blur', lookupStock);
