# 前端工程整改清单

> 文档性质：记录当前前端工程、安全与一致性问题，作为后续整改的验收依据。
>
> 与页面设计文档的关系：页面设计与信息架构见 `CORE_PAGES_UI_DESIGN_V1.md`；本清单只记录工程实现层面的问题，不重复页面布局设计。
>
> 已确认决策：
> - 涨跌/盈亏颜色语义统一为 **红涨绿跌**（A 股习惯）。
> - 静态资源合并/构建需要做，不跳过。

## 当前三层 UI 体系

| 壳层 | 前缀 | 页面 | 说明 |
|---|---|---|---|
| 工作台 | `wb-` | 工作台 | 独立壳层 |
| 数据模块 | `dm-` | 数据中心、数据资产、任务中心 | 独立壳层 |
| 业务页 | `.app` | 其余 30+ 页面 | 复用 `base.html` + `_nav.html`，靠 `ui-shell.css` 适配 |

公共样式 `base.css`、`ui-shell.css` 已抽出，但绝大多数页面仍在模板内联整套设计令牌与组件样式。

## P0：一致性与安全

### C-01 涨跌/盈亏颜色语义冲突

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| 同一数字在不同页面颜色相反 | 用户误读盈亏方向，投资判断被颜色误导 | 全站统一为“红涨绿跌”；盈利/上涨用红，亏损/下跌用绿；买卖方向标签与盈亏颜色分开定义 | `warroom.html`、`portfolio.html`、`position_detail.html`、`review.html`、`observe.html`、`stock-detail.js`、`index.html` | 同一盈亏字段在任何页面颜色一致；涨红跌绿；买卖方向标签不随盈亏颜色变化 |

### C-02 同一业务多种实现

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| 加仓/减仓/平仓在 warroom(`openOp`)、portfolio(`openTxn+prompt`)、position_detail(`execOp`) 各写一遍 | 逻辑重复，行为不一致，改动易漏 | 用 `data-*` 属性 + 事件绑定收敛为单一持仓操作组件 | `warroom.html`、`portfolio.html`、`position_detail.html` | 三处入口调用同一组件；操作按钮统一 disabled 与结果反馈 |

### C-03 toast 重复定义

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `base.html` 已有全局 `#toast`，但十几个模板又各自声明一个，样式/位置各不相同 | 全局 `ui-feedback.js` 与页面本地 toast 相互覆盖，反馈位置不一致 | 保留 `base.html` 唯一 `#toast`；删除页面重复定义；收敛 `showToast` 到公共文件 | `observe`、`warroom`、`review`、`portfolio`、`simulation`、`settings`、`notify_composer`、`watchlist`、`portfolio_add`、`strategy_composer` | 全站只有一个 `#toast`；`showToast` 只定义一次 |

### C-04 alert / confirm / prompt 不统一

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `alert()` 被 `ui-feedback.js` 全局改写为 toast；`confirm()`/`prompt()` 仍是原生弹窗；`watch_pool`、`indicator_center` 里的 `alert(d.error)` 现在无声变 toast | 用户看不到错误；确认/输入交互风格分裂 | 建立统一确认/输入面板；页面反馈统一走 `showUiMessage`；移除对 `window.alert` 的全局劫持 | `ui-feedback.js`、`watch_pool.html`、`indicator_center.html`、`market_discovery.html`、`portfolio.html` | 无全局 `alert` 劫持；确认/输入使用页面内组件；错误有明确可读文案 |

### C-05 观察类入口重叠

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `observe.html`、`watch_pool.html`、`watchlist.html`、`simulation.html` 功能交叉 | 用户无法判断正式入口；数据来源混乱 | 明确主入口为 `watch_pool`（观察池），旧页面只作过渡或重定向 | 上述四页面 | 用户导航只出现一个观察池主入口；旧入口有明确归并关系 |

### S-01 属性注入 / XSS

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| 多处把后端变量直接拼进 JS 字符串与 `onclick`，如 `observe.html:121`、`simulation.html:113`、`watchlist`、`portfolio.html:141-144`、`warroom.html:168-174` | Jinja 转义 `'` 为 `&#39;` 后 HTML 属性内被反转义为引号，股票名含引号即破坏 JS/注入 | JS 内插值统一走 `escapeAttr`；`onclick` 改 `data-*` + 事件绑定；股票名等用户数据不进内联事件 | `observe.html`、`simulation.html`、`watchlist.html`、`portfolio.html`、`warroom.html`、`position_detail.html` | 无后端变量直接拼进 `onclick`；含引号/特殊字符的股票名不破坏页面且不执行注入 |

### S-02 markdown 直渲 innerHTML

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `index.html`、`compare.html`、`morning_report.html` 用 `marked.parse` 后直接 `innerHTML` | 报告内容含股票名等数据，无净化则存在存储型 XSS 面 | 引入净化（如 DOMPurify）或转义渲染；禁止未净化的 markdown 直接写 `innerHTML` | `index.html`、`compare.html`、`morning_report.html` | 注入脚本不执行；报告内容只渲染为安全 HTML |

### S-03 未配置 CSP 安全头

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `app.py` `after_request` 仅记录信息，无 `Content-Security-Policy` 等安全头 | 无 CSP 时 XSS 影响面扩大 | 增加 `Content-Security-Policy`、`X-Content-Type-Options`、`X-Frame-Options` 等安全头；内联脚本如无法完全去除，先使用受限策略并记录 | `web/app.py` | 响应携带 CSP 安全头；页面功能不受影响 |

## P1：工程与维护

### E-01 公共 JS/CSS 五路分散

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `base.css` + `ui-shell.css` + `workbench.css` + `data-module.css` + `research-detail.css` + 每页内联 `<style>`；`index.html` 自带整套旧版设计系统（font-size 17px 与其他页 16px 不一致） | 主题化/暗色/一致性难以维护 | 收敛设计令牌到公共 CSS；删除页面重复内联 `<style>`；统一字号 | 各模板 | 页面不重复定义设计令牌；全站字号/间距一致；`base.css` 之外无整套并行设计系统 |

### E-02 公共 JS 函数逐页复制

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `showToast`/`esc`/`color`/`pctHtml` 在至少 10 个模板重复定义 | 改一处要全局同步 | 收敛到公共 JS（如 `ui-utils.js`），页面只引用 | `ui-feedback.js`、各模板 | 公共函数只定义一次；页面调用统一实现 |

### E-03 CDN 依赖混乱

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| echarts 本地有 1MB 文件，但 `market.html`/`strategy.html`/`market_discovery.html` 仍走 bootcdn；`observe`/`warroom`/`watchlist` 本地+CDN fallback 两种策略并存；`morning_report.html` 同时加载两个 marked 源；font-awesome 在 `base.html` 与 `login.html` 重复加载 | 内网/离线直接不可用；重复下载 | 统一 echarts、marked、font-awesome 走本地静态文件；删除 CDN fallback 和重复加载 | `market.html`、`strategy.html`、`market_discovery.html`、`observe.html`、`warroom.html`、`watchlist.html`、`morning_report.html`、`base.html`、`login.html` | 页面不依赖外网 CDN；无重复库加载；离线可访问 |

### E-04 静态资源版本手写

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| 资源引用靠手写 `?v=1`/`?v=11` | 更新易漏，浏览器缓存旧资源 | 统一版本管理；最终改为构建哈希或统一配置生成版本号 | 各模板静态引用 | 资源变更后浏览器能获取新版本 |

### E-05 大量内联 style

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| 全站大量内联 `style="..."`（warroom 一处 30+） | 无法主题化/暗色；样式不一致 | 逐步迁移到类名与公共 CSS | 各模板 | 页面主体样式不依赖内联 `style` |

### E-06 无前端构建、无模块化

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| 脚本裸函数堆叠，命名易冲突 | 维护困难，无法复用 | 评估静态资源合并/构建方案（轻量脚本合并或引入构建工具） | 静态资源目录 | 公共脚本有明确模块边界，命名不冲突 |

## P2：交互与潜在 Bug

### I-01 分析进度条为假进度

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| 后端维护真实 `_analysis_status.progress`，但 `index.html` fetch 完直接隐藏，未轮询 | 用户看到假进度，长任务无反馈 | 接真实进度轮询，展示 stage/progress，终态后停止 | `web/app.py`、`index.html` | 进度条反映后端真实 stage/progress；长任务完成前持续更新 |

### I-02 卖出计划以当前价替代年高

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `index.html:410` 把「年高」简化为当前价，回测输出被当前价锚定 | 数值可能误导 | 使用真实年高/最高价字段，或明确标注估算口径 | `index.html`、对应后端字段 | 卖出计划使用真实参考价；口径变更有明确说明 |

### I-03 SESSION_COOKIE_SECURE 与 http 直跑

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| `SESSION_COOKIE_SECURE=True`，但 `main()` 用 `http://127.0.0.1:9000` 直跑 | 非 HTTPS 环境下登录 cookie 可能无法回传 | 确认生产是否有 HTTPS 反代；直连环境禁用 secure 或走 HTTPS | `web/app.py` | 登录态在直连与 HTTPS 环境均正常 |

### I-04 stock-chart.js 实例缓存未释放

| 现状 | 风险 | 处理方案 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| ECharts 实例缓存以 `data-chart-id` 为键，动态复用时旧实例不释放 | 图表页多开有内存泄漏面 | 复用前 `dispose()` 旧实例 | `stock-chart.js` | 图表容器重建时旧实例被释放 |

## 整改执行顺序建议

```text
P0 一致性：C-01 颜色语义 → C-04 反馈统一 → C-05 观察入口
P0 安全：S-01 onclick XSS → S-02 markdown 净化 → S-03 CSP
P1 工程：E-01 样式收敛 → E-02 公共函数收敛 → E-03 CDN 本地化 → E-04 版本
P2 交互：I-01 真实进度 → I-02 年高口径 → I-03 cookie → I-04 图表释放
```

每项完成后运行：

```text
python3 -m pytest -q
全量页面 HTTP 冒烟
JavaScript 语法检查
```

并更新本清单的验收状态。
