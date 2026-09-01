(() => {
  const chartEls = {index: 'chart-index', board: 'chart-board', stock: 'chart-stock'};
  const makeChart = id => StockChart.mount(document.getElementById(id));

  function lineOption(title, dates, series, lines){
    const option = {
      title: {text: title, left: 'center', textStyle: {fontSize: 14}},
      tooltip: {trigger: 'axis'}, legend: {top: 28},
      grid: {left: 50, right: 20, top: 60, bottom: 30},
      xAxis: {type: 'category', data: dates, boundaryGap: false},
      yAxis: {type: 'value', scale: true},
      dataZoom: [{type: 'inside'}, {type: 'slider', height: 14}],
      series: series.map(item => ({name: item.name, type: 'line', data: item.data, showSymbol: false, lineStyle: {width: 1.5}})),
    };
    if (lines?.length) {
      option.series.push({name: '点位线', type: 'line', data: [], markLine: {
        silent: true, symbol: 'none',
        label: {formatter: point => point.name, position: 'insideEndTop', fontSize: 10},
        data: lines.map(line => ({name: line.name, yAxis: line.value, lineStyle: {type: 'dashed', color: line.color || '#999'}})),
      }});
    }
    return option;
  }

  function legendHtml(lines){
    if (!lines?.length) return '';
    return '<div style="margin-top:6px;font-size:12px;color:var(--color-text-subtle);">' +
      lines.map(line => '<span style="margin-right:14px;white-space:nowrap;"><span style="display:inline-block;width:16px;height:3px;background:' + (line.color || '#999') + ';vertical-align:middle;margin-right:4px;"></span>' + line.name + '</span>').join('') + '</div>';
  }

  async function loadIndices(){
    const codes = Object.values(window.MARKET_INDICES || {}).join(',');
    try {
      const response = await fetch('/market/index_kline?codes=' + encodeURIComponent(codes));
      const data = await response.json();
      if (data.status !== 'success') return showUiMessage(data.error || '指数加载失败', 'error');
      const names = Object.keys(window.MARKET_INDICES || {});
      const series = names.map(name => ({name, data: data.series[window.MARKET_INDICES[name]] || []}));
      makeChart(chartEls.index).setOption(lineOption('指数走势', data.dates || [], series, null), true);
    } catch (error) { showUiMessage('指数加载失败: ' + error.message, 'error'); }
  }

  async function loadBoard(){
    const name = document.getElementById('board-select').value;
    showUiMessage('加载板块 ' + name + '…', 'info');
    try {
      const response = await fetch('/market/board_kline?name=' + encodeURIComponent(name));
      const data = await response.json();
      if (data.status !== 'success') return showUiMessage(data.error || '板块加载失败', 'error');
      makeChart(chartEls.board).setOption(lineOption(data.name, data.dates || [], [{name: data.name, data: data.close || []}], null), true);
      showUiMessage('板块 ' + data.name + ' 已加载', 'success');
    } catch (error) { showUiMessage('板块加载失败: ' + error.message, 'error'); }
  }

  async function loadStock(){
    const code = document.getElementById('stock-select').value;
    if (!code) return;
    showUiMessage('加载 ' + code + '…', 'info');
    try {
      const response = await fetch('/market/stock_chart?code=' + encodeURIComponent(code));
      const data = await response.json();
      if (data.status !== 'success') return showUiMessage(data.error || '股票加载失败', 'error');
      document.getElementById('stock-state').textContent = '市场状态：' + (data.market_state || '');
      makeChart(chartEls.stock).setOption(lineOption(code, data.dates || [], [{name: code, data: data.close || []}], data.lines || []), true);
      document.getElementById('stock-legend').innerHTML = legendHtml(data.lines || []);
      showUiMessage('已加载 ' + code, 'success');
    } catch (error) { showUiMessage('股票加载失败: ' + error.message, 'error'); }
  }

  function initStocks(){
    const codes = window.MARKET_POSITIONS || [];
    const select = document.getElementById('stock-select');
    if (!codes.length) { select.innerHTML = '<option value="">暂无持仓（先去持仓/观察建仓）</option>'; return; }
    codes.forEach(item => { const option = document.createElement('option'); option.value = item.code; option.textContent = item.name; select.appendChild(option); });
    loadStock();
  }

  window.loadBoard = loadBoard;
  window.loadStock = loadStock;
  window.MARKET_INDICES = window.MARKET_INDICES || {};
  initStocks();
  loadIndices();
})();
