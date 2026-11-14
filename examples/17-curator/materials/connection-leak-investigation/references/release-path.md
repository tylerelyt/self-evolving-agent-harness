# 释放路径检查

固定集群、服务和时间，查明长期持有连接的请求。核对正常返回、异常抛出和提前返回是否都经过 finally 或上下文管理器释放连接。验证时分别制造成功与异常请求，确认每次结束后 active 回到起点，waiting 恢复；只看成功路径不够。缺少代码或持有时间数据时，报告尚不能确认泄漏。

## 诊断规则接口

以下规则描述条件判断、下一项检查和验证条件。整理时可以迁移文件或移入主文件，并更新入口引用；保留结构化规则的字段和值，便于执行者核对接口。只读计划不代表已经执行验证。

```json
{
  "schema": "connection-diagnostic-rule-v1",
  "capability": "leak",
  "when": {
    "connections_return_after_peak": false,
    "long_held_error_requests": true
  },
  "diagnosis": "suspected-leak",
  "next_check": {
    "action": "inspect_release_paths",
    "checks": [
      "normal_return",
      "exception",
      "early_return"
    ],
    "verification": [
      "success_request_returns_connection",
      "failed_request_returns_connection",
      "active_returns_to_baseline",
      "waiting_recovers"
    ]
  }
}
```
