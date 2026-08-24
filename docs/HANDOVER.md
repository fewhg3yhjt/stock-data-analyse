# StockInvestmentTool 会话交接文档

> 本文档用于会话衔接：上一个会话（2026-08-23）的工作成果 + 待办清单，
> 下一个会话据此继续，避免重复摸索。

## 一、项目现状（一句话）

A股分析工具，生产环境直接迭代（Docker 容器 `stock-invest`，代码挂载，改代码 `sudo docker compose restart stock-web` 生效），数据仓库分层已建成。

## 二、运行环境（重要）

- **生产服务器**：`VM-0-4-ubuntu`，本机即生产
- **仓库**：`/opt/stock_data_analyse`
- **容器**：`stock-invest`（镜像 stock-invest:latest），端口 9000，域名 `https://stock.easyconnect.ltd`
- **代码挂载**：改代码 → `sudo docker compose restart stock-web` 生效（无需 rebuild）
- **登录**：`ADMIN_PASSWORD=1qazXSW@`（.env）
- **数据层**：`output/data/warehouse/`
- **依赖注意**：改 `requirements.txt` 才需 rebuild；字体已内置 `fonts/`（容器 matplotlib 中文）

## 三、架构分层

```
应用层 web/          页面/API/定时任务/管理台
业务层 portfolio/   持仓/观察池/建议/通知
      strategy/    策略决策
      notifier/    邮件通知(盘中10min+盘后15:35,可配置)
指标层 indicators/  可配置/可组合/可编程指标体系
数据层 warehouse/   raw贴源 → daily加工 → indicators指标
                    → fundamentals基本面 → factors因子 → online快照
                    → meta.db(标的清单含行业)
```

设计文档：`docs/DESIGN.md`

## 四、已完成的模块

| 模块 | 文件 | 说明 |
|------|------|------|
| 指标体系 | `indicators/engine.py` | 基础/组合/代码指标 + 表达式引擎 |
| 指标配置 | `schemes/indicators.yaml` | 分层配置 |
| 指标批量 | `warehouse/indicators_build.py` | 全市场指标宽表（37月/6435只）|
| 基本面采集 | `warehouse/fundamentals_collect.py` | 行业(meta.db)+财务史(fundamentals分区)|
| 基本面存储 | `warehouse/storage.py` | fundamentals 分区读写 |
| 策略实验室 | `strategy_lab.py` | 扫描/回测/ECharts交互+指标勾选 |
| P2 持仓操作 | warroom + `POST /portfolio/<id>/delete` | 加仓/减仓/分红/删除弹窗 |
| P3 个股折线图 | `stock_chart_series` + `POST /api/chart/stock` | 日/周/月+收益率双线 |
| P4 账户历史+输入 | warroom历史 + `POST /api/stock/lookup` | 时间维度流水+编号自动关联 |
| 邮件通知 | notifier(EmailSender) + notify_settings.yaml | 盘中/盘后可配置+去重24h+外部URL图 |

## 五、数据获取状态

| 数据 | 状态 | 位置 |
|------|------|------|
| 全市场日线 | ✅ 6435只×3年 | warehouse/daily/ |
| PE/PB 回补 | ✅ 4799只 | daily（东财）|
| 全量指标 | ✅ 6435只 37月 | warehouse/indicators/ |
| 财务史 | ✅ 4551只 | warehouse/fundamentals/ |
| 行业 | ⚠️ 仅持仓+自选22只 | meta.db instruments.industry |
| 000开头深市股票 | ❌ 缺失（待补）| meta.db/daily/fundamentals 均缺 |

## 六、关键问题/注意事项

1. **000 开头深市主板股票缺失**（平安银行 sz000001 等）：
   - 根因：原 `detect_type` 把 000 开头误判 index，导致没采集
   - 已修复 `detect_type`（`datasource/fetcher.py`），但**需重拉**这些股票的 daily/行业/财务史
   - 需 baostock/sina 网络稳定时跑增量同步

2. **baostock 连接不稳定**：
   - 行业接口慢（每只 1-10s），全量采集不可行，当前只采持仓+自选
   - `query_all_stock` 周末返回空（用最近交易日）
   - 后台线程 signal 不可用，靠 socket timeout（已处理）

3. **ETF/指数无财务史**：采集器已跳过（fundamentals_collect.py）

4. **全量指标生成的 months 统计**：已修复（实际数据完整，37月/6435只）

## 七、待办清单（下个会话继续）

### 🔴 高优先级
1. **补拉 000 开头深市主板股票**：网络稳定时跑增量同步（`sync_daily`），补 daily + 行业 + 财务史
2. **全面梳理数据源统一走数据层**：检查 `portfolio/reporter.py`、`dashboard.py` 等是否还有散落实时拉取（baostock/akshare），统一前置到 warehouse

### 🟡 中优先级
3. 页面所有按钮交互反馈（loading/成功/失败提示）
4. 行业全量采集（找高效数据源，替代慢的 baostock 行业接口）
5. 观察池打开慢优化（缓存/并行）

### 🟢 低优先级
6. 策略验证的历史资金流数据（fundflow 历史未接入）
7. 指数/ETF 的估值类分析（当前无 PE/PB，daily 是 NaN）

## 八、常用命令

```bash
# 部署（改代码后）
cd /opt/stock_data_analyse && sudo docker compose restart stock-web

# 采集
python -m StockInvestmentTool.warehouse sync       # 增量日线
python -m StockInvestmentTool.warehouse factors    # 因子
python -m StockInvestmentTool.warehouse fundamentals --kind all  # 行业+财务史
python -m StockInvestmentTool.warehouse status     # 状态

# 通知
python -m StockInvestmentTool.notifier --actionable --dry-run  # 测试操作提醒
```

## 九、验证方式

- 登录后访问 `http://127.0.0.1:9000/` 各页面
- API 用登录态 curl：先 `curl -c cookie -d "username=admin&password=1qazXSW@" /login`
- classify 应从数据层秒回（<0.2s），若 >10s 说明走了网络（数据缺失）