# 已确认的构造 Hotfix 计划

练习实例 `legacy-payment-demo-09`、三个调用点以及 30 秒、5 秒都是构造值；接口只返回参数，不模拟生产等待时间。

- 修改 `services/payment/legacy_adapter/`：为 authorize、capture、cancel 显式设置五秒连接等待时间。
- 补充 `tests/payment/legacy_adapter/`：三个调用点都应传入 wait_seconds=5，并保留共享默认值三十秒的检查。
- `lib/database/pool.py` 只用于确认接口，不修改。订单、退款、结算共享这一接口。
- 不联网，不修改其他服务和部署配置，不在执行中申请扩大权限。

教学范围已由题设确认。配置由 Agent 生成后，仍须由人逐项核对并应用；Notebook 的独立核对代码只回放这份预先确定的教学批准，不代替生产审批。
