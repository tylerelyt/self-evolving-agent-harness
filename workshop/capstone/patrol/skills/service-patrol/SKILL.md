---
name: service-patrol
description: 抽查指定服务的 HTTP 窗口，按需检索历史并留下可复查的巡检记录。
---

# 例行巡检

1. 用 `service_snapshot` 读取分配的服务与当前窗口。请求失败时报告 `unavailable`。历史读数只供对照。
2. 请求少于 10 个时判 `insufficient`。样本足够时，5xx 比例达到 5% 或 P95 达到 500ms 就判 `abnormal`，其余判 `healthy`。
3. 有历史可对照时，用 `search_inspections` 查询同一服务和环境的近期巡检。只引用实际查到的记录。
4. 输出一个 JSON 对象，含 `status`、`history_id` 和中文 `summary`。`history_id` 是引用记录的整数 ID，无引用时填 `null`。摘要写清请求数、5xx 比例、P95 及判断依据。

如果本回合收到 `ONCALL_ALERT`，执行通知指定的 On-call Skill。保留上述巡检字段，并添加该 Skill 要求的初判字段。
