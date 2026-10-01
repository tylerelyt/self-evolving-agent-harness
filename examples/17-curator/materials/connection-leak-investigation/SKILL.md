---
name: connection-leak-investigation
description: 连接池耗尽且流量恢复后连接仍未归还时，检查异常路径上的数据库连接释放。
---
# 连接泄漏诊断

构造教学方法，只读检查。先确认集群、服务、时间，比较 active、idle、waiting 与请求量，不能仅依据连接池已满判断泄漏。

如果请求量已恢复、连接持续被持有，按 [释放路径检查](references/release-path.md) 检查正常、异常和提前返回分支，再用小规模失败注入测试确认连接归还。不执行生产写操作。
