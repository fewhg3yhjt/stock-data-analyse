# 股票消息提醒模块 notifier

价格阈值 / 资金流信号 / 每日盘后汇总 → 推到**自己的**群机器人
（企业微信 或 飞书，消息直达你的 App，不经任何第三方中转）。

## 准备（一次性）

1. **飞书**：注册飞书 → 建一个群（可只含自己）→ 群设置 → 添加「自定义机器人」→ 复制 webhook URL
   （或企业微信：建群 → 添加「群机器人」→ 复制 webhook）
2. 在 `StockInvestmentTool/.env` 配置：
   ```
   NOTIFY_CHANNEL=feishu                      # feishu | wecom
   FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
   # WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
   ```
3. 测试：
   ```bash
   python -m StockInvestmentTool.notifier --test
   ```

## 使用

```bash
python -m StockInvestmentTool.notifier --price     # 检查自选股价格阈值
python -m StockInvestmentTool.notifier --fundflow  # 资金流信号提醒
python -m StockInvestmentTool.notifier --daily     # 每日盘后汇总
python -m StockInvestmentTool.notifier --orders    # 作战仓今日持仓指令
python -m StockInvestmentTool.notifier --all       # 全部（price+fundflow+daily+orders）
python -m StockInvestmentTool.notifier --all --dry-run   # 只打印不推送
```

提醒规则见 `notifier/rules.yaml`：
- `watchlist`：自选股 + `price_above/price_below`（价格突破/跌破）+ `change_pct`（单日涨跌幅）
- `fundflow`：持续流入榜 / 背离警示（价涨钱走）/ 净额阈值过滤
- `daily`：盘后资金流概览 + 行业净流入/流出 TOP5

## Windows 计划任务（每日定时）

开始菜单搜索「任务计划程序」→ 创建任务：
- 触发器：每天 15:35（收盘后）
- 操作：程序 `python`，参数 `-m StockInvestmentTool.notifier --all`，起始位置填 repo 根目录
- 条件：取消「只有在计算机使用交流电源时才启动」（笔记本会省电跳跑）

## 数据流

```
watchlist → 腾讯批量报价(实时价) → 阈值比对 → 触发消息
fundflow  → 同花顺资金流(即时+3日) → 持续流入/背离信号
daily     → 大盘概况 + 行业资金流 TOP → 汇总消息
        └→ 群机器人 webhook（飞书/企微）
```

## 目录结构

```
notifier/
├── channels.py   # 渠道适配器（企微/飞书 webhook）
├── notify.py     # 规则加载 + 消息构造 + 推送
├── cli.py        # 命令行入口
├── __main__.py
└── rules.yaml    # 提醒规则（自选股/资金流/每日）
```

测试: `PYTHONPATH=repo根 python -m pytest StockInvestmentTool/tests/test_notifier.py`（离线，不真发消息）。
