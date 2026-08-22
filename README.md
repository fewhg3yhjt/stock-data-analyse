# StockInvestmentTool

A股股票投资分析工具包 —— 自动数据获取、技术/估值分析、策略方案、历史回测、LLM 智能分析、A股初筛、资金流分析、消息提醒、持仓管理与 Web 看板一体化的投资决策系统。

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [CLI 使用](#cli-使用)
- [Web 界面](#web-界面)
- [模块结构](#模块结构)
- [策略方案](#策略方案)
- [部署](#部署)
- [环境变量](#环境变量)
- [数据来源](#数据来源)
- [常见问题](#常见问题)

## 功能特性

- **一键分析**：数据获取 → 技术指标 → 估值分析 → 策略买入计划 → 策略回测 → Prompt 生成 → LLM 分析 → Markdown 报告 + 图表。
- **配置驱动策略**：买点/卖点/风控/回测参数全部外置为 YAML 策略方案（`schemes/*.yaml`），代码只提供原子执行能力，新增方案无需改代码。
- **多方案对比**：同一股票/区间跑多个方案，横向对比收益率、最大回撤、夏普、胜率。
- **历史回测**：网格搜索右侧移动止盈回撤阈值 × 买入偏移量，输出交易明细、权益曲线与绩效指标（收益率/回撤/夏普/Calmar/胜率）。
- **两代策略引擎共存**：v4.5（分批买入 + 左侧固定止盈 + 右侧移动止盈）与 V6.0（六态市场仲裁 + 逻辑/价格/时间止损 + 三层止盈）并行。
- **持仓管理**：建仓、交易流水（买卖/分红/纠错）、平仓、行情刷新 + 决策建议、模拟、复盘底账，数据落 SQLite。
- **A股初筛（screener）**：全市场按板块/名称/价格/PE/PB/市值/换手等规则初筛出目标股票集合。
- **资金流分析（fundflow）**：同花顺短线资金方向与趋势（行业/概念/个股 × 多周期）。
- **消息提醒（notifier）**：价格阈值、资金流信号、盘后汇总 → 飞书/企业微信机器人。
- **Web 界面**：三页看板（观察池 / 作战仓 / 复盘底账）+ 大盘行情 + 持仓 + 模拟 + 管理后台，与 CLI 共用同一分析引擎。
- **全量数据仓库（warehouse）**：离线全量采集全市场日线（baostock）+ Parquet 月分区存储 + 因子宽表 + DuckDB 全市场扫描；在线观察池盘中低频快照（腾讯，不封 IP）。2C2G/40GB 环境下按「离线全量 + 在线聚焦」设计，全量历史不到 3GB，内存峰值 <500MB。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量（DeepSeek 可选，用于 LLM 分析）
cp .env.example .env
# 编辑 .env，至少配置 DEEPSEEK_API_KEY

# 3. 一键分析（示例：长江电力，含回测）
python -m StockInvestmentTool --code sh.600900 --name 长江电力 --backtest --api

# 4. 启动 Web 界面
python -m StockInvestmentTool.web.app
# 浏览器访问 http://localhost:9000
```

## CLI 使用

### 一键分析

```bash
# 基础分析（近 1 年）
python -m StockInvestmentTool --code sh.600900 --name 长江电力

# 指定时间范围
python -m StockInvestmentTool --code sz.000001 --name 平安银行 --start 2024-01-01 --end 2024-12-31

# 回测 + LLM 分析（自动调用 DeepSeek）
python -m StockInvestmentTool --code sh.600900 --name 长江电力 --backtest --api

# 只生成 Prompt 文件，不调用 API
python -m StockInvestmentTool --code sh.600900 --name 长江电力 --prompt-only

# 跳过参数优化，使用固定止盈参数
python -m StockInvestmentTool --code sh.600900 --name 长江电力 --backtest --no-optimize --trail 0.05
```

### 策略方案

```bash
# 列出所有可用方案
python -m StockInvestmentTool --list-schemes

# 指定方案分析
python -m StockInvestmentTool --code sh.600900 --name 长江电力 --scheme aggressive_growth

# 多方案对比（至少 2 个，逗号分隔）
python -m StockInvestmentTool --code sh.600900 --name 长江电力 --compare default_value,aggressive_growth
```

### 持仓管理

```bash
# 建仓
python -m StockInvestmentTool portfolio add --code sh.600900 --name 长江电力 --shares 1000 --cost 28

# 持仓清单 / 详情
python -m StockInvestmentTool portfolio list
python -m StockInvestmentTool portfolio show 1

# 记录交易（买入/卖出/全部卖出/分红）
python -m StockInvestmentTool portfolio trans add 1 --type buy --price 28 --shares 1000

# 平仓
python -m StockInvestmentTool portfolio close 1 --price 35 --reason "止盈"

# 刷新行情 + 决策建议 / 编辑 / 删除 / 导出 Excel
python -m StockInvestmentTool portfolio refresh
python -m StockInvestmentTool portfolio edit 1 --notes "..."
python -m StockInvestmentTool portfolio delete 1
python -m StockInvestmentTool portfolio export
```

### 自选股 & 晨报

```bash
python -m StockInvestmentTool watchlist add --code sh.600900 --name 长江电力
python -m StockInvestmentTool watchlist list
python -m StockInvestmentTool watchlist delete 1

python -m StockInvestmentTool morning-report --refresh
```

### 独立子工具

```bash
# A股初筛
python -m StockInvestmentTool.screener --boards main_sh,main_sz --max-price 50 --export csv,json

# 资金流分析
python -m StockInvestmentTool.fundflow --sections industry,concept,stock --trend-days 3,5

# 消息提醒（测试/价格/资金流/盘后/持仓指令/全部）
python -m StockInvestmentTool.notifier --all
python -m StockInvestmentTool.notifier --test
```

### 全量数据仓库（warehouse）

```bash
# 首建：同步全市场代码清单 + 全量日线（默认近 3 年）
python -m StockInvestmentTool.warehouse init [--years 3] [--max-symbols N]

# 增量同步日线（每日收盘后跑一次，只补缺失日期）
# 增量同步日线（每日收盘后跑一次，只补缺失日期，自动断点续传）
python -m StockInvestmentTool.warehouse sync [--start YYYY-MM-DD] [--source tencent] [--target daily|raw:tencent]

# 计算全市场因子宽表（MA/量比/乖离/动量/波动率）
python -m StockInvestmentTool.warehouse factors

# 全市场因子扫描（DuckDB，按需读 parquet）
python -m StockInvestmentTool.warehouse scan --start 2026-08-01 --end 2026-08-20 \
    --where "vol_ratio > 2 AND ret_5d > 5" --limit 50

# 贴源层 → 加工层（raw/* 合并生成 daily 完整宽表）
python -m StockInvestmentTool.warehouse process [--months 2026-08]

# 单点回补历史 PE/PB（东财 stock_value_em，与 baostock 口径一致）
python -m StockInvestmentTool.warehouse backfill --codes sh600900,sh600519 --start 2023-08-21 --end 2026-08-21

# 观察池盘中低频快照（腾讯，不封 IP；默认保留 90 天）
python -m StockInvestmentTool.warehouse online [--codes sh600900,sz000001]

# 仓库状态 / 清空（破坏性）
python -m StockInvestmentTool.warehouse status
python -m StockInvestmentTool.warehouse reset [--kinds daily,factor,online]
```

数据落盘位置：`output/data/warehouse/`（`daily/` 与 `factors/` 按月分区 parquet，`raw/<源>/` 贴源层独立存放，`online/` 按日快照，`meta.db` 存标的清单与分区清单）。

**每日自动增量**：`run_warehouse_daily`（收盘后）按「标的自有最后日期」只补缺失区间（方案B），不会全量重跑；新股/缺失标的自动补 PE/PB。盘中实时快照由 `WAREHOUSE_ONLINE_SNAPSHOT=1` 开启，每 10 分钟一次。

## Web 界面

启动：`python -m StockInvestmentTool.web.app`，默认端口 `9000`（可用环境变量 `STOCK_WEB_PORT` 修改）。

主要页面：

| 菜单 | 路由 | 说明 |
|------|------|------|
| 大盘 | `/market` | 指数/板块 K 线、个股图表 |
| 观察 | `/dashboard/observe` | 观察池（自选 + 资金流候选） |
| 自选 | `/watchlist` | 自选股管理 |
| 模拟 | `/simulation` | 持仓目标模拟 |
| 持仓 | `/dashboard/warroom` | 作战仓（持仓 + 建议 + 基本面） |
| 复盘 | `/dashboard/review` | 复盘底账（胜率/盈亏比） |
| 管理 | `/settings` | 方案 YAML、初筛/通知规则、Webhook、登录、重置 |
| 分析 | `/` | 单股分析表单 |
| 对比 | `/compare` | 多方案对比 |

可选登录鉴权：`.env` 配置 `ADMIN_USER` / `ADMIN_PASSWORD` / `SECRET_KEY` 后启用。

内置 APScheduler 定时任务，默认每个交易日 15:35（Asia/Shanghai）运行（`DAILY_RUN_TIME` 可调，`DISABLE_SCHEDULER=1` 关闭）。

## 模块结构

```
StockInvestmentTool/
├── main.py                 # CLI 统一入口（分析 / 持仓 / 晨报 / 方案 / 对比）
├── config.py               # 全局配置（路径 / 数据源 / 默认参数 / LLM）
├── requirements.txt
├── Dockerfile              # 容器化（waitress 运行 web）
├── docker-compose.yml      # 容器编排（9000 端口 + 数据卷）
├── deploy-remote.sh        # 远程部署脚本
├── schemes/                # 策略方案 YAML（配置驱动策略）
│   ├── default_value.yaml      # 默认价值白马方案（v4.5）
│   ├── aggressive_growth.yaml  # 激进成长方案
│   └── v6_si_wei.yaml          # V6.0 四维一体实验方案
├── core/                   # 分析引擎与方案注册中心
│   ├── engine.py           #   统一分析引擎（CLI/Web 共用）
│   ├── registry.py         #   方案注册中心（YAML 扫描/加载/缓存）
│   └── scheme.py           #   方案数据模型（YAML→SchemeConfig）
├── datasource/             # 行情数据源层
│   ├── fetcher.py          #   baostock K线/基本面/分红 + AkShare 财务史
│   ├── indicators.py       #   技术指标 + 估值辅助（PE分位/三重锚/交叉支撑）
│   └── macro.py            #   V6.0 宏观数据（10Y债/ M2/ 沪深300 PE → ERP）
├── strategy/               # 策略决策模块
│   ├── multi_buy.py        #   分批买入（规则A + 规则C 趋势跟随）
│   ├── take_profit.py      #   止盈 + 网格搜索优化器（v4.5 回测核心）
│   ├── risk_control.py     #   风控（止损/回撤/仓位）
│   ├── stock_classifier.py #   股票类型分类（v4.5）
│   ├── market_state.py     #   市场状态（v4.5）
│   ├── position_sizing.py  #   仓位测算（类型×市场×乖离）
│   ├── buy_tree.py         #   V6.0 买入决策树（六态仲裁 + 5 前置）
│   ├── sell_tree_v6.py     #   V6.0 卖出决策树（逻辑/价格/时间/三层止盈）
│   └── v6_dispatch.py      #   V6 规则 type → 执行类注册中心
├── backtest/               # 回测
│   ├── engine.py           #   回测引擎（委托 TakeProfitOptimizer）
│   ├── engine_v6.py        #   V6.0 机械状态机回测引擎
│   └── metrics.py          #   绩效指标（收益率/回撤/夏普/Calmar/胜率）
├── comparison/             # 多方案对比（runner / report）
├── analysis/               # 报告与可视化 + V6.0 纯函数判定模块
│   ├── report.py           #   Markdown 报告生成
│   ├── charts.py           #   matplotlib/mplfinance 图表
│   ├── scorer.py           #   四维加权评分
│   ├── screener.py         #   红线/预警筛选（v4.5）
│   ├── stock_classifier_v6.py  # 类型决策树 A/B/C/D/E
│   ├── market_state_v6.py      # 六态市场状态
│   ├── macro_brake_v6.py       # 宏观上限熔断
│   ├── support_v6.py           # V6 支撑位（阵地/铁底）
│   └── screener_v6.py          # V6 排雷/盈利/筛选
├── screener/               # A股初筛（板块/规则/来源/导出）
├── fundflow/               # 资金流分析（同花顺）
├── notifier/               # 消息提醒（飞书/企微）
├── portfolio/              # 持仓管理 + 三页看板
│   ├── manager.py          #   持仓管理入口
│   ├── storage.py          #   SQLite 存储
│   ├── advisor.py          #   持仓决策引擎
│   ├── monitor.py          #   行情监控
│   ├── dashboard.py        #   三页看板数据服务
│   ├── reporter.py         #   晨报生成
│   ├── export.py           #   Excel 导入导出
│   └── settings.py         #   设置（方案/规则/Webhook 持久化）
├── prompt/                 # LLM Prompt 构建 + DeepSeek 客户端
│   ├── builder.py          #   Prompt 模板填充
│   ├── llm_client.py       #   DeepSeek API 调用（含流式）
│   └── templates/          #   股票/基金分析 Prompt 模板
├── warehouse/              # 全量数据仓库（离线全量 + 在线聚焦）
│   ├── storage.py          #   Parquet 月分区 + SQLite 元数据清单
│   ├── collector.py        #   baostock 全市场日线增量采集
│   ├── factors.py          #   因子宽表计算（月度分块）
│   ├── scanner.py          #   DuckDB 全市场扫描
│   ├── online.py           #   观察池盘中快照（腾讯）
│   └── cli.py              #   init/sync/factors/scan/online/reset/status
└── web/                    # Flask Web 前端
    ├── app.py              #   路由 + 蓝图
    ├── scheduler.py        #   定时任务（含可开关的数据仓库采集）
    └── templates/          #   Jinja2 模板（16 个页面）
```

## 策略方案

策略行为由 `schemes/*.yaml` 完全配置驱动，新增方案只需新增一个 YAML 文件（放入 `schemes/` 或 `schemes/custom/`），无需改代码。

### 内置方案

| 方案 | 版本 | 说明 | 适用类型 |
|------|------|------|----------|
| `default_value` | 1.0 | 基于支撑位交叉验证的三批买入 + 左侧固定止盈 + 右侧移动止盈（默认） | A/B/C/D |
| `aggressive_growth` | 1.0 | 弱支撑即重仓 + 规则C趋势跟随 + 宽右侧止盈 | A/B/C/D |
| `v6_si_wei` | 1.0 | V6.0 六态市场仲裁买入 + 逻辑/价格/时间止损 + 三层止盈（实验） | A/B/C/D/E |

### 方案结构

```yaml
name: default_value          # 方案名（唯一）
version: "1.0"
description: "..."           # 方案说明
applicable_types: [A, B, C, D]   # 适用股票类型
strategy_spec: {...}         # 策略说明书（纯文档，不参与执行）

buy_rules:                   # 买点规则（可叠加）
  - type: support_level      #   规则类型（决定执行类）
    params: {...}            #   规则参数

sell_rules:                  # 卖点规则（按优先级）
  - type: hard_stop
  - type: technical_stop
  - type: left_side_fixed
  - type: right_side_trailing

risk:                        # 风控参数
  stop_loss_by_type: {A: 0.15, B: 0.15, C: 0.15, D: 0.10}

backtest:                    # 回测参数
  initial_cash: 100000
  optimize: true
  grid_search:               # 网格搜索范围
    trail_thresholds: [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
    buy_offsets: [-0.02, 0.0, 0.02]
```

### 股票类型

- **A** 高成长 / **B** 价值白马（默认）/ **C** 强周期 / **D** 深度价值 / **E** 深度价值降级（V6）

## 部署

### Docker

```bash
# 构建并启动
docker compose up -d --build

# 停止（切勿使用 down -v，数据在挂载的 output/ 卷中）
docker compose down
```

- 服务：`stock-web`，端口 `9000:9000`
- 数据卷：`./output` → `/app/StockInvestmentTool/output`
- 环境变量文件：默认 `./.env`（可用 `STOCK_ENV_FILE` 覆盖）

### 远程部署

```bash
bash deploy-remote.sh <project> <package.tar.gz> [--dir]
```

支持代码包（云端构建）与镜像包（`docker load`）两种方式，自动保留 `.env` 与 `output/` 数据，并做健康检查。

## 环境变量

参考 `.env.example`，主要变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key（LLM 分析必填） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek 接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `STOCK_WEB_PORT` | `9000` | Web 端口 |
| `NOTIFY_CHANNEL` | — | 通知渠道（`feishu` / `wecom`） |
| `FEISHU_WEBHOOK_URL` | — | 飞书机器人 Webhook |
| `WECOM_WEBHOOK_URL` | — | 企业微信机器人 Webhook |
| `ADMIN_USER` / `ADMIN_PASSWORD` | — | Web 登录账号 / 密码 |
| `SECRET_KEY` | — | Flask 会话密钥 |
| `DAILY_RUN_TIME` | `15:35` | 每日定时任务时间 |
| `DISABLE_SCHEDULER` | — | `1` 时关闭定时任务 |
| `WAREHOUSE_DAILY_SYNC` | — | `1` 时每日收盘后自动做数据仓库离线采集（增量日线+因子+新股PE/PB回补，默认关闭） |
| `WAREHOUSE_YEARS` | `3` | 数据仓库历史深度（年），首建时决定全量回补长度 |
| `WAREHOUSE_ONLINE_SNAPSHOT` | — | `1` 时盘中每 10 分钟采集观察池实时快照（腾讯，默认关闭） |

## 数据来源

| 数据 | 来源 |
|------|------|
| K 线 / 基本面 / 分红 / 行业 | baostock |
| 财务历史（扣非/商誉/ROE/毛利率等） | AkShare（东财财务摘要） |
| 业绩快照（销售毛利率） | AkShare（业绩报表） |
| 资金流 | AkShare（同花顺，规避东财 IP 限制） |
| 实时行情批量报价 | 腾讯行情（qt.gtimg.cn） |
| 宏观（10Y 债 / M2 / 沪深300 PE） | AkShare |
| LLM 智能分析 | DeepSeek API |

数据缓存于 `output/data/`，K 线按股票缓存 CSV、分红/财务按 JSON 缓存，命中缓存时零网络请求。全量数据仓库（warehouse）另存于 `output/data/warehouse/`（Parquet 分区 + meta.db），全市场日线/因子来自 baostock，在线快照来自腾讯行情。

## 常见问题

- **baostock 连接失败 / 被限流**：内置连接自愈（空闲重连 + 失败退避 + 缓存降级）。持续失败时检查网络与登录频率。
- **数据来源全部失败**：screener 会返回退出码 1；分析流程会降级使用缓存数据（可能过期）并给出警告。
- **输出目录**：所有运行产物（报告/图表/数据/数据库）统一在 `output/` 下，已加入 `.gitignore`。
- **方案不生效**：确认 `--scheme` 名称与 `schemes/*.yaml` 的 `name` 一致，用 `--list-schemes` 查看可用方案。

## 许可证

私有项目。未经授权请勿用于商业用途。
