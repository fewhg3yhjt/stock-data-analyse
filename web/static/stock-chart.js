// 通用 ECharts 图表组件 — 多序列折线 + 可选指标勾选
// 用法:
//   window.StockChart.drawLine(el, {dates, series:[{name,key,data}]}, opts)
//   window.StockChart.drawKLine(el, {dates, closes, lines}, opts)
//   window.StockChart.metricSelector(container, metrics, selected, onChange)
// 需先引入 echarts.min.js

window.StockChart = (function(){
  const METRIC_COLORS = {
    close:'#1a73e8', ma5:'#8e44ad', ma10:'#2980b9', ma20:'#e67e22',
    ma60:'#27ae60', ma120:'#7f8c8d', vol_ratio:'#e74c3c', turn:'#9b59b6',
    amount:'#16a085', pe:'#d35400', pb:'#2c3e50', ret_pct:'#1a73e8',
    ret_amount:'#e67e22'
  };
  const _charts = {};

  function mount(el){
    if (!el || !window.echarts) return null;
    const id = el.getAttribute('data-chart-id') || el.id || Math.random().toString(36).slice(2);
    el.setAttribute('data-chart-id', id);
    const existing = _charts[id] || echarts.getInstanceByDom(el);
    if (existing) {
      _charts[id] = existing;
      return existing;
    }
    const chart = echarts.init(el);
    _charts[id] = chart;
    ensureResizeHandler();
    return chart;
  }

  function dispose(el){
    if (!el || !window.echarts) return;
    const id = el.getAttribute('data-chart-id') || el.id;
    const chart = (id && _charts[id]) || echarts.getInstanceByDom(el);
    if (!chart) return;
    try { chart.dispose(); } catch (e) {}
    if (id) delete _charts[id];
  }

  function colorOf(key){ return METRIC_COLORS[key] || '#1a73e8'; }

  // 全局 resize 监听（只绑一次），让所有图表随容器/窗口尺寸变化重绘
  let _resizeBound = false;
  function ensureResizeHandler(){
    if (_resizeBound) return;
    _resizeBound = true;
    window.addEventListener('resize', function(){
      Object.values(_charts).forEach(function(c){ try{ c.resize(); }catch(e){} });
    });
  }

  function init(el){
    const id = el.id || el.getAttribute('data-chart-id') || Math.random().toString(36).slice(2);
    el.setAttribute('data-chart-id', id);
    if(!_charts[id]){
      // echarts.init 在元素尺寸为 0（如动态展开、父容器未就绪）时创建实例不绘制，
      // 延迟到布局完成后 resize() 强制按真实尺寸重绘
      const chart = mount(el);
      requestAnimationFrame(function(){
        try{ chart.resize(); }catch(e){}
      });
      _charts[id] = chart;
      ensureResizeHandler();
    }
    return _charts[id];
  }

  function baseOption(){
    return {
      tooltip:{trigger:'axis'},
      legend:{top:0, type:'scroll'},
      grid:{left:60, right:30, top:34, bottom:40},
      xAxis:{type:'category', boundaryGap:false},
      yAxis:{type:'value', scale:true},
      dataZoom:[{type:'inside'},{type:'slider', height:16}]
    };
  }

  // 多序列折线（指标勾选）
  function drawLine(el, data, opts={}){
    const chart = init(el);
    const opt = baseOption();
    opt.xAxis.data = data.dates || [];
    opt.yAxis.axisLabel = opts.yFormatter ? {formatter: opts.yFormatter} : {};
    opt.series = (data.series||[]).map(s=>({
      name:s.name, type:'line', data:s.data, showSymbol:false,
      lineStyle:{width: s.key==='close'||s.key==='ret_pct'?1.8:1.1, color:colorOf(s.key)},
      itemStyle:{color:colorOf(s.key)},
      areaStyle: opts.area ? {color:'rgba(26,115,232,0.08)'} : undefined
    }));
    if(data.signals && data.signals.length){
      const closeIdx = (data.series||[]).findIndex(s=>s.key==='close');
      if(closeIdx>=0){
        const dateIdx={}; data.dates.forEach((dt,i)=>dateIdx[dt]=i);
        const marks=data.signals.map(sg=>{
          const x = dateIdx[sg.date]!==undefined?dateIdx[sg.date]:sg.date;
          const isBuy = sg.type==='buy';
          return {name:isBuy?'买':'卖', coord:[x, sg.value], value:sg.value,
            symbol:isBuy?'triangle':'triangle', symbolRotate:isBuy?0:180,
            itemStyle:{color:isBuy?'#28a745':'#dc3545'}, label:{color:'#fff',fontSize:9,formatter:isBuy?'买':'卖'}};
        }).filter(m=>m.coord[0]!==undefined);
        if(marks.length) opt.series[closeIdx].markPoint={symbol:'pin',symbolSize:40,data:marks};
      }
    }
    if(data.lines && data.lines.length){
      opt.series.push({name:'点位', type:'line', data:[], silent:true,
        markLine:{symbol:'none',label:{formatter:p=>p.name,position:'insideEndTop',fontSize:10},
          data:data.lines.map(l=>({name:l.name,yAxis:l.value,lineStyle:{type:'dashed',color:l.color||'#999'}}))}});
    }
    chart.setOption(opt, true);
    return chart;
  }

  function volShort(v){ if(v==null||isNaN(v))return '-'; if(v>=1e8)return (v/1e8).toFixed(1)+'亿'; if(v>=1e4)return (v/1e4).toFixed(1)+'万'; return String(v); }

  // 完整K线（与持仓页一致）：主图蜡烛图+均线，副图成交量，买卖点 triangle，水平线
  function drawKLineFull(el, data, opts={}){
    const chart = init(el);
    const dates = data.dates || [];
    const ohlc = data.ohlc || [];
    const kCloses = (data.closes || ohlc.map(o=>o[1]));
    const kHighs = (data.highs || ohlc.map(o=>o[3]));
    const kLows = (data.lows || ohlc.map(o=>o[2]));
    const vols = data.volumes || [];
    const ma = data.ma || {};
    const signals = data.signals || [];
    const kLines = data.lines || [];
    // y 轴范围
    let yMin=Infinity, yMax=-Infinity;
    const scan=(v)=>{const n=Number(v); if(!isNaN(n)&&isFinite(n)){ if(n<yMin)yMin=n; if(n>yMax)yMax=n; }};
    kCloses.forEach(scan); kHighs.forEach(scan); kLows.forEach(scan);
    kLines.forEach(l=>scan(l.value));
    if(!isFinite(yMin)){yMin=0;yMax=1;}
    const pad=(yMax-yMin)*0.05 || 0.1;
    // 成交量颜色（红涨绿跌）
    const volData = vols.map((v,i)=>{
      const up = (i>0 && kCloses[i]>=kCloses[i-1]);
      return { value:v, itemStyle:{ color: up?'#dc3545':'#28a745' } };
    });
    const maColors={ma5:'#8e44ad',ma10:'#2980b9',ma20:'#e67e22',ma60:'#27ae60'};
    const series=[];
    series.push({name:'K线',type:'candlestick',data:ohlc,xAxisIndex:0,yAxisIndex:0,
      itemStyle:{color:'#dc3545',color0:'#28a745',borderColor:'#dc3545',borderColor0:'#28a745'}});
    Object.keys(ma).forEach(k=>{ if(ma[k]&&ma[k].some(v=>v!=null)) series.push({name:(k.toUpperCase?k.toUpperCase():k),type:'line',data:ma[k],xAxisIndex:0,yAxisIndex:0,showSymbol:false,lineStyle:{width:1,color:maColors[k]||'#1a73e8'},itemStyle:{color:maColors[k]||'#1a73e8'}}); });
    if(vols.length) series.push({name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:volData,barWidth:'60%'});
    // 买卖点
    if(signals.length){
      const idx={}; dates.forEach((d,i)=>idx[d]=i);
      const pts=signals.filter(s=>idx[s.date]!==undefined).map(s=>{
        const buy=(s.type==='buy');
        return { name: buy?'买入':'卖出', coord:[idx[s.date], s.value], value:s.value,
          itemStyle:{color: buy?'#28a745':'#dc3545'}, symbol:'triangle', symbolRotate: buy?0:180, symbolSize:12,
          label:{show:false} };
      });
      if(pts.length) series[0].markPoint={symbol:'triangle',symbolSize:12,label:{show:false},data:pts};
    }
    chart.setOption({
      tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
      legend:{top:0,type:'scroll'},
      grid:[{left:58,right:24,top:34,height:'58%'},{left:58,right:24,top:'70%',height:'20%'}],
      xAxis:[
        {type:'category',data:dates,boundaryGap:true,axisLabel:{show:false}},
        {type:'category',data:dates,gridIndex:1,boundaryGap:true,axisLabel:{formatter:v=>v?String(v).slice(5):'',hideOverlap:true}}
      ],
      yAxis:[
        {type:'value',scale:true,min:yMin-pad,max:yMax+pad},
        {type:'value',scale:true,gridIndex:1,splitNumber:2,axisLabel:{formatter:volShort}}
      ],
      dataZoom:[{type:'inside',xAxisIndex:[0,1]},{type:'slider',xAxisIndex:[0,1],bottom:6,height:18,start:0,end:100}],
      series:series
    }, true);
    return chart;
  }

  // K线（OHLC 蜡烛图 + 可选均线 + 买卖点标记）
  function drawKLine(el, data, opts={}){
    const chart = init(el);
    const opt = baseOption();
    opt.xAxis.data = data.dates || [];
    opt.yAxis.scale = true;
    opt.series = [];
    if(data.ohlc && data.ohlc.length){
      opt.series.push({name:'K线', type:'candlestick', data:data.ohlc,
        itemStyle:{color:'#dc3545',color0:'#28a745',borderColor:'#dc3545',borderColor0:'#28a745'}});
    }
    (data.series||[]).forEach(s=>{
      opt.series.push({name:s.name,type:'line',data:s.data,showSymbol:false,
        lineStyle:{width:1.1,color:colorOf(s.key)},itemStyle:{color:colorOf(s.key)}});
    });
    if(data.lines && data.lines.length){
      opt.series.push({name:'点位',type:'line',data:[],silent:true,
        markLine:{symbol:'none',label:{formatter:p=>p.name,position:'insideEndTop',fontSize:10},
          data:data.lines.map(l=>({name:l.name,yAxis:l.value,lineStyle:{type:'dashed',color:l.color||'#999'}}))}});
    }
    // 买卖点标记（叠加在 K 线序列上）
    if(data.signals && data.signals.length){
      const kIdx = opt.series.findIndex(s=>s.type==='candlestick');
      if(kIdx>=0){
        const dateIdx={}; (data.dates||[]).forEach((dt,i)=>dateIdx[dt]=i);
        const marks=data.signals.map(sg=>{
          const x = dateIdx[sg.date]!==undefined?dateIdx[sg.date]:sg.date;
          const isBuy = sg.type==='buy';
          return {name:isBuy?'买':'卖', coord:[x, sg.value], value:sg.value,
            symbol:'triangle', symbolRotate:isBuy?0:180,
            itemStyle:{color:isBuy?'#28a745':'#dc3545'}, label:{color:'#fff',fontSize:9,formatter:isBuy?'买':'卖'}};
        }).filter(m=>m.coord[0]!==undefined);
        if(marks.length) opt.series[kIdx].markPoint={symbol:'pin',symbolSize:40,data:marks};
      }
    }
    chart.setOption(opt, true);
    return chart;
  }

  // 指标勾选器
  function metricSelector(container, metrics, selected, onChange){
    container.innerHTML = '';
    (selected||[]).forEach(key=>{
      if(!metrics[key]) return;
      const lbl = document.createElement('label');
      lbl.innerHTML = `<span class="metric-color" style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${colorOf(key)};margin-right:2px;"></span>
        <input type="checkbox" value="${key}" checked onchange="StockChart._metricChanged()"> ${metrics[key]}`;
      container.appendChild(lbl);
    });
    Object.keys(metrics).forEach(key=>{
      if((selected||[]).includes(key)) return;
      const lbl = document.createElement('label');
      lbl.innerHTML = `<span class="metric-color" style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${colorOf(key)};margin-right:2px;"></span>
        <input type="checkbox" value="${key}" onchange="StockChart._metricChanged()"> ${metrics[key]}`;
      container.appendChild(lbl);
    });
    _metricHandler = onChange;
  }

  // 勾选回调：在模块赋值完成后由 StockChart._metricChanged 暴露，避免加载期引用未定义
  let _metricHandler = null;
  function _metricChanged(){
    const container = document.querySelector('.metric-selector');
    if(!container || !_metricHandler) return;
    const selected = Array.from(container.querySelectorAll('input:checked')).map(i=>i.value);
    _metricHandler(selected);
  }

  return { drawLine, drawKLine, drawKLineFull, metricSelector, colorOf, mount, dispose, _metricChanged };
})();
