---
name: payment-investigation
description: 单笔足额支付只读调查，查清付款状态与仍未知部分。适用 payment_query 工具。
---
# 单笔足额支付只读调查

## 适用条件
- 仅限单笔足额支付（payment_mode=single-full）的调查
- 订单持续显示"支付中"或状态不明，需查清实际付款结果
- 不适用于分次付款、重新扣款、改订单或补发通知

## 只读范围
- 只查询、不修改：不扣款、不改订单、不补发通知
- 所有订单和数值均为构造值

## 工具
使用 payment_query，支持五个只读查询动作：
- help：查看字段含义与适用范围
- local：用订单号查本地流水
- channel：用渠道交易号查渠道付款最终状态
- platform：用订单号查平台付款结果
- notification：用订单号查商户通知回执

字段与查询说明详见 references/payment-query-fields.md

## 判断顺序

### 第一步：help
先调用 payment_query help 了解字段含义与适用范围。help 指明 request_result=success 是请求入口的成功返回，其业务含义需确认；channel 的 final_status 才是渠道付款状态。

### 第二步：local
用订单号查本地流水，取得 request_result、order_status、channel_transaction_id 等字段。

### 第三步：身份一致性核对（合并判断前提）
在跨来源比较付款状态前，先核对各来源的交易号、商户（merchant_id）、金额（amount）及单位（amount_unit）、币种（currency）是否一致。只有一致才能确认各来源记录的是同一笔交易，才能进行后续合并判断。

### 第四步：channel
用本地记录中的 channel_transaction_id 查渠道付款状态（final_status）。
- final_status=paid：渠道确认付款成功，继续判断平台与通知环节
- final_status=processing：渠道仍在处理，应停止，不能拿旧通知结果回答当前付款状态
- final_status=failed：渠道确认付款失败

### 第五步：platform（渠道确认 paid 后）
用订单号查平台付款结果（payment_status、order_status）。
- payment_status=paid 但 order_status≠paid：存在平台状态同步延迟或缺失，需标记为待确认

### 第六步：notification（渠道确认 paid 后）
用订单号查商户通知回执（notification_status、merchant_processing）。
- notification_status=timeout 且 merchant_processing=unknown：无法确认商户是否已收到并处理，需标记为待确认
- notification_status=timeout 不证明商户未处理，也不证明已处理

## 关键语义：受理成功 ≠ 最终付款
- request_result=success 仅代表请求入口受理成功，不等于付款完成
- 业务同学已确认：受理成功≠最终付款
- 判断付款是否成功应以渠道 final_status 和平台 payment_status 为准，不以 request_result 为准

## 未知时如何停止与核验
- 渠道 final_status=processing 时，停止后续判断，记录渠道仍在处理
- 记录缺失（某来源查不到）时，如实说明缺失，不凭其他来源推断
- 归属不一致（商户、金额、币种不匹配）时，停止合并判断，标记为非同笔
- 付款尚未终结时，保留待确认判断，不强行给出最终结论
- 本地 order_status 与渠道/平台付款结果不一致时，标记状态同步问题，需业务侧确认同步机制

## 报告要求
- 报告控制在 400 字内
- 列明各来源回执的关键字段值
- 区分"已可确认"与"仍待确认"
- 不向用户索要工具已能查到的材料
- 收到业务同学解释后继续查清当前订单，不只复述纠正