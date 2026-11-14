# On-call Workshop

关键词：**闭环**。两套 HTTP 环境、两个 Hermes：巡检 → 外部告警 → Skill 调查 → paging → 恢复 → SkillClaw → 独立评测 → 发布与采用。

## 运行双实例演练

在仓库根目录执行：

```bash
make setup
make demo
```

真实运行 Hermes、HTTP 和 SkillClaw，模型使用固定脚本，无需 API key。两轮恢复窗口需要等待数分钟。末尾看 `closed_loop_verified: True`、两端新探测与 `rollback: completed`；原始 Session、工具回执、版本和评测保存在打印出的独立目录。

指定自己的输出目录可运行：

```bash
.deps/hermes-agent/.venv/bin/python -B scripts/demo.py --out /tmp/oncall-demo
```

已有目录不会覆盖；失败返回非零，已经产生的证据保留。[原生 CLI 与验收字段](RUNNING.md#运行双实例演练)。

## 启动本地工作台

```bash
make ui     # 完整参考系统，脚本模型
make lab    # 逐步实现；先关闭占用 8765 的前一个工作台
```

打开终端给出的地址，启动 Agent，注入故障，查看调查与交接，再申请并批准本地恢复。`Ctrl-C` 停止自己的进程；[操作截图](WALKTHROUGH.md)。

端口已占用时用 `make ui PORT=8766` 或 `make lab PORT=8766`。

### 使用真实模型

见[模型配置与调用边界](RUNNING.md#使用真实模型)。`make demo`、`make ui` 和 `make lab` 都不会自动切到在线模型。

### 逐步实现

让 Coding Agent 读取 [lab/AGENTS.md](lab/AGENTS.md)，只编辑 [lab/self_improvement_agent.py](lab/self_improvement_agent.py)。[四步实作](lab/README.md)给出每一步的现场操作和验收命令。

| 改动入口 | 你接入的能力 |
| --- | --- |
| `patrol_config` | 服务范围、抽查数量、巡检间隔 |
| `route_alert`、`branch_report`、`notify_operator` | 同回合接告警、报告分支、交接后继续巡检 |
| `recall_memory` | 新会话检索历史，再查询当前 HTTP 现场 |
| `release_policy` | 新旧独立回放、无回归与严格增益 |

最终演练传入同一份实现和累计检查；[完整命令](lab/README.md#第-4-步让团队改进下一次的做法)。

脚本模型不会理解任意新规则；修改自然语言 Skill 后，按在线模型配置另做评测。

## 完整参考运行

| 读代码 | 入口 |
| --- | --- |
| 编排与版本决策 | [self_improvement_agent.py](self_improvement_agent.py) |
| 巡检、检查点与 steer | [patrol.py](patrol/patrol.py) |
| 原生演化与分发 | [skillclaw_evolve.py](patrol/skillclaw_evolve.py) |
| 调查方法 | [service-patrol](patrol/skills/service-patrol/SKILL.md) · [oncall-alert-triage](patrol/skills/oncall-alert-triage/SKILL.md) |

## 双实例持续值守

[持续服务与手动故障注入](RUNNING.md#双实例持续值守)。受控两轮是验收场景，持续服务运行到主动停止。

## 证据检查与回放

从输出目录的 `report.json` 开始，沿 `patrol_evidence`、评测与发布记录找到原始 Session 和 Item；[独立审计与回放命令](RUNNING.md#证据检查与回放)。

## 进阶实验

[OpenViking / 数据集 / GEPA / 回退](RUNNING.md#进阶实验) · [组件映射](INTEGRATION-PLAN.md) · [生产扩展设计](PRODUCTION-DESIGN.md)
