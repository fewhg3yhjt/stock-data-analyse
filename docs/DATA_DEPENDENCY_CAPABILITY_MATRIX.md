# 数据依赖与业务能力可用性矩阵 V1

## 1. 使用说明

本文用于界定“链路可以跑通”与“能力已经完整”的区别。

数据状态：

```text
可用       = 已有正式输入契约，可进入第一版
受限       = 有真实数据或读取能力，但覆盖/字段/质量有限，只能降级使用
未完成     = 不能作为第一版正式业务输入
```

业务状态：

```text
可用       = 第一版可以正式验收
降级       = 可以展示或研究，但必须显式标记限制
延期       = 等数据契约完成后再验收
```

## 2. 当前数据集状态

| 数据集 | 当前状态 | 可以依赖的事实 | 主要限制 |
|---|---|---|---|
| `stock_daily` | 可用 | OHLCV、amount、turn、trade status、date/code | `pre_close` 和估值字段不能无条件依赖 |
| `indicators` | 可用 | MA、RSI、MACD、ATR、动量、量比等实际存在列 | 部分配置指标不一定生成；定义版本绑定仍需补齐 |
| `valuation_daily` | 受限 | 有 PE/PB 产物 | `peTTM/pbMRQ` 与标准字段不一致，覆盖不足 |
| `fundamentals` | 未完成 | 部分财务字段可读 | schema、code、公告日和报告期不统一 |
| `industry_membership` | 未完成 | 有基础行业字段和少量 snapshot | 正式发布覆盖严重不足，分类有效期未定义 |
| `money_flow_security_daily` | 受限 | 有部分证券资金流 snapshot | 路径、周期、单位和覆盖契约未完全统一 |
| `money_flow_industry_daily` | 未完成 | 无稳定正式输入 | 证券/行业记录未完全拆分 |
| benchmark (`000300.SH`) | 可用 | 可复用 Published `stock_daily` | 必须确认基准数据覆盖和日期一致性 |

## 3. 业务能力矩阵

| 业务能力 | 依赖数据集 | 数据状态 | 第一版状态 | 验收边界 |
|---|---|---|---|---|
| 基础行情查看 | `stock_daily` | 可用 | 可用 | 只展示 OHLCV，带 `data_as_of` |
| K 线走势和基础指标 | `stock_daily` + `indicators` | 可用 | 可用 | 指标列缺失必须显式提示 |
| MA/ATR/动量选股 | `stock_daily` + `indicators` | 可用 | 可用 | 使用 Published 指标和统一条件 |
| 全市场量价筛选 | `stock_daily` + `indicators` | 可用 | 可用 | 动态窗口条件需走受控编译器 |
| 行业筛选 | `industry_membership` | 未完成 | 延期 | 完成正式行业覆盖前不作为完整验收项 |
| PE/PB 筛选 | `valuation_daily` | 受限 | 降级 | 仅展示有估值数据的标的，不能判定缺失为通过 |
| 单股技术研究 | `stock_daily` + `indicators` | 可用 | 可用 | 技术结论可正式验收 |
| 单股估值研究 | `valuation_daily` | 受限 | 降级 | 显示估值缺失/不可适用原因 |
| 单股基本面研究 | `fundamentals` | 未完成 | 延期 | schema 和公告日完成后验收 |
| 市场状态评估 | `stock_daily` + `indicators` | 可用 | 可用 | 统一 `MarketRegime`，记录算法版本 |
| 策略买卖判断 | `stock_daily` + `indicators` + `MarketRegime` | 可用 | 可用 | 不要求基本面硬依赖的第一版策略 |
| 基本面硬约束策略 | `fundamentals` + `stock_daily` | 未完成 | 延期 | 公告日安全和 schema 完成后再支持 |
| 单股回测/模拟 | `stock_daily` + `indicators` | 可用 | 可用 | 固定资金、成本、滑点和基准 |
| 含估值回测 | `valuation_daily` | 受限 | 降级 | 缺估值证券需明确跳过原因 |
| 含基本面回测 | `fundamentals` | 未完成 | 延期 | 不允许使用报告期但未到公告日的数据 |
| 观察池 | `ScreenCandidate` + `stock_daily` + `indicators` | 可用 | 可用 | 不依赖行业/基本面完整覆盖 |
| 模拟到建仓参考 | `SimulationResult` + `stock_daily` | 可用 | 可用 | 只生成计划，不生成真实成交 |
| 持仓估值 | `stock_daily` | 可用 | 可用 | 按实际行情日期估值 |
| 持仓技术建议 | `stock_daily` + `indicators` + `MarketRegime` | 可用 | 可用 | 不依赖基本面硬门禁的策略 |
| 持仓估值展示 | `valuation_daily` | 受限 | 降级 | 估值缺失单独标记 |
| 基本面持仓展示 | `fundamentals` | 未完成 | 延期 | 数据契约完成后再作为完整卡片 |
| 实际收益曲线 | `Execution` + `CashLedger` + `stock_daily` | 可用 | 可用 | 逐日重算，外部现金流独立 |
| 策略/基准对比 | `SimulationResult` + benchmark `stock_daily` | 可用 | 可用 | 相同区间和初始资金 |
| 行业超额对比 | `industry_membership` + 行业行情 | 未完成 | 延期 | 行业数据完成后再支持 |
| 资金流辅助选股 | `money_flow_security_daily` | 受限 | 降级 | 仅作为可选条件，带覆盖说明 |
| 资金流行业轮动 | `money_flow_industry_daily` + `industry_membership` | 未完成 | 延期 | 两个数据集都完成后支持 |
| 买卖邮件通知 | StrategyDecision + Advice + Email | 可用 | 可用 | 通知使用已生成决策，不重算策略 |
| 基本面触发通知 | `fundamentals` | 未完成 | 延期 | 基本面契约完成后支持 |

## 4. 第一版可以完整验收的闭环

```text
stock_daily
→ indicators
→ 指标条件筛选
→ 查看走势
→ 配置不依赖未完成数据的策略
→ 回测/模拟
→ 观察池
→ 真实成交录入
→ 持仓估值和收益
→ 策略/基准对比
→ 买卖邮件通知
```

## 5. 第一版不得伪装完整的能力

以下能力在对应数据集完成前，只能显示“受限”或“待数据完成”：

```text
全市场行业筛选
完整 PE/PB 因子策略
基本面硬约束策略
基本面历史回测
行业轮动
行业资金流
完整基本面持仓卡片
```

## 6. 统一验收规则

1. 业务能力验收必须引用本矩阵中的数据状态。
2. 数据状态为“未完成”的能力不能在总览中标记为 healthy/complete。
3. “降级”能力必须返回限制原因、数据日期和覆盖范围。
4. “延期”能力不得通过空值、旧字段或在线 fallback 强行验收。
5. 数据集状态变化后，相关业务能力状态必须重新计算。
