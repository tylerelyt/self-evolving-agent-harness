# 支付日志平台正式迁移说明（教学变更）

平台负责人：支付平台当班负责人。变更编号：PAY-LOG-20261010。
生效时间：本次实验 `lab env use regional` 成功回执的 `effective_at`（UTC）；该回执随说明一起交给 Agent。
影响范围：payment-api 全部区域和环境，包括 us-east/prod、eu-west/staging。

统一入口 payment-api-prod 已退役，不再用于读取当前日志，调用会返回 410。
支付日志现按区域与环境拆分。今后通过 Service Catalog，以服务名、区域、环境查询当前有效日志入口，再读取返回入口的日志。
查询命令：`payctl catalog lookup --service payment-api --region <区域> --env <环境>`。
目录也提供历史归档指针，迁移前日志保留；退役统一入口不意味着删除历史日志。
区域入口可能继续变化，具体 URI 由目录维护，不应当作长期不变的地址。
