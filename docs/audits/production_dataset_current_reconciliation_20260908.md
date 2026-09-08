# Production Dataset Current Reconciliation

> 审计日期：2026-09-08
> 审计性质：阶段 0 只读基线
> 审计范围：`management.db.dataset_current`、对应 `dataset_versions`、Published 文件和 checksum
> 结论：未修改任何生产数据；本报告不代表所有数据集已完成业务正式验收。

## Method

管理库中的 Published 路径使用容器路径 `/app/StockInvestmentTool`。由于生产 Compose 将宿主机 `/opt/stock_data_analyse` 挂载到该路径，审计时将此前缀映射回宿主机后进行文件和 SHA-256 校验。部分文件宿主机权限不足，因此使用只读 `sudo` 完成校验。

## Summary

所有 `dataset_current` 记录均满足以下静态一致性条件：

- 对应 `dataset_versions` 记录存在
- `publish_status = published`
- `quality_status` 为 `PASS` 或 `WARNING`
- 容器路径映射到的宿主机文件存在
- 文件 SHA-256 与版本记录 checksum 一致

| Dataset | Current partitions | Published | Quality OK | File OK | Checksum OK |
|---|---:|---:|---:|---:|---:|
| `stock_daily` | 38 | 38 | 38 | 38 | 38 |
| `indicators` | 38 | 38 | 38 | 38 | 38 |
| `industry_daily` | 33 | 33 | 33 | 33 | 33 |
| `industry_features_daily` | 1 | 1 | 1 | 1 | 1 |
| `industry_rotation_daily` | 1 | 1 | 1 | 1 | 1 |
| `industry_membership` | 1 | 1 | 1 | 1 | 1 |
| `ths_industry_membership` | 1 | 1 | 1 | 1 | 1 |
| `fundamentals` | 4574 | 4574 | 4574 | 4574 | 4574 |
| `valuation_daily` | 37 | 37 | 37 | 37 | 37 |
| `valuation_snapshot` | 1 | 1 | 1 | 1 | 1 |
| `industry` | 1 | 1 | 1 | 1 | 1 |
| `money_flow_daily` | 1 | 1 | 1 | 1 | 1 |

合计：`4727` 个 Current 分区，静态状态和文件 checksum 未发现不一致。

## Data Freshness Notes

- `stock_daily` 当前最新 Published 分区为 `2026-09`，版本元数据最大日期为 `2026-09-04`。
- `industry_daily` 当前最新 Published 分区为 `2026-09`，最近发布记录为 `2026-09-07`。
- `industry_rotation_daily` 当前 Published 版本最大数据日期为 `2026-09-04`。
- 静态 Current/checksum 一致不等于数据日期完整，也不等于任务持续成功；日期新鲜度和任务闭环需要单独验收。

## Findings

1. Published 文件、Current 指针和 checksum 在容器挂载路径映射后是一致的。
2. 不能使用宿主机直接读取容器绝对路径的结果判断文件丢失；路径映射是当前部署契约的一部分。
3. `stock_daily` 最近任务持续失败，但已有旧 Published 版本仍然可读，符合“失败不覆盖旧 Current”的安全边界。
4. `fundamentals` 存在大量 Published 单标的版本，但 Web 股票分类入口仍有缺失后在线采集路径，不能据此宣布 fundamentals 已达到 Published-only。

## Next Evidence

- 生成包含任务 Run、Source Batch、Version、Quality、Current 和 as_of 的逐任务链路报告。
- Data Worker 接管后重新核验同一套静态一致性，并增加失败版本不推进 Current 的演练记录。
