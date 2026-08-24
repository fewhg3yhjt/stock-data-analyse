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
      const chart = echarts.init(el);
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
        const marks=data.signals.map(sg=>({name:'买', coord:[dateIdx[sg.date]!==undefined?dateIdx[sg.date]:sg.date, sg.value]})).filter(m=>m.coord[0]!==undefined);
        if(marks.length) opt.series[closeIdx].markPoint={symbol:'pin',symbolSize:36,itemStyle:{color:'#e74c3c'},label:{color:'#fff',fontSize:9,formatter:'买'},data:marks};
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

  // K线（OHLC 蜡烛图 + 可选均线）
  function drawKLine(el, data, opts={}){
    const chart = init(el);
    const opt = baseOption();
    opt.xAxis.data = data.dates || [];
    opt.yAxis.scale = true;
    opt.series = [];
    if(data.ohlc && data.ohlc.length){
      opt.series.push({name:'K线', type:'candlestick', data:data.ohlc,
        itemStyle:{color:'#ef5350',color0:'#26a69a',borderColor:'#ef5350',borderColor0:'#26a69a'}});
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
    window.StockChart._metricHandler = onChange;
  }

  window.StockChart._metricChanged = function(){
    const container = document.querySelector('.metric-selector');
    if(!container || !window.StockChart._metricHandler) return;
    const selected = Array.from(container.querySelectorAll('input:checked')).map(i=>i.value);
    window.StockChart._metricHandler(selected);
  };

  return { drawLine, drawKLine, metricSelector, colorOf };
})();