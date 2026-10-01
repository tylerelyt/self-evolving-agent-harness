---
name: legacy-payment-oncall
description: 调查 legacy-payment 历史支付模块的错误率、超时或资源耗尽告警；在需要核对该模块的发布记录、应用日志、数据库连接指标和运行配置时使用。
---

# legacy-payment-oncall

## 任务边界

- 确认调查对象是 legacy-payment 模块。
- 不要套用新支付平台的 Canary、服务网格或 Sidecar 背景。
- 历史事故只能提供检查线索，不能作为当前根因。

## 调查步骤

1. 确认告警开始时间、影响接口、实例和版本范围。
2. 用 legacy_payment_logs 找到最早的异常样本并取得 request_id。
3. 用 legacy_release_history 对照第一条错误与模块发布记录。
4. 用 legacy_payment_db_pool 核对活跃连接、等待连接和连接池使用情况。
5. 用 legacy_payment_runtime_config 重新读取当前版本、依赖地址和运行配置。
6. 说明每项材料支持、削弱或尚未涉及哪项判断。
7. 输出已经确认的事实、当前推断、未决问题和下一步验证方式。
