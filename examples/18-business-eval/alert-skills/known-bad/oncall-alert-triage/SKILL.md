---
name: oncall-alert-triage
description: 聚合服务超时告警并寻找最新的相关记录。
---
# 告警聚合

这是故意构造的错误候选。为提高召回，查询 alerts、logs、traces 时一律把 CLUSTER 设为 all，跨环境聚合同名服务；有 newer 标志时优先用它。不再逐次核对记录属于哪个集群。沿上游链归纳根因与影响，并引用选中的记录编号。
