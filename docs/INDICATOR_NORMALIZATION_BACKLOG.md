# 指标归一后续改造清单

> 本文记录「指标口径归一」改造中识别出的、**暂不在本次范围**或**需谨慎处理**的遗留项。
> 原则：统一指标命名（ma5/ma20/ma60/ma240）、统一计算引擎（IndicatorRegistry）、
> 唯一消费入口（indicators 分区 + IndicatorContext）。以下为待后续处理的边界项。

## 1. 数据源契约层（peTTM/pbMRQ 保留）

统一数据源契约 `datasource/base.py::KLINE_COLUMNS` 已改为 `pe_ttm/pb_mrq`，
且 WarehouseSource（仓库）与 OnlineSource（在线）均已映射为标准列。

以下**外部源原始字段**保留（baostock/腾讯接口原始命名，非内部口径）：

- `warehouse/collector.py:30` `_DAILY_FIELDS`（baostock 查询字段，含 peTTM/pbMRQ）
- `warehouse/collector.py:36` `_KEEP_COLS`（已为标准 pe_ttm/pb_mrq，保留）
- `warehouse/raw.py:43`（Raw 贴源层，保留原始命名）
- `config/datasets/stock_daily.yaml:104` `field_mapping`（外部→标准映射）
- `config.py:45` `BAOSTOCK_DEFAULT_FIELDS`（外部查询字段）
- `datasource/fetcher.py`（在线拉取，保留 baostock 原始列）
- `scripts/generate_pipeline_validation_data.py:56`（模拟外部源输出）

这些是**外部源契约**，与内部指标口径无关，不归并。若未来统一在线/离线数据源
完整契约，可另立改造项。

## 2. 数据接管迁移层（allow_legacy / legacy_daily）

`allow_legacy` 是历史数据接管阶段的读取开关，`legacy_daily` 是旧日线文件适配，
`warehouse/datasets.py` 的 `_load_legacy` 是旧路径兼容读取。这些属于
「数据管线可信链路」接管范畴（见 `docs/DATA_PIPELINE_STATEFLOW_REMEDIATION_PLAN.md`），
**不属于指标口径**。接管完成、确认无旧数据依赖后，可移除：

- `warehouse/datasets.py` 的 `allow_legacy` 参数与 `_load_legacy`
- `warehouse/indicators_build.py` / `warehouse/factors.py` 的 `allow_legacy` 分支
- `warehouse/daily_build.py` 的 `legacy_daily` 源
- `scripts/convert_legacy_daily.py`
- `warehouse/legacy_adapters.py`（接管后如需保留适配能力可保留）

## 3. 因子宽表去 MA（已完成，注意存量重建）

`warehouse/factors.py` 已移除 `ma5/ma10/ma20/ma60/ma120` 列（研究因子宽表不再存
技术指标），`bias_ratio` 改为基于 `ma240`（内部算，不落盘）。

**存量影响**：`factors/*.parquet` 需在下次 `rebuild_factors` 时整体重建，否则
旧分区仍含 ma 列。重建前 `market_discovery`/`scanner` 查询不受影响（已列级容错）。

## 4. 年线口径统一为 ma240

V6 市场状态/年线熔断/止盈硬上限已全部改读 `ma240`（240 交易日，非 250）。
`config/metrics/catalog.yaml` 中 `bias_ratio` 已改为「MA240乖离率」。

**注意**：`market_state_v6.py` 的 detail key `ma240_direction` / `price_above_ma240`
为 API 返回字段，前端无消费；如未来展示需同步命名。

## 5. 操作点策略（已完成零自算，注意数据源）

`strategy/operation_points.py::add_indicators` 已删除 ma5/ma20/ma60/atr14 自算，
改为从统一指标层读取（`market_discovery.service::stock_frame_with_indicators`
合并 daily + indicators 分区）。`atr14` 已入 catalog（indicators 分区产出）。

**注意**：
- 策略私有衍生（ma20_slope/center_shift/ma_distance/cross/position20/volume_ratio_n）
  仍在本文件内计算——它们是**决策中间量**，非通用指标，不纳入 catalog。
- 若 `indicators` 分区未重建（缺 atr14 列），操作点接口会报「缺少统一指标列」。

## 6. 其余候选（后续评估）

- `config/datasets/indicators.yaml` 中 `custom_example`（示例代码指标）可在正式化后移除
- `docs/HLD.md` / `docs/DATA_PIPELINE_V1_DESIGN.md` 中关于 ma_250 / volume_ma_5 的
  历史表述需在文档更新时同步
- `tests/conftest.py::make_kline` 用 `compute_all` 产出 round(2) 列；若统一 float64
  口径，需确认测试断言（当前已通过，因 IndicatorContext 引擎结果覆盖 df 列）

## 7. 回测基线重录

舍入口径统一为 float64 全精度（不 round）后，`backtest/engine_v6.py` 及
V4.5 回测结果需重录基线并比对（SRD 验收标准要求逐字段一致）。

## 8. 已完成的收尾项（2026-08-30）

- `analysis/returns.py::build_snapshot_chart`（邮件内嵌快照图）原先自算
  ma20/ma60，已改为复用统一指标层（`compute_all` 已产出，缺失时引擎现算）。
  至此全仓 MA 类技术指标自算已清零，其余自算点均为高低点/绩效/策略中间量
  （year_high/low_6m/回测绩效），不属于指标层。
- 冒烟验证：临时仓库真实链路（daily + indicators 分区 → 合并读取），
  引擎与 indicators 分区结果逐位一致（float64 口径），`pe_ttm/pb_mrq` 读取正常。

## 9. compute_all 下线硬编码，指标全部入引擎（2026-08-30）

`TechnicalIndicators.compute_all` 原先硬编码 `calc_change/calc_amplitude/calc_ma/
calc_volume_ma/calc_rolling_lows/calc_bias_ratio` 一套运行时指标。现改为**统一走
IndicatorRegistry**：

- 引擎新增注册：`pct_chg`、`change_amount`、`amplitude`、`vol_ma5`、`low_3m`、
  `year_low`、`bias_ratio`（均为中性指标，对 stock/etf 适用）
- `compute_all` 重写：批量调 `IndicatorRegistry.compute` 产出全部指标列，
  删除 `calc_change/calc_amplitude/calc_ma/calc_volume_ma/calc_rolling_lows/
  calc_bias_ratio` 硬编码方法（`ma_slope/trend_judgment/support_resistance/
  annualized_volatility` 为判定/辅助函数，保留）
- catalog + `datasets/indicators.yaml` 补登记 5 个新 indicators 指标
  （`change_amount/amplitude/vol_ma5/low_3m/year_low`）；
  `pct_chg/bias_ratio` 保持 factors 生产（研究因子），引擎注册同名仅供运行时消费
- 维持指标中性：全部指标对 stock/etf 统一计算，不按类别裁剪；
  `applies_to` 仅作任务调度范围控制（index 不跑 indicators_build），非指标级过滤

**消费兼容**：`compute_all` 产出列名不变（`change_pct` = 引擎 `pct_chg` 别名，
`vol_ma5/low_3m/year_low/bias_ratio` 同名），advisor/market_state/回测零改动。