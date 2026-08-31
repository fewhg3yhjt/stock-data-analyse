/* StockInvestmentTool 统一状态词表（目标契约：UI 设计文档第 5.1 节）
   全局常量，页面只引用，不自行维护状态中文与颜色。 */
window.UI_STATUS = {
  healthy: {label: '正常', cls: 'healthy', tone: 'healthy'},
  stale: {label: '待更新', cls: 'stale', tone: 'partial'},
  partial: {label: '部分可用', cls: 'partial', tone: 'partial'},
  partial_success: {label: '部分可用', runLabel: '部分完成', cls: 'partial', tone: 'partial'},
  critical: {label: '异常', cls: 'critical', tone: 'critical'},
  failed: {label: '失败', runLabel: '执行失败', cls: 'failed', tone: 'critical'},
  running: {label: '运行中', cls: 'running', tone: 'running'},
  deferred: {label: '延期', cls: 'deferred', tone: 'unknown'},
  disabled: {label: '未启用', cls: 'disabled', tone: 'unknown'},
  empty: {label: '暂无数据', cls: 'empty', tone: 'unknown'},
  not_applicable: {label: '不适用', cls: 'not-applicable', tone: 'unknown'},
  unknown: {label: '状态未知', cls: 'unknown', tone: 'unknown'},
  ok: {label: '正常', cls: 'healthy', tone: 'healthy'},
  success: {label: '正常', runLabel: '已完成', cls: 'healthy', tone: 'healthy'},
  requested: {label: '等待执行', runLabel: '等待执行', cls: 'running', tone: 'running'},
  waiting_close: {label: '等待收盘', cls: 'unknown', tone: 'unknown'},
  cancelled: {label: '已取消', cls: 'unknown', tone: 'unknown'},
  unavailable: {label: '暂无数据', cls: 'empty', tone: 'unknown'},
  degraded: {label: '部分可用', cls: 'partial', tone: 'partial'},
};

window.UI_STATUS_LABEL = (key, context = 'default') => {
  const item = window.UI_STATUS[key] || {};
  return (context === 'run' ? item.runLabel : item.label) || item.label || (key || '状态未知');
};
window.UI_STATUS_CLS = key => (window.UI_STATUS[key] || {}).cls || 'unknown';
window.UI_STATUS_TONE = key => (window.UI_STATUS[key] || {}).tone || 'unknown';
