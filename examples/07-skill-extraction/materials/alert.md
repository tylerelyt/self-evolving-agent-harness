# 两周后的当前告警（教学案例）

2026-04-15 09:00 起，legacy-payment 历史支付模块的 /payment 接口大量超时，影响实例 legacy-payment-07；版本范围需要重新核对。

模块独立发布、独立配置，不使用新支付平台的 Canary、服务网格或 Sidecar。
四个只读查询入口为 legacy_release_history、legacy_payment_logs、legacy_payment_db_pool、legacy_payment_runtime_config。
用当前 Python 运行 current/query.py，后接入口名称；日志可按 --request-id 关联，--limit 20 取得最早二十条异常样本。
