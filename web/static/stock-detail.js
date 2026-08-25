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
    if (opts.onStart) opts.onStart(code);
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
        if (opts.onDone) opts.onDone(code, d);
        return d;
      })
      .catch(err => { if (opts.onError) opts.onError(code, err); throw err; });
  }

  function render(container, d, kind, opts){
    const b = d.basic || {};
    const h = d.daily_history || {};
    const iv = d.intraday || {};
    const it = d.intraday_trend || {};
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
    // 收益显示控制：观察页(无 onSimEntry)不展示收益，自选(onSimEntry)/持仓(position)展示
    const showReturn = kind === 'position' || !!opts.onSimEntry;
    const retBlock = (showReturn && ret) ? `
      <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin-top:8px;">
        <span>${kind==='position'?'实际收益率':'模拟收益率'} ${pctHtml(ret.ret_pct_latest)}</span>
        ${kind==='position' ? `<span>浮动盈亏 <b style="color:${color(ret.ret_amount_latest)}">${fmt(ret.ret_amount_latest)} 元</b></span>` : ''}
        <span>入场价 <b>${fmt(hasEntry)}</b></span>
        <span>最新收盘 <b>${fmt(ret.latest_close)}</b></span>
      </div>` : '';

    function mcapFmt(v){
      if (v === null || v === undefined || isNaN(Number(v))) return '—';
      const n = Number(v);
      if (n >= 10000) return (n/10000).toFixed(2) + '万亿';
      return n.toFixed(1) + '亿';
    }
    function handFmt(v){
      if (v === null || v === undefined || isNaN(Number(v))) return '—';
      const n = Number(v);
      if (n >= 1e8) return (n/1e8).toFixed(2) + '亿手';
      if (n >= 1e4) return (n/1e4).toFixed(1) + '万手';
      return n + '手';
    }
    // 成交量副图用：输入为「股」(warehouse daily volume)，转 手→万手/亿手
    function volHandFmt(v){
      if (v === null || v === undefined || isNaN(Number(v))) return '';
      const hands = Number(v) / 100;   // 股 → 手
      if (Math.abs(hands) >= 1e8) return (hands/1e8).toFixed(2) + '亿手';
      if (Math.abs(hands) >= 1e4) return (hands/1e4).toFixed(1) + '万手';
      return Math.round(hands) + '手';
    }
    // 副图 Y 轴短格式：不带「手」，只留 万/亿
    function volShortFmt(v){
      if (v === null || v === undefined || isNaN(Number(v))) return '';
      const hands = Number(v) / 100;
      if (Math.abs(hands) >= 1e8) return (hands/1e8).toFixed(1) + '亿';
      if (Math.abs(hands) >= 1e4) return Math.round(hands/1e4) + '万';
      return String(Math.round(hands));
    }

    // 实时报价面板（现价/涨跌/开高低/量额/换手/量比/PE/PB/市值/振幅）
    const chg = Number(b.change_pct);
    const chgColor = color(chg);
    const quotePanel = `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:4px 16px;padding:10px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;margin-bottom:10px;font-size:13px;">
        <div style="grid-column:1/-1;display:flex;align-items:baseline;gap:12px;margin-bottom:4px;">
          <span style="font-size:24px;font-weight:700;color:${chgColor};">${fmt(b.price)}</span>
          <span style="font-size:15px;font-weight:600;color:${chgColor};">${chg>=0?'+':''}${fmt(chg)}%</span>
          <span style="font-size:11px;color:var(--text2);"><i class="fas fa-clock"></i> ${b.snapshot_time || iv.snapshot_time || '—'}</span>
        </div>
        <div><span style="color:var(--text2);">今开</span> <b>${fmt(b.open)}</b></div>
        <div><span style="color:var(--text2);">昨收</span> <b>${fmt(b.prev_close)}</b></div>
        <div><span style="color:var(--text2);">最高</span> <b style="color:var(--red);">${fmt(b.high)}</b></div>
        <div><span style="color:var(--text2);">最低</span> <b style="color:var(--green);">${fmt(b.low)}</b></div>
        <div><span style="color:var(--text2);">成交量</span> <b>${handFmt(b.volume)}</b></div>
        <div><span style="color:var(--text2);">成交额</span> <b>${b.amount_wan!=null?(b.amount_wan/10000).toFixed(2)+'亿':'—'}</b></div>
        <div><span style="color:var(--text2);">换手率</span> <b>${b.turnover!=null?fmt(b.turnover)+'%':'—'}</b></div>
        <div><span style="color:var(--text2);">量比</span> <b>${fmt(b.vol_ratio)}</b></div>
        <div><span style="color:var(--text2);">PE(TTM)</span> <b>${fmt(b.pe_ttm)}</b></div>
        <div><span style="color:var(--text2);">PB</span> <b>${fmt(b.pb)}</b></div>
        <div><span style="color:var(--text2);">总市值</span> <b>${mcapFmt(b.total_mcap)}</b></div>
        <div><span style="color:var(--text2);">流通市值</span> <b>${mcapFmt(b.float_mcap)}</b></div>
        <div><span style="color:var(--text2);">振幅</span> <b>${b.amplitude!=null?fmt(b.amplitude)+'%':'—'}</b></div>
      </div>`;

    // 盘中走势（10分钟粒度快照序列，有则展示）
    const hasTrend = it && it.times && it.times.length > 0;

    container.innerHTML = `
      ${simRow}
      ${quotePanel}
      <div style="display:flex;gap:6px;margin:6px 0 2px;">
        <button class="tab-btn" data-tab="kline" style="padding:4px 14px;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:6px;font-size:13px;cursor:pointer;">日K</button>
        <button class="tab-btn" data-tab="trend" style="padding:4px 14px;border:1px solid var(--border);background:var(--surface);color:var(--text2);border-radius:6px;font-size:13px;cursor:pointer;">分时</button>
      </div>
      <div class="kline-wrap">
        <div id="krange" style="font-size:12px;color:var(--text2);padding:2px 0 0;"></div>
        <div class="chart kline-chart" style="width:100%;height:470px;"></div>
        ${showReturn ? `<div class="returns-chart" style="width:100%;height:220px;margin-top:10px;"></div>` : ''}
      </div>
      <div class="intraday-wrap" style="display:none;">
        ${hasTrend ? `<div class="chart intraday-trend-chart" style="width:100%;height:240px;"></div>` : '<div style="font-size:12px;color:var(--text2);padding:10px 0;">暂无盘中快照数据</div>'}
      </div>
      ${retBlock}
    `;

    // K线数据（OHLCV + 均线 + 点位）
    const klineEl = container.querySelector('.kline-chart');
    const volEl = container.querySelector('.volume-chart');
    const trendEl = container.querySelector('.intraday-trend-chart');
    const retEl = container.querySelector('.returns-chart');

    let activeTab = 'kline';
    function switchTab(tab){
      activeTab = tab;
      container.querySelectorAll('.tab-btn').forEach(function(b){
        const on = b.dataset.tab === tab;
        b.style.background = on ? 'var(--accent)' : 'var(--surface)';
        b.style.color = on ? '#fff' : 'var(--text2)';
        b.style.borderColor = on ? 'var(--accent)' : 'var(--border)';
      });
      const kw = container.querySelector('.kline-wrap');
      const iw = container.querySelector('.intraday-wrap');
      if (kw) kw.style.display = (tab === 'kline') ? '' : 'none';
      if (iw) iw.style.display = (tab === 'trend') ? '' : 'none';
      const tryRender = function(){
        let ok;
        if (tab === 'kline'){ ok = renderKlineVolume() && renderRet(); }
        else { ok = renderTrend(); }
        if (!ok) setTimeout(tryRender, 80);
      };
      tryRender();
    }
    container.querySelectorAll('.tab-btn').forEach(function(b){
      b.addEventListener('click', function(){ switchTab(b.dataset.tab); });
    });
    const kDates = h.dates || [];
    const kCloses = h.closes || [];
    const kOpens = h.opens || [];
    const kHighs = h.highs || [];
    const kLows = h.lows || [];
    const kVolumes = h.volumes || [];
    const kMas = h.mas || {};
    const kLines = d.lines || [];

    // 收益折线数据
    let retData = null;
    if (showReturn && ret && ret.dates && ret.dates.length){
      retData = { dates: ret.dates, series:[
        {name: kind==='position'?'实际收益%':'模拟收益%', key:'ret_pct', data: ret.ret_pct}
      ]};
    }

    // 绘制 K线(蜡烛+均线+点位) / 成交量 / 盘中走势 / 收益 —— 直接用 echarts
    let drew = false;
    const charts = [];
    function sized(el){
      return el && el.clientWidth > 0 && el.clientHeight > 0;
    }
    function disposeChart(el){
      const prev = echarts.getInstanceByDom(el);
      if (!prev) return;
      try{ prev.dispose(); }catch(e){}
      const idx = charts.indexOf(prev);
      if (idx >= 0) charts.splice(idx, 1);
    }
    const maColors = { MA5:'#8e44ad', MA10:'#2980b9', MA20:'#e67e22', MA60:'#27ae60' };
    // K线 + 成交量 合并为同一个实例：两个 grid，共享 dataZoom，一个滑块联动两张图
    function renderKlineVolume(){
      if (!window.echarts) return false;
      if (!(kDates && kDates.length)) return false;
      if (!sized(klineEl)) return false;   // 容器未就绪 → 由 draw 重试
      disposeChart(klineEl);
      const chart = echarts.init(klineEl);
      charts.push(chart);
      // 首尾各留一个空类目，避免最早/最新一根K线贴在绘图区边缘而看不见。
      const chartDates = ['', ...kDates, ''];
      // yAxis 范围：收盘 + 高/低 + 点位，避免点位或影线超出可视区
      let yMin = Infinity, yMax = -Infinity;
      const scan = (v) => { const n = Number(v); if(!isNaN(n)){ if(n<yMin)yMin=n; if(n>yMax)yMax=n; } };
      kCloses.forEach(scan); kHighs.forEach(scan); kLows.forEach(scan);
      (kLines||[]).forEach(l => scan(l.value));
      if (!isFinite(yMin)){ yMin = 0; yMax = 1; }
      const pad = (yMax - yMin) * 0.05 || 0.1;

      const hasOHLC = kOpens.some(v => v !== null && v !== undefined);
      const klineData = kDates.map((_, i) => [kOpens[i], kCloses[i], kLows[i], kHighs[i]]);
      const volData = kDates.map((_, i) => {
        const o = kOpens[i], c = kCloses[i];
        const up = (c !== null && c !== undefined && o !== null && o !== undefined) ? (Number(c) >= Number(o)) : true;
        return { value: kVolumes[i], itemStyle:{ color: up ? '#ef5350' : '#26a69a' } };
      });

      const series = [];
      if (hasOHLC){
        series.push({ name:'K线', type:'candlestick', xAxisIndex:0, yAxisIndex:0, data:klineData,
          itemStyle:{ color:'#ef5350', color0:'#26a69a', borderColor:'#ef5350', borderColor0:'#26a69a' } });
      } else {
        series.push({ name:'收盘', type:'line', xAxisIndex:0, yAxisIndex:0, data:kCloses, showSymbol:false,
          lineStyle:{width:1.5,color:'#1a73e8'}, itemStyle:{color:'#1a73e8'} });
      }
      ['MA5','MA10','MA20','MA60'].forEach(function(n){
        const data = kMas[n];
        if (data && data.some(v => v !== null && v !== undefined)){
          series.push({ name:n, type:'line', xAxisIndex:0, yAxisIndex:0, data:data, showSymbol:false,
            lineStyle:{width:1, color:maColors[n]}, itemStyle:{color:maColors[n]} });
        }
      });
      // 成交量副图（grid1 / yAxis1 / xAxis1）
      series.push({ name:'成交量', type:'bar', xAxisIndex:1, yAxisIndex:1,
        data:[{value:null}, ...volData, {value:null}], barWidth:'60%' });
      series.forEach(function(s){
        // 首尾各补一个「无数据」占位；candlestick 须用 '-'（null 会在
        // getInitialData 里读 null.value 抛错），成交量已用 {value:null} 占位。
        if (Array.isArray(s.data) && s.data.length === kDates.length) s.data = ['-', ...s.data, '-'];
      });

      const opt = {
        tooltip:{
          trigger:'axis',
          axisPointer:{ type:'cross' },
          formatter: function(params){
            if (!params || !params.length) return '';
            let out = '<div style="font-weight:600;margin-bottom:2px;">' + params[0].axisValue + '</div>';
            params.forEach(function(p){
              if (p.seriesType === 'candlestick'){
                // ECharts candlestick 的 value 带 dataIndex 前缀: [dataIndex, open, close, lowest, highest]
                const v = p.value || [];
                if (v === '-' || v.length < 2) return;
                let o, c, l, h;
                if (v.length >= 5){ o = v[1]; c = v[2]; l = v[3]; h = v[4]; }
                else { o = v[0]; c = v[1]; l = v[2]; h = v[3]; }
                out += '<span>开 <b>' + o + '</b>　高 <b>' + h + '</b>　低 <b>' + l + '</b>　收 <b>' + c + '</b></span><br/>';
              } else if (p.seriesName === '成交量'){
                out += '成交量：<b>' + volHandFmt(p.value) + '</b><br/>';
              } else if (p.seriesName && p.value !== null && p.value !== undefined){
                out += (p.marker || '') + ' ' + p.seriesName + '：' + p.value + '<br/>';
              }
            });
            if (kLines && kLines.length){
              kLines.forEach(function(l){
                out += '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + (l.color||'#999') + ';margin-right:5px;"></span>'
                     + l.name + '：' + l.value + '<br/>';
              });
            }
            return out;
          }
        },
        legend:{top:0},
        animation:false,
        axisPointer:{ link:[{ xAxisIndex:'all' }] },
        grid:[
          { left:58, right:24, top:34, height:'58%' },
          { left:58, right:24, top:'70%', height:'20%' }
        ],
        xAxis:[
          { type:'category', data:chartDates, boundaryGap:true, axisLabel:{show:false} },
          { type:'category', data:chartDates, gridIndex:1, boundaryGap:true,
            axisLabel:{ formatter: function(v){ return v ? String(v).slice(5) : ''; }, hideOverlap:true } }
        ],
        yAxis:[
          { type:'value', scale:true, min: yMin - pad, max: yMax + pad },
          { type:'value', scale:true, gridIndex:1, splitNumber:2,
            axisLabel:{ formatter: function(v){ return volShortFmt(v); } } }
        ],
        dataZoom:[
          { type:'inside', xAxisIndex:[0,1] },
          // 默认聚焦最近一段交易日，避免首次打开时停在历史左侧；滑块仍可回看全量历史。
          { type:'slider', xAxisIndex:[0,1], bottom:6, height:18,
            startValue:Math.max(0, kDates.length - 60), endValue:chartDates.length - 1,
            handleSize:'130%', showDetail:false }
        ],
        series: series
      };
      if (kLines && kLines.length){
        series[0].markLine = {
          symbol:'none',
          label:{formatter:p=>p.name, position:'insideEndTop', fontSize:10},
          data:kLines.map(l=>({name:l.name, yAxis:l.value, lineStyle:{type:'dashed', color:l.color||'#999'}}))
        };
      }
      chart.setOption(opt, true);
      // 显式同步到最新数据，避免 ECharts 或复用的 dataZoom 状态覆盖初始范围。
      try{
        chart.dispatchAction({type:'dataZoom', dataZoomIndex:1,
          startValue:Math.max(0, kDates.length - 60), endValue:chartDates.length - 1});
      }catch(e){}
      const kr = container.querySelector('#krange');
      if (kr) kr.textContent = '日K区间：' + (kDates[0]||'—') + ' ~ ' + (kDates[kDates.length-1]||'—') + '（共 ' + kDates.length + ' 个交易日）';
      try{ requestAnimationFrame(function(){ chart.resize(); }); }catch(e){}
      return true;
    }
    function renderTrend(){
      if (!window.echarts || !hasTrend) return false;
      if (!sized(trendEl)) return false;
      disposeChart(trendEl);
      const chart = echarts.init(trendEl);
      charts.push(chart);
      chart.setOption({
        title:{ text:'盘中走势（' + (it.day || '当日') + ' 快照源）', left:0, top:0, textStyle:{fontSize:12, color:'#6b7280', fontWeight:'normal'} },
        animation:false,
        tooltip:{trigger:'axis'},
        grid:{left:55, right:30, top:30, bottom:24},
        xAxis:{type:'category', data:it.times, boundaryGap:false},
        yAxis:{type:'value', scale:true},
        series:[{ name:'价格', type:'line', data:it.prices, showSymbol:false,
          lineStyle:{width:1.5, color:'#1a73e8'}, itemStyle:{color:'#1a73e8'},
          areaStyle:{ color:'rgba(26,115,232,0.08)' } }]
      }, true);
      try{ requestAnimationFrame(function(){ chart.resize(); }); }catch(e){}
      return true;
    }

    function renderRet(){
      if (!window.echarts || !retData) return false;
      if (!retEl || !sized(retEl)) return false;
      disposeChart(retEl);
      const chart = echarts.init(retEl);
      charts.push(chart);
      chart.setOption({
        tooltip:{trigger:'axis'}, legend:{top:0}, animation:false,
        grid:{left:55, right:30, top:34, bottom:40},
        xAxis:{type:'category', data:retData.dates, boundaryGap:false},
        yAxis:{type:'value', scale:true, axisLabel:{formatter:'{value}%'}},
        dataZoom:[{type:'inside'}],
        series:[{name:retData.series[0].name, type:'line', data:retData.series[0].data,
                 showSymbol:false, lineStyle:{width:1.5,color:'#e67e22'}, itemStyle:{color:'#e67e22'}}]
      }, true);
      return true;
    }
    function draw(){
      if (!window.echarts) return false;
      let any = false;
      if (activeTab === 'kline'){
        if (!sized(klineEl)) return false;   // 等容器布局完成再画
        if (renderKlineVolume()) any = true;
        if (showReturn && renderRet()) any = true;
      } else {
        if (!sized(trendEl)) return false;
        if (renderTrend()) any = true;
      }
      drew = any;
      return true;
    }
    if (!draw()){
      let tries = 0;
      const iv2 = setInterval(function(){
        if (draw()){ clearInterval(iv2); return; }
        if (++tries > 40){ clearInterval(iv2); }  // ~4s 上限
      }, 100);
    }
    // 折行/关闭后无用时清理实例，避免泄漏与重复初始化
    if (container._ro) try{ container._ro.disconnect(); }catch(e){}
    if (window.ResizeObserver){
      const ro = new ResizeObserver(function(){
        charts.forEach(function(c){ try{ c.resize(); }catch(e){} });
      });
      ro.observe(container);
      container._ro = ro;
    }
    // 首帧再次 resize，规避初始化时容器宽度测量偏差（图被父级/视口裁切导致右侧数据不可见）
    try{ requestAnimationFrame(function(){ charts.forEach(function(c){ try{ c.resize(); }catch(e){} }); }); }catch(e){}
    if (opts.onChart) opts.onChart(drew, klineEl);

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
