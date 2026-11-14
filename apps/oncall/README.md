# On-call

两个 Hermes 持续巡检本地 HTTP 服务，外部监控投递告警，后台从事故中演化 Skill。命令从 repo 根目录执行。

## Run

```bash
make setup
make ui
```

工作台使用完整实现与脚本模型。启动 Agent、注入故障、读调查报告，再批准本地恢复。零模型费用；在线推理的配置在[运行参考](REFERENCE.md#continuous-service)。

只需要终端服务时：

```bash
.deps/hermes-agent/.venv/bin/python -B -m apps.oncall serve --out /tmp/oncall-service
```

这是持续服务，运行到 `Ctrl-C`；使用新的输出目录。想自动验证两轮故障、候选发布、后续采用和回退，运行 `make demo`。

## Change it

| 入口 | 管什么 |
| --- | --- |
| [service.py](service.py) | 持续巡检、事故处理和后台学习 |
| [workflow.py](workflow.py) | 有界双轮验收 |
| [patrol/skills/](patrol/skills/) | 巡检与故障调查方法 |
| [memory.py](memory.py) | 已恢复事故复盘与记忆发布 |
| [knowledge_tools.py](knowledge_tools.py) | 模型按需检索的范围和回执 |
| [web/](web/) | 工作台 |

改代码后用 `make check`，再重跑受影响的原生行为；脚本模型接线通过与在线模型质量分别验证。

## Deploy

[部署配置](../../deploy/README.md)管理进程、重启恢复与持久化 OpenViking。默认专用配置的索引摘要需要私有模型连接；直接脚本演练不需要它。

[模型、状态恢复与 CLI](REFERENCE.md) · [Memory 契约](MEMORY.md) · [企业工具与恢复授权](ENTERPRISE.md)
