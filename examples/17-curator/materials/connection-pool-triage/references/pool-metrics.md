# 连接池指标

构造教学说明。active 是已借出的连接，idle 是可借连接，waiting 是等待获取连接的请求。先固定集群、服务和时间，再与请求量比较。连接正常归还但峰值需求超过 max，优先验证容量与慢查询；请求量恢复后 active 仍高，继续检查持有时间和未关闭的连接。不要直接扩大连接池掩盖问题。

## 诊断规则接口

以下规则描述条件判断、下一项检查和验证条件。整理时可以迁移文件或移入主文件，并更新入口引用；保留结构化规则的字段和值，便于执行者核对接口。只读计划不代表已经执行验证。

```json
{
  "schema": "connection-diagnostic-rule-v1",
  "capability": "capacity",
  "when": {
    "demand_exceeds_limit": true,
    "connections_return_after_peak": true
  },
  "diagnosis": "capacity-pressure",
  "next_check": {
    "action": "inspect_capacity_and_slow_queries",
    "checks": [
      "peak_concurrency_vs_limit",
      "slow_query_duration"
    ],
    "verification": [
      "returned_connections_at_recovery",
      "waiting_clears_after_load_drop"
    ]
  }
}
```
