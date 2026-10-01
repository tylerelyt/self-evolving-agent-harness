---
name: legacy-payment-oncall
description: 调查历史支付模块的超时、错误率和数据库连接池异常。需要核对该模块的发布、旧日志、连接池与运行配置时使用。索引标记 {{META}}。
---

# 历史支付模块调查

方法正文标记：{{BODY}}。本练习的时间、实例名、请求编号和数值均为构造值。

只调查 legacy-payment 模块，不套用新支付平台的设定，不沿用旧事故根因。

1. 读取 `inputs/alert.md`，确认告警范围、时间、影响接口与实例。
2. 调查到旧日志时，先读取相对于本文件的 `references/log-fields.md`，再读取 `inputs/legacy.log`，从最早异常取得 request_id，沿同一编号关联依赖调用。
3. 读取 `inputs/release.md`、`inputs/pool.md`、`inputs/config.md`，相互核对发布、连接池指标和当前配置；区分事实、推断和未决问题。
4. 调查结束、准备形成报告时，读取相对于本文件的 `assets/incident-report.md`，把报告写入工作目录的 `report.md`；引用具体文件行号。
5. 报告写完，运行相对于本文件的 `scripts/check_report.py`，第一个参数为报告路径。正常执行无需读取源码，依据返回结果说明哪些条目缺引用。

本次 Python 解释器为 `{{PYTHON}}`；工作目录就是本次实验项目。不得修改输入材料、Skill 或运行配置，不读取项目外的资料，不访问网络。
检查程序只验证引用格式，不能证明引用的内容支持结论。
