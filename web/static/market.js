(() => {
  const chartEls = {index: 'chart-index', board: 'chart-board', stock: 'chart-stock'};
  const rotations = window.MARKET_ROTATIONS || {};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (value, digits = 2, percent = false) => value == null || value === '' ? '—' : `${Number(value * (percent ? 100 : 1)).toFixed(digits)}${percent ? '%' : ''}`;
  const stateClass = s => s === 'strong' ? 'green' : s === 'weak' ? 'red' : 'gray';
  const value = (item, key) => key === 'as_of' ? item.as_of : item[key];
  const filterKeys = ['status', 'sector', 'score', 'return_1d', 'return_5d', 'return_20d', 'up_ratio', 'amount_ratio', 'member_count', 'leader'];
  const filterLabels = {status:'状态', sector:'行业/板块', score:'评分', return_1d:'1日', return_5d:'5日', return_20d:'20日', up_ratio:'上涨比例', amount_ratio:'成交额比', member_count:'成员数', leader:'龙头'};

  function renderLiveRotation(payload) {
    const panel = document.getElementById('live-rotation-panel');
    const meta = document.getElementById('live-rotation-meta');
    if (payload?.status !== 'success' || !payload.items?.length) {
      meta.textContent = payload?.error || '暂无在线板块数据';
      panel.innerHTML = `<div class="empty">${esc(payload?.error || '暂无在线板块数据')}</div>`;
      return;
    }
    meta.textContent = `${payload.warning} · 请求时间 ${payload.data_as_of}`;
    const rows = payload.items.map(item => {
      const positive = Number(item.change_pct) >= 0;
      const netClass = Number(item.net) >= 0 ? 'positive' : 'negative';
      const stateClass = item.state === '强势上行' || item.state === '资金回流' ? 'green' : item.state === '弱势下行' || item.state === '价涨钱走' ? 'red' : 'gray';
      return `<tr><td><b>${esc(item.name)}</b></td><td class="${positive ? 'positive' : 'negative'}">${item.change_pct == null ? '—' : `${Number(item.change_pct).toFixed(2)}%`}</td><td class="${netClass}">${item.net == null ? '—' : `${Number(item.net).toFixed(2)}亿`}</td><td class="${Number(item.net_days) >= 0 ? 'positive' : 'negative'}">${item.net_days == null ? '—' : `${Number(item.net_days).toFixed(2)}亿`}</td><td>${esc(item.trend || '—')}</td><td><span class="tag ${stateClass}">${esc(item.state)}</span></td><td>${esc(item.advice)}</td><td>${esc(item.leader || '—')}</td></tr>`;
    }).join('');
    panel.innerHTML = `<table class="ui-table market-table live-rotation-table"><thead><tr><th>板块</th><th>今日涨跌</th><th>即时净额</th><th>近3日净额</th><th>资金趋势</th><th>趋势判断</th><th>操作建议</th><th>领涨股</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  async function loadLiveRotation() {
    const button = document.getElementById('live-rotation-refresh');
    button.disabled = true; button.textContent = '加载中…';
    try {
      const response = await fetch('/market/live_rotation?kind=industry');
      renderLiveRotation(await response.json());
    } catch (error) {
      renderLiveRotation({status: 'error', error: `在线板块数据加载失败：${error.message}`});
    } finally { button.disabled = false; button.textContent = '刷新在线数据'; }
  }

  function table(category, payload) {
    const panel = document.getElementById(`rotation-${category}`);
    const items = payload?.items || [];
    if (!items.length) { panel.innerHTML = `<div class="empty">${esc(payload?.reason || '暂无可用的已发布行业轮动特征。')}</div>`; return; }
    const headers = [['rank_5d','排名（按5日收益）'],['as_of','最新日期'],['industry_name','行业/板块'],['status','状态'],['score','评分'],['return_1d','1日'],['return_5d','5日'],['return_20d','20日'],['up_ratio','上涨比例'],['amount_ratio','成交额比'],['member_count','成员数'],['leader','龙头']];
    const th = headers.map(([key, label]) => {
      const filterKey = key === 'industry_name' ? 'sector' : key;
      const filter = filterKeys.includes(filterKey) ? `<select class="rotation-filter" data-filter="${filterKey}" aria-label="筛选${label}"><option value="">全部</option></select>` : '';
      return `<th data-sort="${key}"><button class="rotation-sort" type="button">${label}<span class="sort-icon" aria-hidden="true"><i class="fas fa-arrow-down"></i><i class="fas fa-arrow-up"></i></span></button>${filter}</th>`;
    }).join('');
    const rows = items.map(item => {
      item.as_of = payload.actual_data_as_of || '';
      const id = item.industry_id || item.industry_code || '';
      const name = item.industry_name || '';
      const discovery = `/market-discovery?category=${encodeURIComponent(category)}&sector_id=${encodeURIComponent(id)}&sector_name=${encodeURIComponent(name)}&as_of=${encodeURIComponent(item.as_of)}&membership_as_of=${encodeURIComponent(item.as_of)}`;
      return `<tr data-status="${esc(item.status)}" data-status-label="${esc(item.status_label || item.status || '未知')}" data-sector-id="${esc(id)}" data-sector-name="${esc(name)}" data-item='${esc(JSON.stringify(item))}'>
        <td>${esc(item.rank_5d ?? '—')}</td><td>${esc(item.as_of || '—')}</td><td><b>${esc(name)}</b><br><span class="note">${esc(id)}</span></td>
        <td><span class="tag ${stateClass(item.status)}">${esc(item.status_label || item.status || '未知')}</span></td><td>${fmt(item.score, 3)}</td>
        <td class="${Number(item.return_1d) >= 0 ? 'positive' : 'negative'}">${fmt(item.return_1d, 2, true)}</td><td class="${Number(item.return_5d) >= 0 ? 'positive' : 'negative'}">${fmt(item.return_5d, 2, true)}</td><td class="${Number(item.return_20d) >= 0 ? 'positive' : 'negative'}">${fmt(item.return_20d, 2, true)}</td>
        <td>${fmt(item.up_ratio, 1, true)}</td><td>${fmt(item.amount_ratio, 2)}</td><td>${esc(item.member_count ?? '—')}</td><td>${esc(item.leader_name || item.leader_code || '—')}</td><td class="rotation-reason">${esc(item.reason || '—')}</td>
        <td><button class="ui-button rotation-chart" data-category="${esc(category)}" data-sector-id="${esc(id)}" data-name="${esc(name)}">看走势/展开K线</button> <a class="ui-button" href="${discovery}">筛选板块内股票</a></td></tr>`;
    }).join('');
    panel.innerHTML = `<div class="note">${esc(payload.reason || '已发布轮动数据')} · 实际交易日 ${esc(payload.actual_data_as_of || '暂无')}</div><div class="table-wrap"><table class="ui-table market-table rotation-table"><thead><tr>${th}<th>原因</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function activePanel() { return document.querySelector('.rotation-panel:not([style*="display: none"])'); }
  function filterValue(row, key, item) {
    if (key === 'sector') return row.dataset.sectorId;
    if (key === 'status') return row.dataset.status;
    if (key === 'leader') return item.leader_name || item.leader_code || '';
    const current = item[key];
    return current == null ? '' : String(current);
  }
  function filterLabel(key, raw, item) {
    if (key === 'status') return item?.status_label || raw;
    if (key === 'sector') return item?.industry_name || raw;
    if (key === 'leader') return item?.leader_name || raw;
    if (['return_1d', 'return_5d', 'return_20d', 'up_ratio'].includes(key)) return fmt(raw, key === 'up_ratio' ? 1 : 2, true);
    if (key === 'score') return fmt(raw, 3);
    return raw;
  }
  function rebuildFilters() {
    const panel = activePanel(); if (!panel) return;
    const rows = [...panel.querySelectorAll('tbody tr:not(.rotation-chart-row)')];
    filterKeys.forEach(key => {
      const select = panel.querySelector(`[data-filter="${key}"]`); if (!select) return;
      const values = new Map();
      rows.forEach(row => { const item = JSON.parse(row.dataset.item || '{}'); const raw = filterValue(row, key, item); if (raw !== '') values.set(raw, filterLabel(key, raw, item)); });
      const sorted = [...values.entries()].sort((a, b) => ['status', 'sector', 'leader'].includes(key) ? a[1].localeCompare(b[1], 'zh') : Number(a[0]) - Number(b[0]));
      select.innerHTML = '<option value="">全部</option>' + sorted.map(([raw, label]) => `<option value="${esc(raw)}">${esc(label)}</option>`).join('');
      select.value = '';
    });
  }
  function applyFilters() {
    const panel = activePanel();
    const selected = Object.fromEntries(filterKeys.map(key => [key, panel?.querySelector(`[data-filter="${key}"]`)?.value || '']));
    panel?.querySelectorAll('tbody tr:not(.rotation-chart-row)').forEach(row => {
      const item = JSON.parse(row.dataset.item || '{}');
      row.hidden = filterKeys.some(key => selected[key] && filterValue(row, key, item) !== selected[key]);
      const detail = row.nextElementSibling;
      if (detail?.classList.contains('rotation-chart-row')) detail.hidden = row.hidden;
    });
  }
  function sortTable(th, toggle = true) {
    const table = th.closest('table'); const key = th.dataset.sort; const current = th.dataset.direction || 'desc'; const effectiveDirection = current === 'desc' ? 'asc' : 'desc';
    table.querySelectorAll('th[data-sort]').forEach(x => { x.dataset.direction = ''; x.classList.remove('sort-active'); }); th.dataset.direction = effectiveDirection; th.classList.add('sort-active');
    const body = table.tBodies[0];
    const entries = [...body.querySelectorAll('tr:not(.rotation-chart-row)')].map(row => ({row, detail: row.nextElementSibling?.classList.contains('rotation-chart-row') ? row.nextElementSibling : null}));
    entries.sort((a, b) => { const ai = JSON.parse(a.row.dataset.item || '{}'); const bi = JSON.parse(b.row.dataset.item || '{}'); const av = value(ai,key), bv = value(bi,key); const an = Number(av), bn = Number(bv); const cmp = av == null ? 1 : bv == null ? -1 : Number.isNaN(an) || Number.isNaN(bn) ? String(av).localeCompare(String(bv),'zh') : an - bn; return effectiveDirection === 'asc' ? cmp : -cmp; });
    entries.forEach(entry => { body.appendChild(entry.row); if (entry.detail) body.appendChild(entry.detail); });
  }
  async function expandRotationChart(btn) {
    const row = btn.closest('tr');
    const existing = row.nextElementSibling;
    if (existing?.classList.contains('rotation-chart-row')) { existing.remove(); btn.textContent = '看走势/展开K线'; return; }
    const chartId = `rotation-chart-${btn.dataset.category}-${btn.dataset.sectorId}`.replace(/[^a-zA-Z0-9_-]/g, '_');
    const detail = document.createElement('tr');
    detail.className = 'rotation-chart-row';
    detail.innerHTML = `<td colspan="14"><div class="rotation-chart-meta">${esc(btn.dataset.name)} · 加载中…</div><div id="${chartId}" class="chart rotation-inline-chart"></div></td>`;
    row.after(detail); btn.textContent = '收起K线';
    try {
      const query = new URLSearchParams({category: btn.dataset.category, sector_id: btn.dataset.sectorId, name: btn.dataset.name});
      const d = await (await fetch('/market/board_kline?' + query)).json();
      if (d.status !== 'success' || !d.dates?.length) throw new Error(d.error || '暂无板块日线数据');
      detail.querySelector('.rotation-chart-meta').textContent = `${d.name || btn.dataset.name} · ${d.series_type || '行业走势'} · 数据截至 ${d.as_of || '—'}`;
      StockChart.drawLine(document.getElementById(chartId), {dates: d.dates, series: [{name: d.name || btn.dataset.name, key: 'close', data: d.close || []}]});
    } catch (e) {
      detail.querySelector('.rotation-chart-meta').textContent = `加载失败：${e.message}`;
    }
  }
  function drawRotationCharts() { document.querySelectorAll('.rotation-panel').forEach(panel => panel.querySelectorAll('.rotation-chart').forEach(btn => btn.onclick = () => expandRotationChart(btn))); }

  async function loadIndices() { const codes = Object.values(window.MARKET_INDICES || {}).join(','); try { const d = await (await fetch('/market/index_kline?codes=' + encodeURIComponent(codes))).json(); if (d.status !== 'success') return showUiMessage(d.error || '指数加载失败','error'); StockChart.drawLine(document.getElementById(chartEls.index), {dates:d.dates || [], series:Object.keys(window.MARKET_INDICES || {}).map(name=>({name,key:name,data:d.series[window.MARKET_INDICES[name]] || []}))}); } catch(e) { showUiMessage('指数加载失败: '+e.message,'error'); } }
  async function loadBoard() { const select=document.getElementById('board-select'), category=document.getElementById('board-category').value, option=select.selectedOptions[0], sectorId=option?.dataset.sectorId || select.dataset.sectorId || ''; if (!sectorId && !select.value) return; try { const d=await (await fetch(`/market/board_kline?category=${encodeURIComponent(category)}&sector_id=${encodeURIComponent(sectorId)}&name=${encodeURIComponent(select.value)}`)).json(); if(d.status!=='success'||!d.dates?.length) return showUiMessage(d.error||'暂无板块日线数据','error'); StockChart.drawLine(document.getElementById(chartEls.board), {dates:d.dates, series:[{name:d.name,key:'close',data:d.close}]}); showUiMessage(`板块 ${d.name} 已加载（截至 ${d.as_of || '—'}）`,'success'); } catch(e) { showUiMessage('板块加载失败: '+e.message,'error'); } }
  async function loadBoardOptions(category) { const select=document.getElementById('board-select'); select.dataset.sectorId=''; select.innerHTML='<option value="">加载中…</option>'; try { const d=await (await fetch('/market/board_options?category='+encodeURIComponent(category))).json(); const options=d.status==='success'?d.options||[]:[]; select.innerHTML=options.length?options.map(x=>`<option value="${esc(x.sector_name)}" data-sector-id="${esc(x.sector_id)}">${esc(x.label)}</option>`).join(''):'<option value="">暂无板块数据</option>'; select.dataset.sectorId=options[0]?.sector_id||''; } catch(e) { select.innerHTML='<option value="">暂无板块数据</option>'; showUiMessage('板块列表加载失败：'+e.message,'error'); } }
  window.loadRotationBoard=(category, id, name)=>{ const select=document.getElementById('board-select'); document.getElementById('board-category').value=category; select.innerHTML=`<option value="${esc(name)}" data-sector-id="${esc(id)}">${esc(name)}</option>`; select.value=name; select.dataset.sectorId=id; loadBoard(); };
  async function loadStock() { const code=document.getElementById('stock-select').value; if(!code)return; try { const d=await (await fetch('/market/stock_chart?code='+encodeURIComponent(code))).json(); if(d.status!=='success')return showUiMessage(d.error||'股票加载失败','error'); StockChart.drawLine(document.getElementById(chartEls.stock), {dates:d.dates||[],series:[{name:code,key:'close',data:d.close||[]}] ,lines:d.lines||[]}); document.getElementById('stock-state').textContent='市场状态：'+(d.market_state||''); document.getElementById('stock-legend').textContent='数据截至：'+(d.as_of||d.dates?.at(-1)||'—'); } catch(e) { showUiMessage('股票加载失败: '+e.message,'error'); } }
  function initRotation() { Object.entries(rotations).forEach(([category,payload])=>table(category,payload)); rebuildFilters(); drawRotationCharts(); }
  document.querySelectorAll('.market-tab').forEach(tab=>tab.onclick=()=>{ document.querySelectorAll('.market-tab').forEach(x=>x.classList.toggle('active',x===tab)); document.querySelectorAll('.rotation-panel').forEach(x=>x.style.display=x.id===`rotation-${tab.dataset.tab}`?'':'none'); rebuildFilters(); applyFilters(); });
  document.querySelector('.rotation-card').addEventListener('click', e=>{ const sortButton=e.target.closest('.rotation-sort'); if(sortButton)sortTable(sortButton.closest('th')); });
  document.querySelector('.rotation-card').addEventListener('change', e=>{ if(e.target.matches('[data-filter]')) applyFilters(); });
  document.getElementById('board-select').onchange=e=>e.currentTarget.dataset.sectorId=e.currentTarget.selectedOptions[0]?.dataset.sectorId||''; document.getElementById('board-category').onchange=e=>loadBoardOptions(e.target.value);
  window.loadBoard=loadBoard; window.loadStock=loadStock; const stock=document.getElementById('stock-select'); (window.MARKET_POSITIONS||[]).forEach(x=>{const o=document.createElement('option');o.value=x.code;o.textContent=x.name;stock.appendChild(o);}); if(stock.options.length)loadStock();
  initRotation(); loadIndices(); loadBoardOptions(document.getElementById('board-category').value); document.getElementById('live-rotation-refresh').onclick=loadLiveRotation; loadLiveRotation();
})();
