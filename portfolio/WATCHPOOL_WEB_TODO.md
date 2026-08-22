# 观察池「两框」Web 呈现待办清单

> 后端数据已打通（`DashboardService.stock_dual_view` 返回天周期历史 + 盘中快照）。
> 以下为 web 端呈现的待办，统一后续实现。

## 背景
统一观察池 = 持仓 ∪ 自选 ∪ 策略选入（`portfolio.watchlist` 表，`source` 字段区分来源）。
每只观察股票应有「两个框」：
- **框1 天周期历史**：`warehouse/daily` 的日线/因子历史（趋势、支撑压力、策略验证）
- **框2 盘中快照**：`warehouse/online` 当日快照（实时价/换手/量比/资金，感知盘中博弈）

## 待办项

### 1. 观察池页改版（`/dashboard/observe`）
- [ ] 观察池列表每只股票展示来源徽标（manual 手动 / holding 持仓 / strategy 策略）
- [ ] 点击某只 → 展开/跳转双视图：天周期 K 线图（框1）+ 盘中实时数据（框2）
- [ ] 框1：K 线（复用 `analysis.charts`）+ 支撑/压力位叠加 + 因子指标
- [ ] 框2：实时价/涨跌/换手/量比/PE/PB/成交额 + 盘中资金流信号

### 2. 双视图数据接口
- [ ] web 路由：`/api/stock/dual-view?code=sh600900`（调 `stock_dual_view`）
- [ ] 前端按需拉取（点击时请求，不一次性全量加载）

### 3. 策略选入可视化
- [ ] 观察池里区分展示"策略选中"来源，注明是哪条策略（当前：资金流持续流入）
- [ ] 策略候选过期/变化时，前端提示（旧候选保留但标注日期）

### 4. 盘中快照历史
- [ ] 框2 支持查看当日多时点快照（09:30/10:00/.../15:00 盘中曲线）
- [ ] 观察某只股票盘中资金流向变化（快照序列对比）

### 5. 代码格式统一（数据层遗留）
- [ ] 观察池里历史数据的 code 格式不一致（`sz.002415` 带点 vs `601899` 不带点）
  - 现有 `StockDataFetcher.normalize_code` 能兼容读取，但展示时应统一为无点格式
  - 建议观察池展示层统一 normalize，避免前端处理两种格式

## 后端已就绪（无需重复开发）
- `DashboardService.stock_dual_view(code)` → 天周期历史 + 盘中快照
- `warehouse/online.py` 统一观察池读取（持仓∪自选∪策略）
- `portfolio/manager.sync_strategy_candidates()` 策略选入