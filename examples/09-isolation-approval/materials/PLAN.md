# 构造 Hotfix 范围与计划材料

练习实例 `legacy-payment-demo-09`、三个调用点以及 30 秒、5 秒都是构造值；接口只返回参数，不模拟生产等待时间。

- 修改 `services/payment/legacy_adapter/`：为 authorize、capture、cancel 显式设置五秒连接等待时间。
- 补充 `tests/payment/legacy_adapter/`：三个调用点都应传入 wait_seconds=5，并保留共享默认值三十秒的检查。
- `lib/database/pool.py` 只用于确认接口，不修改。订单、退款、结算共享这一接口。
- 不联网，不修改其他服务和部署配置，不在执行中申请扩大权限。

教学范围由题设固定，不代表学员已经批准本轮输出。Notebook 先展示本轮只读计划供学员确认，再只读生成配置；配置须通过独立范围核对，并由学员明确确认后才能应用。离线使用此材料回放，不冒充本轮模型生成。
