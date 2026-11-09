# 巡检与检查点

本目录提供本地 HTTP 服务环境、持续巡检、可恢复的 Hermes 会话与报告检查点。

- `environment.py` 管理服务流量、观测窗口和故障注入。
- `patrol.py` 调度服务抽查，核对工具回执与报告，保存巡检记录。
- `hermes_runtime.py` 管理原生 SessionDB 与报告 Item。

运行参数可通过各入口的 `--help` 查看。当前健康状态取自本轮 HTTP 观测；历史巡检记录用于追溯与比较。
