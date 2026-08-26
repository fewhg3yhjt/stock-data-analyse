# StockInvestmentTool 后续开发执行指南

> 文档性质：项目内部执行手册，供后续 AI 模型或开发者直接接手使用。
> 更新时间：2026-08-26
> 适用仓库：`/opt/stock_data_analyse`
> 生产地址：`https://stock.easyconnect.ltd/`
> 注意：本文包含真实项目路径和生产环境信息，仅限项目内部使用，勿对外传播。

## 1. 文档目标

本文件不是产品介绍，也不是对现有实现的宣传材料。它的目标是让一个没有当前会话上下文的后续模型能够：

1. 正确理解项目产品定位和真实架构；
2. 区分“已创建抽象”与“生产主链路已迁移”；
3. 按风险和依赖顺序继续开发；
4. 每完成一阶段都有明确、可执行的验收标准；
5. 不破坏现有持仓数据、回测语义和生产服务；
6. 避免重复出现“测试通过但线上关键功能不可用”的情况。

开始工作前必须同时阅读：

- [需求规格 SRD](SRD.md)
- [概要设计 HLD](HLD.md)
- [现状盘点 STATUS](STATUS.md)
- [总体设计 DESIGN](DESIGN.md)
- [项目 README](../README.md)

其中 `STATUS.md` 第九节对 FR-1 至 FR-5 的完成判断偏乐观。后续执行以本文的代码审计结论和 `SRD.md` 的逐条验收标准为准，不得仅凭模块文件存在或单元测试通过宣布完成。

## 2. 产品定位和核心闭环

StockInvestmentTool 是一个单用户 A 股投资工作台，不是自动交易系统。核心产品闭环为：

```text
市场数据
  -> 指标和估值
  -> 个股分析
  -> 策略及买入计划
  -> 历史回测
  -> 观察池/自选/模拟
  -> 实际持仓管理
  -> 盘中/盘后建议
  -> 通知
  -> 交易复盘
```

当前最成熟的产品能力是：

- 个股分析和既有方案回测；
- 观察池、自选、模拟和实际持仓管理；
- 交易记录、FIFO 盈亏和复盘；
- Parquet + SQLite + DuckDB 数据仓库；
- 单用户生产部署和日常调度。

当前不应将系统描述为：

- 可自动执行交易；
- 可作为唯一止损通知来源；
- 支持多用户或多租户；
- 所有可视化配置都已被真实引擎执行；
- 已完成生产级安全、备份和一致性保障。

## 3. 生产环境约束

### 3.1 部署拓扑

```text
Internet
  -> Caddy :443
  -> stock.easyconnect.ltd
  -> localhost:9000
  -> Docker 容器 stock-invest
  -> Waitress/Flask
```

生产仓库就是当前工作目录：

```text
/opt/stock_data_analyse
```

代码通过 bind mount 挂入容器：

```text
/opt/stock_data_analyse -> /app/StockInvestmentTool
```

纯代码修改后只需要重启，不要重建镜像：

```bash
sudo docker compose restart stock-web
```

只有修改 `requirements.txt` 或 `Dockerfile` 时才允许重新构建。

### 3.2 数据安全红线

- 不得执行 `docker compose down -v`；
- 不得删除或覆盖 `output/`；
- 不得删除 `portfolio.db`、仓库分区或运行时方案；
- 任何清库、重置、批量删除操作都必须先说明会丢失什么，并得到用户明确确认；
- 不得为了让测试通过而清理生产数据；
- 测试必须使用临时目录、临时 SQLite 数据库和 mock 数据源；
- 不得把 `.env`、密码、密钥或真实通知地址提交到 Git。

### 3.3 常用验证

```bash
python3 -m pytest tests/ -q
sudo docker compose restart stock-web
sudo docker inspect stock-invest --format '{{.State.Health.Status}}'
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:9000/
curl -s -o /dev/null -w 'HTTP %{http_code}\n' https://stock.easyconnect.ltd/
```

未登录时返回 `302` 跳转 `/login` 属于正常结果。

## 4. 当前真实完成度

### 4.1 总体判断

| 维度 | 当前判断 |
|------|----------|
| 产品功能完整度 | 约 72%，核心个人投资流程基本形成 |
| SRD FR-1 至 FR-5 | 约 55%，多项仅完成抽象或局部接入 |
| 架构成熟度 | 约 52%，处于新旧架构并存期 |
| 开发可用度 | 7.5/10 |
| 受控自用/Beta | 5/10 |
| 严格生产就绪度 | 2.5/10 |

### 4.2 FR 实际状态

| 需求 | 实际状态 | 后续模型必须知道的事实 |
|------|----------|--------------------------|
| FR-1.1 RuleRegistry | 部分完成 | 10 个规则已注册，但主要策略、回测和 advisor 仍直接查找规则 |
| FR-1.2 支撑位 | 大部分完成 | 三条路径已复用共同骨架 |
| FR-1.2 状态机 | 未接入 | `PositionStateMachine` 主要只有实现和单测，没有生产调用 |
| FR-1.3 IndicatorContext | 部分完成 | 新上下文存在，但旧指标系统仍是主要执行路径之一 |
| FR-1.4 DataSource | 部分完成 | monitor 和部分图表已接入，engine/dashboard 仍有具体数据源直连 |
| 指标中心 | 第一版完成 | `/indicator-center` 已支持指标浏览、表达式预览、自定义 base/composite 指标 CRUD；代码指标和内置指标保持只读 |
| FR-2 策略编排器 | 部分完成 | API 和页面存在，核心 list/map-list 参数及语义校验不完整 |
| FR-3 通知编排器 | 部分完成 | 配置和调度存在，但条件参数、AND/OR、indicator 条件没有完整执行 |
| FR-4 UI 统一 | 少量完成 | `base.html/base.css` 存在，绝大部分页面仍未继承公共模板 |
| FR-5 DuckDB 图表 | 基本完成 | 主图表路径已改善，但没有性能基线和 P95 证据 |

完成定义必须是：

```text
生产主链路使用新实现
  + 对应行为测试通过
  + 旧重复路径删除或明确停用
  + 页面关键元素和用户流程验收通过
  + 生产冒烟通过
```

只满足“新增了类/文件/API”或“单测通过”不能标记完成。

## 5. 第一项立即任务：首页导航回归

### 5.1 用户可见问题

打开生产首页：

```text
https://stock.easyconnect.ltd/
```

页面只显示：

```text
A 股股票分析 · 策略回测 · AI 判读
```

用户看不到市场、观察、自选、模拟、策略、持仓、复盘和管理入口，因此会误以为系统只剩一个页面。

其他路由大部分仍存在，问题是首页导航没有渲染，不是功能或数据丢失。

### 5.2 已定位根因

`web/templates/_nav.html` 使用：

```jinja2
{% if active %}
  ...整个导航...
{% endif %}
```

而 `web/templates/index.html` 直接 include 导航，没有设置 `active`：

```jinja2
{% include '_nav.html' %}
```

因此首页的 `active` 未定义，整个导航被隐藏。

另外还存在：

- 导航中没有明确的“个股分析/首页”入口；
- `strategy_composer.html` 错误设置为 `active='settings'`；
- `notify_composer.html` 错误设置为 `active='settings'`；
- 之前只检查页面 HTTP 200，没有检查关键导航元素，导致回归漏检。

### 5.3 修复要求

1. `_nav.html` 在 `active` 未定义时也必须渲染；
2. 增加首页“分析”入口，并为首页设置 `active='analyze'`；
3. 策略编排器使用 `active='composer'`；
4. 通知中心使用 `active='notify'`；
5. 所有核心页面都必须显示公共导航；
6. 桌面和移动端不能因为导航过宽导致关键入口完全不可达；
7. 不要只修首页一行而保留 `_nav.html` 的脆弱整体条件。

### 5.4 验收标准

- 登录后访问 `/` 能看到“分析、市场、观察、自选、模拟、策略、编排器、通知、持仓、复盘、管理”等入口；
- `/` 的“分析”入口处于 active 状态；
- `/strategy-composer` 的“编排器”处于 active 状态；
- `/notify-center` 的“通知”处于 active 状态；
- `/market`、`/dashboard/observe`、`/watchlist`、`/simulation`、`/strategy`、`/dashboard/warroom`、`/dashboard/review`、`/settings` 均包含导航；
- 新增模板回归测试，断言导航链接和 active class，而非只断言 HTTP 200；
- 完整测试通过；
- 重启容器后，通过生产域名人工/HTTP 冒烟确认。

## 6. 后续修复顺序

后续模型必须按以下顺序工作。除非用户明确改变优先级，不要先增加新页面、新指标或新规则。

### 阶段 A：生产可信性 P0

目标：先保证系统不会给出虚假回测结论、不会丢财务状态、不会静默丢通知。

#### A1. 修复 v4.5 `trail_threshold` 无效

重点文件：

- `strategy/take_profit.py`
- `backtest/engine.py`

已发现：优化器搜索 `trail_threshold`，但右侧卖出判断使用 `self._right_drawdown`，搜索参数可能只影响说明文字。

要求：

- 先写失败测试，构造适合触发追踪止盈的数据；
- 证明不同阈值在适合的数据上产生不同卖出时点或结果；
- 明确方案配置与网格搜索参数谁具有优先级；
- 修复后保存内置方案回测基线，避免后续静默漂移。

#### A2. 资金、持仓、交易原子化

重点文件：

- `portfolio/storage.py`
- `portfolio/manager.py`

要求：

- 一个业务动作使用同一个 SQLite connection/transaction；
- position、transaction、cash、advice 的相关写入要么全部成功，要么全部回滚；
- 现金更新使用原子 SQL，不能读改写覆盖；
- 资金不足应明确失败，不得静默将负数截断为 0；
- 增加每个中间步骤故障注入测试；
- 不允许用生产 `portfolio.db` 跑测试。

#### A3. 通知投递可靠性

重点文件：

- `web/scheduler.py`
- `notifier/core.py`
- `notifier/channels.py`

要求：

- 只有渠道确认成功后才能写去重状态；
- 失败通知保持可重试；
- 至少实现有界重试和错误记录；
- scheduler 必须检查 notifier 返回状态，不能失败也记录 `sent`；
- 增加“发送失败不去重、重试成功后去重”的测试。

#### A4. 安全加固

重点文件：

- `web/app.py`
- `core/scheme_store.py`
- `portfolio/settings.py`
- `datasource/base.py`
- `.env` 和 `docs/HANDOVER.md` 的运维处理

要求：

- 轮换已经出现在文档中的生产密码；
- 轮换弱 `SECRET_KEY`；
- 文档不得保存真实凭据；
- `.env` 和生产数据库权限收紧；
- 生产环境缺少密码或安全密钥时 fail closed，不得自动关闭鉴权；
- 增加 CSRF、登录限流和安全 Cookie；
- `next` 只能跳站内地址；
- scheme 名称只允许严格安全字符，并验证 resolved path 位于目标目录；
- DuckDB 请求参数必须参数化或在边界严格验证；
- 任意 webhook 测试 URL 必须限制协议、主机和私网地址；
- 报告和图表应鉴权，或改为短期签名链接。

此阶段涉及真实密码轮换、权限或生产配置时，执行前必须向用户说明影响并获得明确确认。

#### A5. 数据备份和恢复

要求：

- 建立 `portfolio.db`、`warehouse/meta.db`、运行时方案和 `.env` 的加密离机备份；
- 明确 Parquet 数据哪些需要备份、哪些可重建；
- 定义保留周期；
- 至少完成一次恢复演练；
- 数据重置前自动创建可恢复快照；
- 不得把备份密钥放进仓库。

### 阶段 B：让目标架构接管生产路径 P1

#### B1. RuleRegistry 成为唯一规则派发入口

重点文件：

- `strategy/rule_registry.py`
- `strategy/rule_builtin.py`
- `strategy/multi_buy.py`
- `strategy/take_profit.py`
- `backtest/engine_v6.py`
- `portfolio/advisor.py`

验收：

- 生产代码中不再按字符串直接 `find_buy_rule("...")` 或 `find_sell_rule("...")`；
- scheme 中每条规则都通过 registry 派发；
- executor 收到该条规则真实 `params`，不能忽略参数后重新从 scheme 查找；
- 新增测试规则无需修改现有引擎即可被分析、回测和 advisor 执行；
- 三个内置方案结果与确认过的基线一致。

#### B2. PositionStateMachine 成为唯一状态入口

重点文件：

- `strategy/position_state.py`
- `portfolio/manager.py`
- `portfolio/advisor.py`
- `strategy/take_profit.py`
- `backtest/engine_v6.py`

验收：

- 状态转移只在状态机定义；
- manager、advisor、v4.5、V6 共用同一事件和转移表；
- 非法转移明确失败；
- 实盘和回测相同上下文得到相同阶段变化。

#### B3. IndicatorContext 成为统一指标入口

重点文件：

- `indicators/context.py`
- `indicators/engine.py`
- `datasource/indicators.py`
- `strategy/support.py`
- `strategy/multi_buy.py`
- `core/engine.py`
- `portfolio/advisor.py`

验收：

- `MIN(MA20,MA240)`、`0.95*MA20` 等表达式在真实买入、回测和 advisor 路径执行；
- `RowContext` 不得对不支持的表达式静默返回 0；
- 缺失指标和错误表达式必须包含指标名并明确报错；
- year_high 的窗口、最小样本和短历史行为完全一致；
- 旧 YAML 兼容。

#### B4. DataSource 完整收敛

重点文件：

- `datasource/base.py`
- `core/engine.py`
- `portfolio/monitor.py`
- `portfolio/dashboard.py`
- `web/app.py`

验收：

- 业务层通过接口获得日线、快照、基本面等需要的数据；
- 去除 `PriceMonitor._fetch_from_warehouse()` 等重复死代码；
- 上层不再直接选择 Warehouse/AkShare/Tencent/Baostock；
- 数据源可在测试中替换；
- 图表和指标分区查询均使用投影、过滤后的 DuckDB 查询，不全量 pandas 扫描。

#### B5. 通知模型真正执行

重点文件：

- `notifier/triggers.py`
- `web/scheduler.py`
- `notifier/core.py`
- `web/templates/notify_composer.html`

验收：

- action 的 advice type 精确过滤；
- indicator 阈值、上穿、下穿可执行；
- price_change 使用当前触发器自身参数；
- 多条件 AND/OR 结果正确；
- 最近一次触发预览可用；
- `instant` 立即发送，`batch` 合并到同一批次；
- 旧通知 CLI 与新管线合并或明确停用，避免重复发送。

### 阶段 C：产品化补齐 P1/P2

#### C1. 策略编排器

要求：

- 完整实现 list、map-list、map 等结构化编辑；
- `support_sources` 和 `buy_stages` 可可靠生成；
- 阈值支持“系数 × 指标”；
- 表单变更实时刷新 YAML；
- 同一 rule schema 同时用于 UI、服务端校验和运行时；
- 校验 required、min/max、枚举、指标存在性、比例总和和规则可执行性；
- 保存前执行样本行情冒烟；
- UI 补齐版本历史、diff、回滚、设默认和发布状态；
- 策略实验室可以直接选择并运行编排器方案。

#### C2. UI 和信息架构

要求：

- 所有主要模板继承 `base.html`；
- 公共 token、按钮、表单、表格、toast、导航移至 `base.css`；
- 删除重复 inline 基础样式，只保留真正页面特有规则；
- 导航按“每日工作、研究分析、系统配置”重新组织；
- 个股分析结果提供观察、模拟、建仓的明确下一步；
- 桌面和移动端做实际浏览器验证；
- 页面测试验证关键 DOM，而不是只验证 HTTP 200。

#### C3. Web 模块拆分

建议按 Blueprint 拆分：

```text
web/routes/auth.py
web/routes/analysis.py
web/routes/portfolio.py
web/routes/market.py
web/routes/schemes.py
web/routes/notifications.py
web/routes/settings.py
```

拆分必须保持路由和行为兼容，不要在同一提交中同时进行大规模业务改写。

### 阶段 D：工程治理 P2

- 增加 CI：Python 3.11 测试、lint、类型检查、secret scan、依赖扫描、Docker smoke；
- 使用 constraints/lock 固定生产依赖；
- scheduler 独立进程或增加可靠的单实例锁；
- 增加任务执行台账、通知投递台账和数据新鲜度指标；
- 增加 SQLite WAL、busy timeout、foreign key 和启动完整性检查；
- Parquet 使用临时文件、校验后原子替换；
- Docker 改为非 root，减少可写 bind mount，限制 9000 暴露范围；
- 建立可验证的版本发布和回滚流程。

## 7. 测试策略

### 7.1 当前测试的正确理解

当前约 50 个测试主要覆盖：

- RuleRegistry 独立注册和派发；
- IndicatorContext 独立求值；
- 支撑位骨架；
- 独立 PositionStateMachine；
- DataSource fallback；
- 策略编排器 API；
- 通知配置和局部聚合。

它们不能证明：

- 真实引擎使用 RuleRegistry；
- 真实持仓使用 PositionStateMachine；
- 组合指标在真实策略中有效；
- 财务写入具有原子性；
- 通知失败后可以重试；
- UI 核心入口一定可见。

### 7.2 后续必须增加的测试层次

```text
单元测试
  -> 领域集成测试
  -> Flask 路由/模板测试
  -> 数据库事务测试
  -> 调度与通知可靠性测试
  -> Docker/生产冒烟
```

每个修复至少包括：

1. 一个能在旧实现上失败的回归测试；
2. 修复后的目标行为断言；
3. 对邻近旧行为的兼容性测试；
4. 完整测试集；
5. 必要的生产冒烟。

### 7.3 关键测试清单

- 首页及所有核心页面导航存在且 active 正确；
- `trail_threshold` 改变真实卖出行为；
- 三内置方案固定基线回归；
- 新注册规则能被真实分析、回测、advisor 执行；
- 相同上下文下 advisor 与 backtest 决策一致；
- 组合指标表达式在真实策略链路执行；
- position/transaction/cash 任一步失败全部回滚；
- 并发现金调整不丢更新；
- 通知失败不去重，成功后才去重；
- AND/OR、上穿/下穿和价格阈值正确；
- scheduler 不重复启动、不重叠执行；
- scheme 路径穿越被拒绝；
- 未授权和 CSRF 请求不能修改数据；
- 备份能够恢复到干净环境。

## 8. 后续模型工作流程

每次开始任务时按以下步骤执行。

### 8.1 建立事实

1. 读取本文件及相关 SRD/HLD 条目；
2. 搜索所有生产调用点，不得只读新模块；
3. 检查 git status，保留用户或其他模型的未提交改动；
4. 确认任务是否涉及生产配置、真实数据或破坏性操作；
5. 先写出当前行为、目标行为和验收方式。

### 8.2 实施原则

- 选择最小正确改动；
- 先写失败测试，再修改实现；
- 不增加无必要的兼容层；
- 不在一次提交中混合安全、重构和功能改动；
- 新抽象必须接入生产调用方，不能只新增未使用类；
- 遇到未知行为先用测试固定，不凭印象重写投资算法；
- 不得静默吞掉指标或规则错误并返回 0/空结果；
- 对金融状态、通知状态和配置写入优先保证原子性和可恢复性。

### 8.3 验证和提交

完成一个可独立验收的单元后：

1. 运行相关测试；
2. 运行完整测试；
3. 检查 `git diff` 和 `git status`；
4. 只暂存本次相关文件；
5. 提交一个职责明确的 commit；
6. 纯代码改动重启 `stock-web`；
7. 验证容器 health；
8. 通过本地端口和生产域名验证；
9. 对 UI 改动检查关键 DOM/文本，不得只看状态码；
10. 更新本文的阶段状态和 `STATUS.md`，状态必须与真实主链路一致。

## 9. 禁止事项

- 禁止以“创建了文件/类/API”作为功能完成证据；
- 禁止仅凭 50 个测试通过判断生产功能完成；
- 禁止未经基线验证修改投资算法语义；
- 禁止删除或重置生产数据以解决测试问题；
- 禁止把真实密码、API key、SMTP 密码、Webhook 写入文档或提交；
- 禁止 `docker compose down -v`；
- 禁止纯代码改动无意义 rebuild 镜像；
- 禁止让多个 Web worker 各自启动 scheduler；
- 禁止通知投递失败后仍记录成功或已去重；
- 禁止指标表达式错误时静默返回 0；
- 禁止直接拼接用户输入形成文件路径或 DuckDB SQL；
- 禁止大规模重构和行为修复混在一个提交中。

## 10. 阶段完成判定模板

后续模型汇报完成时必须使用类似格式：

```text
任务：FR-x.x / 缺陷名称

生产路径：
- 修改前由哪些文件/函数执行
- 修改后统一由哪个入口执行
- 已删除或停用哪些旧路径

行为证据：
- 新增哪些失败回归测试
- 哪些固定基线保持一致
- 哪些新行为已验证

运行证据：
- 相关测试结果
- 完整测试结果
- 容器健康状态
- 本地 URL 冒烟
- 生产域名冒烟

风险与剩余项：
- 尚未覆盖什么
- 是否涉及数据迁移或配置轮换
```

不得只汇报“已新增某模块、测试通过”。

## 11. 推荐的第一个执行批次

后续模型接手后，建议第一个批次严格控制范围，只完成首页导航回归：

1. 修复 `_nav.html` 的整体隐藏条件；
2. 增加首页分析入口和正确 active；
3. 修正 composer/notify active；
4. 增加核心页面导航模板测试；
5. 运行完整测试；
6. 单独提交；
7. 重启生产容器；
8. 通过 `https://stock.easyconnect.ltd/` 验证导航真实可见。

这一批完成后，再进入阶段 A 的回测正确性和数据一致性修复。不要将首页修复与架构重构捆绑在同一提交。

## 12. 最终目标

项目下一阶段不应继续以“增加更多功能”为第一目标，而应完成可信化改造：

```text
页面可达
  -> 回测正确
  -> 财务一致
  -> 通知可靠
  -> 安全可控
  -> 新架构接管主链路
  -> 配置真正产品化
  -> UI 和运维统一
```

达到上述目标后，StockInvestmentTool 才能从“功能丰富的个人投资工具”升级为“可信赖、可持续演进的个人投资系统”。
