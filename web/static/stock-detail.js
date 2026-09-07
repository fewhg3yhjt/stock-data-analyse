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
    if (pct === null || pct === undefined || isNaN(pct)) return 'var(--color-text-subtle)';
    return pct > 0 ? 'var(--color-danger)' : (pct < 0 ? 'var(--color-success)' : 'var(--color-text-subtle)');
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

  async function readJson(response){
    const body = await response.text();
    if (!body.trim()) throw new Error(`持仓详情接口返回空响应（HTTP ${response.status}）`);
    let data;
    try { data = JSON.parse(body); }
    catch (_) { throw new Error(`持仓详情接口返回非 JSON（HTTP ${response.status}）`); }
    if (!response.ok) throw new Error(data.error || `持仓详情接口失败（HTTP ${response.status}）`);
    return data;
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
      .then(readJson)
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
        <div class="sim-entry-row" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0;padding:8px 10px;background:var(--color-surface-subtle);border:1px solid var(--color-border);border-radius:6px;">
          <span style="font-size:12px;color:var(--color-text-subtle);"><i class="fas fa-bullseye"></i> 模拟收益入场点</span>
          <input type="date" class="sim-entry-date" value="${opts.entryDate || ''}"
                 style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:5px;padding:4px 8px;font-size:12px;">
          <input type="number" step="0.01" class="sim-entry-price" placeholder="入场价" value="${opts.entryPrice || ''}"
                 style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:5px;padding:4px 8px;font-size:12px;width:90px;">
          <button class="btn btn-outline" style="padding:3px 10px;font-size:11px;cursor:pointer;" data-save="1"><i class="fas fa-check"></i> 应用</button>
          <span class="sim-entry-msg" style="font-size:11px;color:var(--color-text-subtle);"></span>
        </div>`;
    }

    const hasEntry = b.entry_price;
    // 收益显示控制：观察页(无 onSimEntry)不展示收益，自选(onSimEntry)/持仓(position)展示
    const showReturn = kind === 'position' || !!opts.onSimEntry;
    const retBlock = (showReturn && ret) ? `
      <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin-top:8px;">
        <span>${kind==='position'?'实际收益率':'模拟收益率'} ${pctHtml(ret.ret_pct_latest)}${kind==='position' && ret.latest_price ? ' <small style="color:var(--color-text-subtle)">(按实时价)</small>' : ''}</span>
        ${kind==='position' ? `<span>浮动盈亏 <b style="color:${color(ret.ret_amount_latest)}">${fmt(ret.ret_amount_latest)} 元</b></span>` : ''}
        <span>入场价 <b>${fmt(hasEntry)}</b></span>
        <span>${kind==='position' && ret.latest_price ? '当前实时价' : '最新收盘'} <b>${fmt(kind==='position' && ret.latest_price ? ret.latest_price : ret.latest_close)}</b></span>
        ${kind==='position' && ret.latest_price ? `<span style="color:var(--color-text-subtle)">最新收盘 <b>${fmt(ret.latest_close)}</b></span>` : ''}
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
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:4px 16px;padding:10px 14px;background:var(--color-surface-subtle);border:1px solid var(--color-border);border-radius:6px;margin-bottom:10px;font-size:13px;">
        <div style="grid-column:1/-1;display:flex;align-items:baseline;gap:12px;margin-bottom:4px;">
          <span style="font-size:24px;font-weight:700;color:${chgColor};">${fmt(b.price, 4)}</span>
          <span style="font-size:15px;font-weight:600;color:${chgColor};">${chg>=0?'+':''}${fmt(chg)}%</span>
          <span style="font-size:11px;color:var(--color-text-subtle);"><i class="fas fa-clock"></i> ${b.snapshot_time || iv.snapshot_time || '—'}</span>
        </div>
        <div><span style="color:var(--color-text-subtle);">今开</span> <b>${fmt(b.open)}</b></div>
        <div><span style="color:var(--color-text-subtle);">昨收</span> <b>${fmt(b.prev_close)}</b></div>
        <div><span style="color:var(--color-text-subtle);">最高</span> <b style="color:var(--color-danger);">${fmt(b.high)}</b></div>
        <div><span style="color:var(--color-text-subtle);">最低</span> <b style="color:var(--color-success);">${fmt(b.low)}</b></div>
        <div><span style="color:var(--color-text-subtle);">成交量</span> <b>${handFmt(b.volume)}</b></div>
        <div><span style="color:var(--color-text-subtle);">成交额</span> <b>${b.amount_wan!=null?(b.amount_wan/10000).toFixed(2)+'亿':'—'}</b></div>
        <div><span style="color:var(--color-text-subtle);">换手率</span> <b>${b.turnover!=null?fmt(b.turnover)+'%':'—'}</b></div>
        <div><span style="color:var(--color-text-subtle);">量比</span> <b>${fmt(b.vol_ratio)}</b></div>
        <div><span style="color:var(--color-text-subtle);">PE(TTM)</span> <b>${fmt(b.pe_ttm)}</b></div>
        <div><span style="color:var(--color-text-subtle);">PB</span> <b>${fmt(b.pb)}</b></div>
        <div><span style="color:var(--color-text-subtle);">总市值</span> <b>${mcapFmt(b.total_mcap)}</b></div>
        <div><span style="color:var(--color-text-subtle);">流通市值</span> <b>${mcapFmt(b.float_mcap)}</b></div>
        <div><span style="color:var(--color-text-subtle);">振幅</span> <b>${b.amplitude!=null?fmt(b.amplitude)+'%':'—'}</b></div>
      </div>`;

    // 盘中走势（10分钟粒度快照序列，有则展示）
    const hasTrend = it && it.times && it.times.length > 0;

    container.innerHTML = `
      ${simRow}
      ${quotePanel}
      <div style="display:flex;gap:6px;margin:6px 0 2px;">
        <button class="tab-btn" data-tab="kline" style="padding:4px 14px;border:1px solid var(--color-info);background:var(--color-info);color:#fff;border-radius:6px;font-size:13px;cursor:pointer;">日K</button>
        <button class="tab-btn" data-tab="trend" style="padding:4px 14px;border:1px solid var(--color-border);background:var(--color-surface);color:var(--color-text-subtle);border-radius:6px;font-size:13px;cursor:pointer;">分时</button>
      </div>
      <div class="kline-wrap">
        <div id="krange" style="font-size:12px;color:var(--color-text-subtle);padding:2px 0 0;"></div>
        <div class="chart kline-chart" style="width:100%;height:470px;"></div>
      </div>
      <div class="intraday-wrap" style="display:none;">
        ${hasTrend ? `<div class="chart intraday-trend-chart" style="width:100%;height:240px;"></div>` : '<div style="font-size:12px;color:var(--color-text-subtle);padding:10px 0;">暂无盘中快照数据</div>'}
      </div>
      ${retBlock}
    `;

    // K线数据（OHLCV + 均线 + 点位）
    const klineEl = container.querySelector('.kline-chart');
    const volEl = container.querySelector('.volume-chart');
    const trendEl = container.querySelector('.intraday-trend-chart');

    let activeTab = 'kline';
    function switchTab(tab){
      activeTab = tab;
      container.querySelectorAll('.tab-btn').forEach(function(b){
        const on = b.dataset.tab === tab;
        b.style.background = on ? 'var(--color-info)' : 'var(--color-surface)';
        b.style.color = on ? '#fff' : 'var(--color-text-subtle)';
        b.style.borderColor = on ? 'var(--color-info)' : 'var(--color-border)';
      });
      const kw = container.querySelector('.kline-wrap');
      const iw = container.querySelector('.intraday-wrap');
      if (kw) kw.style.display = (tab === 'kline') ? '' : 'none';
      if (iw) iw.style.display = (tab === 'trend') ? '' : 'none';
      const tryRender = function(){
        let ok;
        if (tab === 'kline'){ ok = renderKlineVolume(); }
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

    // 绘制 K线(蜡烛+均线+点位) / 成交量 / 盘中走势 —— 直接用 echarts
    let drew = false;
    const charts = [];
    function sized(el){
      return el && el.clientWidth > 0 && el.clientHeight > 0;
    }
    function disposeChart(el){
      const prev = echarts.getInstanceByDom(el);
      if (!prev) return;
      if (window.StockChart?.dispose) window.StockChart.dispose(el);
      else { try{ prev.dispose(); }catch(e){} }
      const idx = charts.indexOf(prev);
      if (idx >= 0) charts.splice(idx, 1);
    }
    const maColors = { ma5:'#8e44ad', ma10:'#2980b9', ma20:'#e67e22', ma60:'#27ae60' };
    // K线 + 成交量 合并为同一个实例：两个 grid，共享 dataZoom，一个滑块联动两张图
    function renderKlineVolume(){
      if (!window.echarts) return false;
      if (!(kDates && kDates.length)) return false;
      if (!sized(klineEl)) return false;   // 容器未就绪 → 由 draw 重试
      disposeChart(klineEl);
      const chart = window.StockChart?.mount(klineEl) || echarts.init(klineEl);
      charts.push(chart);
      // boundaryGap 自带首尾内边距，不加额外空类目：120根均匀铺满、最新K线贴右。
      const chartDates = kDates;
      // yAxis 范围：收盘 + 高/低 + 点位，避免点位或影线超出可视区
      let yMin = Infinity, yMax = -Infinity;
      const scan = (v) => { const n = Number(v); if(!isNaN(n)){ if(n<yMin)yMin=n; if(n>yMax)yMax=n; } };
      kCloses.forEach(scan); kHighs.forEach(scan); kLows.forEach(scan);
      (kLines||[]).forEach(l => scan(l.value));
      if (!isFinite(yMin)){ yMin = 0; yMax = 1; }
       const pad = (yMax - yMin) * 0.05 || 0.1;

       function visibleRange(payload){
         const n = chartDates.length;
         let startValue = payload && payload.startValue;
         let endValue = payload && payload.endValue;
         let start = payload && payload.start;
         let end = payload && payload.end;
         if (payload && payload.batch && payload.batch.length){
           const b = payload.batch[0];
           startValue = b.startValue ?? startValue; endValue = b.endValue ?? endValue;
           start = b.start ?? start; end = b.end ?? end;
         }
         if (startValue !== undefined && endValue !== undefined){
           return [Math.max(0, Number(startValue)), Math.min(n - 1, Number(endValue))];
         }
         const option = chart.getOption().dataZoom || [];
         const dz = option.find(z => z.xAxisIndex !== undefined) || {};
         const s = Number(start ?? dz.start ?? 0), e = Number(end ?? dz.end ?? 100);
         return [Math.max(0, Math.floor((s / 100) * (n - 1))), Math.min(n - 1, Math.ceil((e / 100) * (n - 1)))];
       }
       function visiblePriceBounds(payload){
         const range = visibleRange(payload);
         let low = Infinity, high = -Infinity;
         const scanVisible = values => values.slice(range[0], range[1] + 1).forEach(v => {
           const n = Number(v); if (!isNaN(n)){ low = Math.min(low, n); high = Math.max(high, n); }
         });
         scanVisible(kCloses); scanVisible(kHighs); scanVisible(kLows);
         ['ma5','ma10','ma20','ma60'].forEach(name => { if (kMas[name]) scanVisible(kMas[name]); });
         (kLines || []).forEach(line => { const n = Number(line.value); if (!isNaN(n)){ low = Math.min(low, n); high = Math.max(high, n); } });
         if (!isFinite(low)) return null;
         const margin = (high - low) * 0.08 || Math.max(Math.abs(low) * 0.01, 0.01);
         return {min: low - margin, max: high + margin};
       }

      const hasOHLC = kOpens.some(v => v !== null && v !== undefined);
      const klineData = kDates.map((_, i) => [kOpens[i], kCloses[i], kLows[i], kHighs[i]]);
      const volData = kDates.map((_, i) => {
        const o = kOpens[i], c = kCloses[i];
        const up = (c !== null && c !== undefined && o !== null && o !== undefined) ? (Number(c) >= Number(o)) : true;
        return { value: kVolumes[i], itemStyle:{ color: up ? '#dc3545' : '#28a745' } };
      });

      const series = [];
      if (hasOHLC){
        series.push({ name:'K线', type:'candlestick', xAxisIndex:0, yAxisIndex:0, data:klineData,
          itemStyle:{ color:'#dc3545', color0:'#28a745', borderColor:'#dc3545', borderColor0:'#28a745' } });
      } else {
        series.push({ name:'收盘', type:'line', xAxisIndex:0, yAxisIndex:0, data:kCloses, showSymbol:false,
          lineStyle:{width:1.5,color:'#1a73e8'}, itemStyle:{color:'#1a73e8'} });
      }
      const maLabels = { ma5:'MA5', ma10:'MA10', ma20:'MA20', ma60:'MA60' };
      ['ma5','ma10','ma20','ma60'].forEach(function(n){
        const data = kMas[n];
        if (data && data.some(v => v !== null && v !== undefined)){
          series.push({ name:maLabels[n], type:'line', xAxisIndex:0, yAxisIndex:0, data:data, showSymbol:false,
            lineStyle:{width:1, color:maColors[n]}, itemStyle:{color:maColors[n]} });
        }
      });
      // 成交量副图（grid1 / yAxis1 / xAxis1）
      series.push({ name:'成交量', type:'bar', xAxisIndex:1, yAxisIndex:1,
        data:volData, barWidth:'60%' });

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
                 out += (p.marker || '') + ' ' + p.seriesName + '：<b>' + fmt(p.value, 4) + '</b><br/>';
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
          // 默认展示全量历史，最新K线贴近右边界（boundaryGap 留首尾内边距）。
          { type:'slider', xAxisIndex:[0,1], bottom:6, height:18,
            start:0, end:100,
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
      const tradeLabels = {buy:'买入', sell:'卖出', sell_all:'清仓', dividend:'分红', correction:'纠错'};
      const tradeColors = {buy:'#16a34a', sell:'#dc2626', sell_all:'#dc2626', dividend:'#9333ea', correction:'#64748b'};
      const tradePoints = (h.transaction_points || []).map(function(point){
        const type = point.type || '';
        const label = tradeLabels[type] || type || '交易';
        const color = tradeColors[type] || '#64748b';
        const isBuy = type === 'buy';
        return {
          name: label, coord: [point.index, point.price], value: label + ' ' + point.price,
          itemStyle: {color: color}, symbol: 'triangle',
          symbolRotate: isBuy ? 0 : 180, symbolSize: 12,
          label: {show:false},
          tradePoint: point
        };
      });
      if (tradePoints.length){
        series[0].markPoint = {
          symbol:'triangle', symbolSize:12, label:{show:false}, data:tradePoints,
          tooltip:{formatter:function(params){
            const p = params.data.tradePoint || {};
            const label = tradeLabels[p.type] || p.type || '交易';
            return '<b>' + label + '</b><br/>' +
              '交易日：' + (p.trade_date || p.date || '—') + '<br/>' +
              '价格：' + (p.price ?? '—') + '<br/>' +
              '份额：' + (p.shares ?? '—') + '<br/>' +
              '金额：' + (p.amount ?? '—') + '<br/>' +
              (p.reason ? '说明：' + p.reason : '');
          }}
        };
      }
       chart.setOption(opt, true);
       chart.off('datazoom');
       chart.on('datazoom', function(payload){
         const bounds = visiblePriceBounds(payload);
         if (bounds) chart.setOption({yAxis:[{min:bounds.min, max:bounds.max}]}, false);
       });
      // 显式同步到默认视图，避免 ECharts 或复用的 dataZoom 状态覆盖初始范围。
      try{
        chart.dispatchAction({type:'dataZoom', dataZoomIndex:1, start:0, end:100});
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
      const chart = window.StockChart?.mount(trendEl) || echarts.init(trendEl);
      charts.push(chart);
      chart.setOption({
        title:{ text:'分时走势（' + (it.day || '未知日期') + ' · ' + (it.source || '未知来源') + '）', left:0, top:0, textStyle:{fontSize:12, color:'#6b7280', fontWeight:'normal'} },
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

    function draw(){
      if (!window.echarts) return false;
      let any = false;
      if (activeTab === 'kline'){
        if (!sized(klineEl)) return false;   // 等容器布局完成再画
        if (renderKlineVolume()) any = true;
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
