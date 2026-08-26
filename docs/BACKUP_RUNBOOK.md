# StockInvestmentTool 数据备份 Runbook

> 内部运维文档，仅限本机/项目维护者使用。不要把备份目录、`.env` 或备份内容提交到 Git。

## 备份范围

备份工具会复制：

- `output/data/portfolio.db`：持仓、交易、现金和建议；使用 SQLite backup API；
- `output/data/warehouse/`：离线 Parquet、在线快照和分钟数据；
- `.env`：运行所需密钥和配置，必须加密保存；
- `schemes/custom/`：用户方案、指标和版本；
- `notifier/notify_rules.yaml`：通知触发器配置。

源目录不会删除或修改。备份目标必须是项目目录之外的空目录。

## 手动备份

在宿主机执行，目标目录建议放到独立磁盘或离机同步目录：

```bash
python3 scripts/backup_data.py /var/backups/stock-invest/$(date +%F-%H%M%S)
```

备份完成后必须：

1. 对备份目录加密；
2. 复制到另一台机器或对象存储；
3. 限制备份权限；
4. 保留最近 7 个日备份和最近 4 个周备份；
5. 定期执行恢复演练。

## 恢复演练

恢复演练必须在临时目录或隔离主机执行，不得覆盖生产 `output/`：

1. 复制备份到隔离目录；
2. 检查两个 SQLite 数据库；
3. 用 `PRAGMA quick_check` 验证完整性；
4. 启动临时应用，验证登录、持仓列表、方案列表和分钟详情；
5. 记录恢复耗时和缺失文件；
6. 删除临时恢复目录前，确认没有需要保留的证据。

## 重要边界

- 备份不是恢复；必须至少每季度做一次恢复演练；
- `output/` 是生产数据挂载目录，禁止 `docker compose down -v`；
- 密码、API key、SMTP 密码和 webhook 不得写入仓库文档；
- 当前脚本不会自动加密或上传备份，操作员必须补齐这两步；
- 任何生产恢复、覆盖或清理操作都需要先确认影响范围。
