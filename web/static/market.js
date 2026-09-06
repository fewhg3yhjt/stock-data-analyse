(() => {
  const chartEls = {index: 'chart-index', board: 'chart-board', stock: 'chart-stock'};
  const rotations = window.MARKET_ROTATIONS || {};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (value, digits = 2, percent = false) => value == null || value === '' ? '—' : `${Number(value * (percent ? 100 : 1)).toFixed(digits)}${percent ? '%' : ''}`;
  const stateClass = s => s === 'strong' ? 'green' : s === 'weak' ? 'red' : 'gray';
  const value = (item, key) => key === 'as_of' ? item.as_of : item[key];
  const filterKeys = ['status', 'sector', 'score', 'return_1d', 'return_5d', 'return_20d', 'up_ratio', 'amount_ratio', 'member_count', 'leader'];
  const filterLabels = {status:'状态', sector:'行业/板块', score:'评分', return_1d:'1日', return_5d:'5日', return_20d:'20日', up_ratio:'上涨比例', amount_ratio:'成交额比', member_count:'成员数', leader:'龙头'};
  const stageLabels = {DORMANT:'潜伏', STARTING:'启动', RISING:'主升', CLIMAX:'高潮', FADING:'退潮', COLD:'冰点'};
  const stageClass = stage => ({STARTING:'green', RISING:'green', CLIMAX:'orange', FADING:'red', COLD:'gray', DORMANT:'blue'}[stage] || 'gray');
  const pct = value => value == null ? '—' : `${Number(value * 100).toFixed(2)}%`;
  const score = value => value == null ? '—' : Number(value).toFixed(1);
  function sectorDiscovery(item) { return `/market-discovery?category=ths_industry&sector_id=${encodeURIComponent(item.industry_id)}&sector_name=${encodeURIComponent(item.industry_name)}&as_of=${encodeURIComponent(item.data_as_of || '')}&membership_as_of=${encodeURIComponent(item.data_as_of || '')}`; }
  function officialRows(items) { return items.map((item, index) => `<tr><td>${index + 1}</td><td><a class="rotation-link" href="${sectorDiscovery(item)}"><b>${esc(item.industry_name)}</b></a><br><span class="note">${esc(item.industry_id)}</span></td><td><span class="tag ${stageClass(item.stage)}">${esc(item.stage_label || stageLabels[item.stage] || item.stage)}</span></td><td>${esc(item.transition_type || 'NORMAL')}</td><td>${score(item.rotation_heat)}</td><td>${score(item.rotation_acceleration)}</td><td>${score(item.strength_level)}</td><td>${pct(item.return_1d)}</td><td>${pct(item.relative_return_3d)}</td><td>${pct(item.relative_return_5d)}</td><td>${item.rotation_rank_3d_ago == null ? '—' : `${Number(item.rotation_rank_3d_ago).toFixed(0)} → ${Number(item.rotation_rank_1d_ago ?? item.rotation_rank_3d_ago).toFixed(0)} → ${Number(item.rotation_rank ?? item.rotation_rank_1d_ago ?? item.rotation_rank_3d_ago).toFixed(0)}`}</td><td>${item.amount_ratio == null ? '—' : Number(item.amount_ratio).toFixed(2)}</td><td>${item.stage_days ?? '—'}</td><td>${score(item.opportunity_score)}</td><td>${esc(item.advice || '—')}</td><td>${item.leader_name || item.leader_code ? `<a class="rotation-link" href="${sectorDiscovery(item)}">${esc(item.leader_name || item.leader_code)}</a>` : '<span class="note">暂无</span>'}</td><td><button type="button" class="ui-button official-kline" data-industry-id="${esc(item.industry_id)}" data-name="${esc(item.industry_name)}">看走势</button> <a class="ui-button" href="${sectorDiscovery(item)}">筛选板块内股票</a></td></tr>`).join(''); }
  function watchlistCard(title, subtitle, stage, items, icon, tone) { const rows=(items||[]).map((item,index)=>`<div class="watchlist-row"><span class="watchlist-rank ${tone}">${index+1}</span><a class="rotation-link" href="${sectorDiscovery(item)}"><b>${esc(item.industry_name)}</b></a><a class="watchlist-score rotation-link" href="${sectorDiscovery(item)}">${score(title === '正在轮入' ? item.rotation_score : item.strength_score)}</a></div>`).join('')||`<div class="watchlist-empty">当前无板块处于“${stageLabels[stage] || title}”阶段</div>`; return `<div class="rotation-watch-card"><div class="rotation-watch-title"><span><i class="fas ${icon}"></i> ${title}</span><small>${subtitle}</small></div>${rows}<button class="watchlist-more" type="button" data-watch-stage="${stage}">查看全部 ${stageLabels[stage] || title} &gt;</button></div>`; }
  function renderOfficialRotation(payload) {
    const root=document.getElementById('rotation-official-workbench'); if(!root)return;
    if(payload?.status!=='success'||!payload.items?.length){root.innerHTML=`<div class="empty rotation-official-empty">${esc(payload?.reason||'暂无已发布正式板块轮动状态')}</div>`;return;}
    const s=payload.summary||{}, l=payload.watchlists||{};
    payload.items.forEach(item=>{item.data_as_of=payload.actual_data_as_of;});
    root.innerHTML=`<section class="rotation-market-state"><h3><i class="fas fa-chart-column"></i> 市场状态</h3><div class="market-state-layout"><div><div class="market-state-label">${esc(s.market_state||'—')}</div><p>${esc(s.market_reason||'')}</p><p class="note">正式收盘状态，不含盘中资金流观察</p></div><div class="rotation-stat-grid">${[['mainline','当前主线'],['starting','正在启动'],['climax','过热风险'],['fading','正在轮出']].map(([k,label])=>`<div class="rotation-stat"><span>${label}</span><strong>${s[k]??0}</strong><small>${Number(s[`${k}_delta`]||0)>=0?'↑':'↓'} 较昨日 ${Math.abs(Number(s[`${k}_delta`]||0))}</small></div>`).join('')}</div></div></section><section class="rotation-watchlists"><div class="section-heading"><h3><i class="fas fa-fire"></i> 板块关注榜</h3><span class="note">基于正式阶段、强度和轮动评分，仅供参考</span></div><div class="rotation-watch-grid">${watchlistCard('当前主线','强度最高','RISING',l.mainline,'fa-fire','gold')}${watchlistCard('正在轮入','动量最强','STARTING',l.starting,'fa-arrow-trend-up','blue')}${watchlistCard('过热警告','谨慎追高','CLIMAX',l.climax,'fa-triangle-exclamation','red')}${watchlistCard('正在轮出','注意风险','FADING',l.fading,'fa-arrow-trend-down','green')}</div></section><section class="rotation-list-section"><div class="section-heading"><h3><i class="fas fa-chart-column"></i> 板块列表</h3><div class="rotation-list-tools"><div class="rotation-stage-tabs"><button class="active" data-official-stage="ALL">全部 ${payload.items.length}</button>${Object.entries(stageLabels).map(([k,label])=>`<button data-official-stage="${k}">${label} ${payload.items.filter(item=>item.stage===k).length}</button>`).join('')}</div><input id="official-rotation-search" type="search" placeholder="搜索板块名称或代码..."></div></div><div class="table-wrap official-table-wrap"><table class="ui-table market-table official-rotation-table"><thead><tr><th>#</th><th>板块名称</th><th>阶段</th><th>状态形成</th><th>热度</th><th>加速度</th><th>强度</th><th>今日涨跌</th><th>3日超额</th><th>5日超额</th><th>排名轨迹</th><th>量比</th><th>阶段持续</th><th>机会评级</th><th>操作建议</th><th>龙头股</th><th>操作</th></tr></thead><tbody>${officialRows(payload.items)}</tbody></table></div><div class="official-pagination"><button type="button" data-page="prev">上一页</button><span id="official-page-label">1 / 1</span><button type="button" data-page="next">下一页</button><select id="official-page-size"><option>10</option><option>20</option><option>50</option></select></div><div class="note">正式数据日期 ${esc(payload.actual_data_as_of)} · 数据来自 Published industry_rotation_daily · 盘中资金流请查看上方独立观察卡片</div></section>`;
    bindOfficialRotationTable(payload.items);
  }
  function bindOfficialRotationTable(items) { const root=document.getElementById('rotation-official-workbench'),body=root.querySelector('.official-rotation-table tbody'),search=root.querySelector('#official-rotation-search'),label=root.querySelector('#official-page-label'),size=root.querySelector('#official-page-size'); let stage='ALL',page=1; const render=()=>{const key=(search.value||'').trim().toLowerCase(),filtered=items.filter(x=>(stage==='ALL'||x.stage===stage)&&(!key||`${x.industry_name} ${x.industry_id}`.toLowerCase().includes(key))),pageSize=Number(size.value||10),pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(page,pages);body.innerHTML=officialRows(filtered.slice((page-1)*pageSize,page*pageSize));label.textContent=`${page} / ${pages}`;bindOfficialActions(body);};root.querySelectorAll('[data-official-stage]').forEach(b=>b.onclick=()=>{stage=b.dataset.officialStage;page=1;root.querySelectorAll('[data-official-stage]').forEach(x=>x.classList.toggle('active',x===b));render();});root.querySelectorAll('.watchlist-more').forEach(b=>b.onclick=()=>{stage=b.dataset.watchStage;page=1;root.querySelectorAll('[data-official-stage]').forEach(x=>x.classList.toggle('active',x.dataset.officialStage===stage));render();root.querySelector('.rotation-list-section')?.scrollIntoView({behavior:'smooth',block:'start'});});search.oninput=()=>{page=1;render();};size.onchange=()=>{page=1;render();};root.querySelector('[data-page="prev"]').onclick=()=>{page=Math.max(1,page-1);render();};root.querySelector('[data-page="next"]').onclick=()=>{page+=1;render();};render(); }
  function bindOfficialActions(body) { body.querySelectorAll('.official-kline').forEach(button=>button.onclick=()=>{const row=button.closest('tr');const old=row.nextElementSibling;if(old?.classList.contains('official-kline-row')){old.remove();return;}const detail=document.createElement('tr');detail.className='official-kline-row';detail.innerHTML=`<td colspan="13"><div class="rotation-chart-meta">${esc(button.dataset.name)} · 加载中...</div><div class="chart rotation-inline-chart"></div></td>`;row.after(detail);fetch(`/market/board_kline?category=ths_industry&sector_id=${encodeURIComponent(button.dataset.industryId)}&name=${encodeURIComponent(button.dataset.name)}`).then(response=>response.json()).then(data=>{if(data.status!=='success'||!data.dates?.length)throw new Error(data.error||'暂无板块日线数据');detail.querySelector('.rotation-chart-meta').textContent=`${data.name||button.dataset.name} · 数据截至 ${data.as_of||'—'}`;StockChart.drawLine(detail.querySelector('.chart'),{dates:data.dates,series:[{name:data.name||button.dataset.name,key:'close',data:data.close||[]}]});}).catch(error=>{detail.querySelector('.rotation-chart-meta').textContent=`加载失败：${error.message}`;});}); }

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
  renderOfficialRotation(window.OFFICIAL_ROTATION || {}); initRotation(); loadIndices(); loadBoardOptions(document.getElementById('board-category').value); document.getElementById('live-rotation-refresh').onclick=loadLiveRotation; loadLiveRotation();
})();
