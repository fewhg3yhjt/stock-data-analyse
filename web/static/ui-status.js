/* StockInvestmentTool 统一状态词表（设计文档 19.1）
   全局常量，页面只引用，不自行维护状态中文与颜色。 */
window.UI_STATUS = {
  healthy: {label: '正常', cls: 'healthy', tone: 'healthy'},
  stale: {label: '待更新', cls: 'stale', tone: 'partial'},
  partial: {label: '部分可用', cls: 'partial', tone: 'partial'},
  partial_success: {label: '部分可用', cls: 'partial', tone: 'partial'},
  critical: {label: '异常', cls: 'critical', tone: 'critical'},
  failed: {label: '失败', cls: 'failed', tone: 'critical'},
  running: {label: '运行中', cls: 'running', tone: 'running'},
  deferred: {label: '延期', cls: 'deferred', tone: 'unknown'},
  disabled: {label: '未启用', cls: 'disabled', tone: 'unknown'},
  empty: {label: '暂无数据', cls: 'empty', tone: 'unknown'},
  not_applicable: {label: '不适用', cls: 'not-applicable', tone: 'unknown'},
  unknown: {label: '状态未知', cls: 'unknown', tone: 'unknown'},
};

window.UI_STATUS_LABEL = key => (window.UI_STATUS[key] || {}).label || (key || '状态未知');
window.UI_STATUS_CLS = key => (window.UI_STATUS[key] || {}).cls || 'unknown';
window.UI_STATUS_TONE = key => (window.UI_STATUS[key] || {}).tone || 'unknown';
