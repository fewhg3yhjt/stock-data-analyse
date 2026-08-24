// 统一个股详情视图 — 观察/自选/持仓 共用
// 依赖: echarts.min.js + stock-chart.js
// window.StockDetail.load(code, kind, container, opts)
//   kind: watch / position
//   opts: { entryDate, entryPrice, onSimEntry  } 自选模拟收益回调
// 渲染: 顶部基础指标卡片 + 天级K线 + 盘中实时快照 + 收益图

window.StockDetail = (function(){
  function fmt(v, nd){
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    if (isNaN(n)) return v;
    return n.toFixed(nd === undefined ? 2 : nd);
  }
  function color(pct){
    // 国人习惯：红涨绿跌
    if (pct === null || pct === undefined || isNaN(pct)) return 'var(--text2)';
    return pct > 0 ? 'var(--red)' : (pct < 0 ? 'var(--green)' : 'var(--text2)');
  }
  function pctHtml(v, suffix){
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    const n = Number(v);
    return `<b style="color:${color(n)}">${n >= 0 ? '+' : ''}${fmt(n)}${suffix||'%'}</b>`;
  }
  function volFmt(v){
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    const n = Number(v);
    if (n >= 1e8) return (n/1e8).toFixed(2) + '亿';
    if (n >= 1e4) return (n/1e4).toFixed(1) + '万';
    return fmt(n, 0);
  }

  function load(code, kind, container, opts){
    opts = opts || {};
    const fd = new FormData();
    fd.append('code', code);
    fd.append('kind', kind || 'watch');
    if (opts.entryDate) fd.append('entry_date', opts.entryDate);
    if (opts.entryPrice) fd.append('entry_price', opts.entryPrice);
    return fetch('/api/stock/detail', {method:'POST', body: fd})
      .then(r => r.json())
      .then(d => {
        if (d.status !== 'success') throw new Error(d.error || '加载失败');
        render(container, d, kind, opts);
        return d;
      });
  }

  function render(container, d, kind, opts){
    const b = d.basic || {};
    const h = d.daily_history || {};
    const iv = d.intraday || {};
    const ret = d.returns;

    let simRow = '';
    if (kind === 'watch' && opts.onSimEntry){
      simRow = `
        <div class="sim-entry-row" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0;padding:8px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;">
          <span style="font-size:12px;color:var(--text2);"><i class="fas fa-bullseye"></i> 模拟收益入场点</span>
          <input type="date" class="sim-entry-date" value="${opts.entryDate || ''}"
                 style="background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:4px 8px;font-size:12px;">
          <input type="number" step="0.01" class="sim-entry-price" placeholder="入场价" value="${opts.entryPrice || ''}"
                 style="background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:4px 8px;font-size:12px;width:90px;">
          <button class="btn btn-outline" style="padding:3px 10px;font-size:11px;cursor:pointer;" data-save="1"><i class="fas fa-check"></i> 应用</button>
          <span class="sim-entry-msg" style="font-size:11px;color:var(--text2);"></span>
        </div>`;
    }

    const hasEntry = b.entry_price;
    const retBlock = ret ? `
      <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin-top:8px;">
        <span>${kind==='position'?'实际收益率':'模拟收益率'} ${pctHtml(ret.ret_pct_latest)}</span>
        ${kind==='position' ? `<span>浮动盈亏 <b style="color:${color(ret.ret_amount_latest)}">${fmt(ret.ret_amount_latest)} 元</b></span>` : ''}
        <span>入场价 <b>${fmt(hasEntry)}</b></span>
        <span>最新收盘 <b>${fmt(ret.latest_close)}</b></span>
      </div>` : '';

    const marketCls = { '强多':'green','弱多':'blue','震荡':'orange','空头':'red' }[b.market_state] || 'gray';

    // 全量指标表：固定表头 + 单行数据（含列表未展示的字段）
    const metricDefs = [
      ['现价', fmt(b.price)],
      ['涨跌幅', pctHtml(b.change_pct)],
      ['成交量', volFmt(b.volume)],
      ['成交额', b.amount_wan ? (b.amount_wan/10000).toFixed(2)+'亿' : '—'],
      ['换手率', b.turnover != null ? fmt(b.turnover)+'%' : '—'],
      ['量比', fmt(b.vol_ratio)],
      ['PE(TTM)', fmt(b.pe_ttm)],
      ['PB', fmt(b.pb)],
      ['MA20', fmt(b.ma_20)],
      ['MA60', fmt(b.ma_60)],
      ['市场状态', `<span class="tag ${marketCls}">${b.market_state||'—'}</span>`],
      ['弱支撑', fmt(b.weak_support)],
      ['强支撑', fmt(b.strong_support)],
      ['年内高点', fmt(b.year_high)],
      ['趋势', (b.trend||'—')],
    ];
    const metricTable = `
      <div class="metric-table-wrap" style="overflow-x:auto;margin-bottom:12px;">
        <table class="metric-table" style="width:100%;border-collapse:collapse;font-size:12px;">
          <thead><tr>
            ${metricDefs.map(d=>`<th style="position:sticky;top:0;background:var(--surface2);color:var(--text2);font-weight:500;padding:6px 10px;border:1px solid var(--border);white-space:nowrap;">${d[0]}</th>`).join('')}
          </tr></thead>
          <tbody><tr>
            ${metricDefs.map(d=>`<td style="padding:7px 10px;border:1px solid var(--border);text-align:center;white-space:nowrap;font-weight:600;">${d[1]}</td>`).join('')}
          </tr></tbody>
        </table>
      </div>`;

    container.innerHTML = `
      ${simRow}
      ${metricTable}
      ${iv.price || iv.price===0 ? `<div class="intraday-note" style="font-size:11px;color:var(--text2);margin-bottom:8px;">
          <i class="fas fa-clock"></i> 盘中快照 ${fmt(iv.snapshot_time)}：现价 ${fmt(iv.price)}，涨跌 ${pctHtml(iv.change_pct)}，换手 ${fmt(iv.turnover)}%，量比 ${fmt(iv.vol_ratio)}</div>` : ''}
      <div class="chart kline-chart" style="width:100%;height:300px;"></div>
      <div class="returns-chart" style="width:100%;height:220px;margin-top:10px;"></div>
      ${retBlock}
    `;

    // K线（收盘 + 点位）
    const klineEl = container.querySelector('.kline-chart');
    const kData = { dates: h.dates || [], series:[{name:'收盘', key:'close', data: h.closes || []}], lines: d.lines || [] };

    // 收益折线
    const retEl = container.querySelector('.returns-chart');
    let retData = null;
    if (ret && ret.dates && ret.dates.length){
      retData = { dates: ret.dates, series:[
        {name: kind==='position'?'实际收益%':'模拟收益%', key:'ret_pct', data: ret.ret_pct}
      ]};
    } else {
      retEl.style.display = 'none';
    }

    // 等待 echarts 就绪后绘制（自托管秒级，但防时序竞争）
    function draw(){
      if (!window.StockChart || !window.echarts){ return false; }
      if (kData.dates && kData.dates.length) window.StockChart.drawKLine(klineEl, kData);
      if (retData) window.StockChart.drawLine(retEl, retData, {yFormatter:'{value}%'});
      return true;
    }
    if (!draw()){
      let tries = 0;
      const iv2 = setInterval(function(){
        if (draw()){ clearInterval(iv2); return; }
        if (++tries > 40){ clearInterval(iv2); }  // ~4s 上限
      }, 100);
    }

    // 模拟入场点事件
    if (kind === 'watch' && opts.onSimEntry){
      const dInput = container.querySelector('.sim-entry-date');
      const pInput = container.querySelector('.sim-entry-price');
      if (dInput) dInput.value = opts.entryDate || (b.entry_date || '');
      if (pInput) pInput.value = opts.entryPrice || b.entry_price || '';
      // 日期变化 → 自动请求该日收盘价
      if (dInput){
        dInput.onchange = function(){
          fetch('/api/stock/detail', {method:'POST', body: (function(){const f=new FormData();f.append('code',d.code);f.append('kind','watch');f.append('entry_date',dInput.value);return f;})()})
            .then(r=>r.json()).then(j=>{
              if (j.status==='success' && j.basic && j.basic.entry_price){
                pInput.value = j.basic.entry_price;
              }
            }).catch(()=>{});
        };
      }
      // 应用 → 保存入场点并重算收益
      const saveBtn = container.querySelector('[data-save="1"]');
      if (saveBtn){
        saveBtn.onclick = function(){
          const entry = {};
          if (dInput.value) entry.date = dInput.value;
          if (pInput.value) entry.price = pInput.value;
          const msg = container.querySelector('.sim-entry-msg');
          if (!entry.date && !entry.price){ msg.textContent = '请选择日期或填价'; return; }
          try {
            opts.onSimEntry(entry).then(ok => {
              if (ok){ msg.textContent = '已保存，收益已重算';
                window.StockDetail.load(d.code, 'watch', container, Object.assign({}, opts, {entryDate: entry.date||'', entryPrice: entry.price||''}));
              } else msg.textContent = '保存失败';
            }).catch(()=>{ msg.textContent = '保存失败'; });
          } catch(e){ msg.textContent = '保存失败'; }
        };
      }
    }
  }

  return { load, fmt, pctHtml, color, volFmt };
})();
