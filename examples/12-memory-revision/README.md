# 第 12 讲：记忆修订 workshop

本目录的读者入口已由 [常驻 Hermes oncall workshop](../../workshop/oncall/README.md) 取代。现在操作的是一个持续接受 HTTP 消息的真实服务：先巡检形成旧入口记忆，再切换支付部署，带正式迁移说明调查告警，观察原生后台自主修订，最后用新会话验证目录查询。

```bash
bash examples/12-memory-revision/run.sh
# 等价主入口：bash workshop/oncall/run.sh
# 希望跑完继续交互：bash examples/12-memory-revision/run.sh --keep-running
```

凭据从 `~/.hermes/.env` 读取 `GLM_API_KEY` 与代理配置。模型地址、教学 Home、五幕日志、两次完整 Memory 与 diff、复盘回执、九项验收和交互命令，均见 [新入口说明](../../workshop/oncall/README.md)。运行记录在 `workshop/oncall/runtime/observations/`，不进 Git。

本讲统一使用上面的 `run.sh`。目录内原有的 `run_memory_revision.py`、`observe_review.py`、`log_service.py` 保留作历史代码对照；当前练习的完整实现位于 `workshop/oncall/`。
