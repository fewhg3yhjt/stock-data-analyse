(() => {
  const esc = window.UI_UTILS.esc;
  const num = id => Number(document.getElementById(id).value);
  const payload = () => ({name: document.getElementById('name').value, version: '1.0', lookback_days: num('lookback_days'), ma_slope_threshold: num('ma_slope_threshold') / 100, center_shift_threshold: num('center_shift_threshold') / 100, low_position_threshold: num('low_position_threshold') / 100, stop_atr_k: num('stop_atr_k'), min_rr: num('min_rr')});
  const setStatus = (text, cls = '') => { const el = document.getElementById('status'); el.textContent = text; el.className = 'op-status ' + cls; };

  async function analyze() {
    const button = document.getElementById('analyze-btn');
    return UI_UTILS.once('operation-points:analyze', async () => {
      button.disabled = true; setStatus('正在读取本地日线并计算…');
      try {
        const response = await UI_UTILS.fetchJson('/api/operation-points/analyze', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: document.getElementById('code').value, config: payload()})});
        if (response.status !== 'success') throw new Error(response.error || '分析失败');
        render(response.result); renderChart(response.series); setStatus('分析完成（参数未保存为版本）', 'state-ok');
      } catch (error) { setStatus('分析失败：' + error.message, 'state-no'); }
      finally { button.disabled = false; }
    });
  }

  function render(result) {
    document.getElementById('headline').textContent = (result.strategy.name || '策略') + ' · ' + (result.state_reasons || []).join('、');
    document.getElementById('asof').textContent = '数据截至 ' + (result.calculation_as_of || '暂无');
    const cls = result.operation === 'BUY_SIGNAL' ? 'state-ok' : result.operation === 'NO_BUY' ? 'state-no' : 'state-wait';
    document.getElementById('summary').innerHTML = `<div class="notice ${cls}"><b>${esc(result.operation)}</b>：${esc(result.operation_reason)}<br>${esc(result.execution)}</div><div class="op-grid"><div class="op-metric"><small>行情状态</small><b>${esc(result.state)}</b></div><div class="op-metric"><small>当前价格</small><b>${result.current_price ?? '—'}</b></div><div class="op-metric"><small>20日位置</small><b>${result.position20 == null ? '—' : (result.position20 * 100).toFixed(1) + '%'}</b><br><small>${esc(result.position_label)}</small></div><div class="op-metric"><small>H20 / L20</small><b>${result.h20 ?? '—'} / ${result.l20 ?? '—'}</b></div><div class="op-metric"><small>MA20 / MA60</small><b>${result.ma20 ?? '—'} / ${result.ma60 ?? '—'}</b></div><div class="op-metric"><small>ATR14</small><b>${result.atr14 ?? '—'}</b></div><div class="op-metric"><small>计划买入价</small><b>${result.plan_buy_price ?? '—'}</b></div><div class="op-metric"><small>结构止损</small><b>${result.stop_price ?? '—'}</b></div><div class="op-metric"><small>Target1 / Target2</small><b>${result.target1 ?? '—'} / ${result.target2 ?? '—'}</b></div><div class="op-metric"><small>风险 / Target1收益</small><b>${result.risk ?? '—'} / ${result.reward1 ?? '—'}</b></div><div class="op-metric"><small>RR1</small><b>${result.rr1 == null ? '—' : result.rr1.toFixed(2)}</b></div><div class="op-metric"><small>MA20穿越次数</small><b>${result.ma20_cross_count}</b></div></div>`;
  }

  function renderChart(series) {
    if (!window.echarts || !series || !series.dates?.length) return;
    const chart = StockChart.mount(document.getElementById('chart'));
    chart.setOption({tooltip: {trigger: 'axis'}, legend: {data: ['收盘', '最高', '最低']}, xAxis: {type: 'category', data: series.dates}, yAxis: {type: 'value', scale: true}, dataZoom: [{type: 'inside'}, {type: 'slider'}], series: [{name: '收盘', type: 'line', data: series.close, showSymbol: false}, {name: '最高', type: 'line', data: series.high, showSymbol: false}, {name: '最低', type: 'line', data: series.low, showSymbol: false}]}, true);
  }

  async function backtest() {
    const button = document.getElementById('backtest-btn');
    return UI_UTILS.once('operation-points:backtest', async () => {
      button.disabled = true; setStatus('正在运行日线回测…');
      try {
        const response = await UI_UTILS.fetchJson('/api/operation-points/backtest', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: document.getElementById('code').value, config: payload()})});
        if (response.status !== 'success') throw new Error(response.error || '回测失败');
        const result = response.result; const diagnostics = result.diagnostics || {};
        document.getElementById('summary').innerHTML = `<div class="notice state-ok">回测完成：${result.trade_count} 笔交易，胜率 ${result.win_rate}%<br><small>数据区间 ${esc(result.data_start || '—')} ~ ${esc(result.data_end || '—')} · 有效判断区间 ${esc(result.effective_start || '—')} ~ ${esc(result.effective_end || '—')}</small></div><div class="op-grid"><div class="op-metric"><small>平均盈利</small><b>${result.average_profit}%</b></div><div class="op-metric"><small>平均亏损</small><b>${result.average_loss}%</b></div><div class="op-metric"><small>Profit Factor</small><b>${result.profit_factor ?? '—'}</b></div><div class="op-metric"><small>有效交易日</small><b>${diagnostics.eligible_days || 0}</b></div><div class="op-metric"><small>低位观察日</small><b>${diagnostics.watch_days || 0}</b></div><div class="op-metric"><small>止跌确认日</small><b>${diagnostics.stop_confirmed_days || 0}</b></div><div class="op-metric"><small>RR达标日</small><b>${diagnostics.rr_pass_days || 0}</b></div></div>`;
        setStatus('回测完成', 'state-ok');
      } catch (error) { setStatus('回测失败：' + error.message, 'state-no'); }
      finally { button.disabled = false; }
    });
  }

  window.analyze = analyze;
  window.backtest = backtest;
})();
