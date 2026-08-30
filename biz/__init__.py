# -*- coding: utf-8 -*-
"""新业务平台（biz）包。

依据 docs/NEW_SYSTEM_STORAGE_DESIGN.md 与 docs/DOMAIN_MODEL_AND_CONTRACTS.md 实现。
业务平面与数据平面分离：本包只消费 DatasetAccess 提供的 Published Dataset，
不使用旧 portfolio/strategy/notifier 领域模型，不读取 Raw / daily 原始目录。
"""