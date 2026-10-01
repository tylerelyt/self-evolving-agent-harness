---
name: legacy-payment-oncall
description: 调查 legacy-payment 历史支付模块的错误率、超时或资源耗尽告警；在需要核对该模块的发布记录、应用日志、数据库连接指标和运行配置时使用。
---

# legacy-payment-oncall

## 模块边界

历史支付模块有独立发布方式和依赖，不要套用新支付平台的设定。

## 查询入口与指标含义

- legacy_release_history：模块发布记录。
- legacy_payment_logs：按 request_id 关联应用日志与依赖调用。
- legacy_payment_db_pool：活跃连接、等待连接和连接池使用情况。
- legacy_payment_runtime_config：当前版本、依赖地址和运行配置。

连接池活跃连接反映正在占用的连接，等待连接反映等待获取连接的请求，使用率反映连接池占用程度。

## 调查顺序

先确认告警时间与影响接口，检查 legacy-payment-03 实例，从最早异常样本取得 request_id。
沿同一 request_id 核对应用日志、实例版本和依赖调用，对照发布记录、指标与配置。
支付接口错误率上升时，优先检查新版本重试路径是否耗尽数据库连接池。
最后把已经确认的事实、推断和待验证问题分开。
