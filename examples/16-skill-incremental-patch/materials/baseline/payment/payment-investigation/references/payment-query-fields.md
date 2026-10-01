# payment_query 字段与查询说明

> 所有订单和数值均为构造值，仅供流程演示。

## 查询动作

| 动作 | 标识 | 说明 |
|------|------|------|
| help | 可省略 | 查看字段含义与适用范围 |
| local | 订单号 | 查本地流水 |
| channel | 渠道交易号 | 用渠道交易号查渠道付款最终状态 |
| platform | 订单号 | 查平台付款结果 |
| notification | 订单号 | 查商户通知回执 |

## 关键字段语义（来源：help 回执）

- **request_result**：本地流水字段，`success` 表示请求入口成功返回。help 明确其业务含义需确认。业务同学已纠正：受理成功≠最终付款，该字段不能用于判断付款是否完成。
- **final_status**：渠道字段，值为 `paid` / `processing` / `failed`，表示渠道付款最终状态。
- **payment_status**：平台字段，表示平台认定的付款结果。
- **order_status**：本地和平台均有此字段，表示订单状态（如 `paying`）。
- **notification_status**：通知字段，`timeout` 只说明当前通知回执超时，不证明商户未处理。
- **merchant_processing**：通知字段，`unknown` 表示无法确认商户是否已处理。

## 身份一致性字段

跨来源比较付款状态前，需核对以下字段确认各来源记录同一笔交易：

- **merchant_id**：商户标识
- **amount + amount_unit**：金额与单位（如 6800 / fen = 68.00 元）
- **currency**：币种（如 CNY）
- **channel_transaction_id**：渠道交易号，local 与 channel 之间靠此关联

只有以上字段一致时，才能合并判断各来源的付款状态。

## 合并判断前提

1. 各来源 merchant_id、amount、amount_unit、currency 一致 → 确认为同笔
2. 渠道 final_status=paid → 渠道确认付款成功，可继续判断平台与通知
3. 渠道 final_status=processing → 停止，渠道仍在处理，不拿旧通知结果回答当前付款状态
4. 渠道 final_status=failed → 渠道确认付款失败