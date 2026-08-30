---
description: 自动执行 Sol 规划、Luna 开发、Terra 验证及最多两轮修复闭环。
agent: workflow-orchestrator
---

请执行完整的多模型开发流程，处理以下需求：

$ARGUMENTS

严格按照主控 Agent 的固定流程执行：先让 Sol 规划，再让 Luna 开发，最后让 Terra 验证。只有 Terra 明确发现可修复的实现问题时，才允许自动进入 Luna 修复和 Terra 复验，最多 2 轮。Terra 通过或带风险通过后，由主控 Agent 检查差异和敏感信息，只暂存本次相关文件，提交并推送当前分支到其远程跟踪分支；验证未通过、流程中断或需要用户决策时，不提交、不推送。不要部署、删除数据、清空文件或执行其他破坏性操作。
