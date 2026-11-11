# 自进化 Agent 实战

极客时间专栏[《自进化 Agent 实战》](https://time.geekbang.org/column/intro/101188801)的配套代码，覆盖第 01—23 讲。

这套课程从 Agent 的执行与控制出发，逐步接入 Memory、Skill、业务评测和跨实例演化，最终搭建一个持续巡检、处理告警，并在后续任务中验证改进效果的 On-call 系统。

## 选择入口

| 你想做什么 | 从这里开始 |
| --- | --- |
| 按专栏顺序学习 | [课程与代码对照](COURSE_MAP.md) |
| 先运行完整 On-call 系统 | [综合实战](workshop/capstone/README.md) |
| 在工作台中逐步搭建系统 | [四步实作指南](workshop/capstone/lab/README.md) |
| 先做一个无需密钥的短练习 | [支付查询与评分器校准](workshop/practice/README.md) |

## 快速开始：运行完整系统

需要 Git、Bash，以及 Python 3.12 或 `uv`。以下命令均在仓库根目录执行。

```bash
bash scripts/setup_deps.sh
bash scripts/setup_skillclaw.sh

.deps/hermes-agent/.venv/bin/python -B workshop/capstone/self_improvement_agent.py \
  --out workshop/capstone/runtime/fleet-demo
```

默认无需模型密钥。两套本地 HTTP 环境、Hermes 工具调用、会话存储和 SkillClaw 实际运行，模型响应由脚本提供。重跑时换一个新的输出目录。

演练共两轮：先处理持续失败与间歇失败，归档初判会话，生成候选 Skill 并独立评测；再交换故障类型，核对两端是否实际采用新版。结束后先看 `report.json`，再按[证据说明](workshop/capstone/README.md#跑完先看三份记录)回查调查、恢复和版本决定。

### 打开业务工作台

完成上面的依赖安装后，可直接启动完整参考工作台：

```bash
.deps/hermes-agent/.venv/bin/python -B workshop/capstone/app.py \
  --solution --offline --port 8765
```

打开 [localhost:8765](http://127.0.0.1:8765)，点击「启动 Agent」，观察持续巡检，再注入故障。工作台提供指标、日志、变更、SRE 对话、恢复批准、Memory 和 Skill 学习记录。

`--offline` 用于脚本模型演示。使用真实模型时，先按[配置说明](workshop/capstone/README.md#启动本地工作台)准备本地 `.env`，再去掉 `--offline`；模型调用会产生 API 费用。去掉 `--solution` 则进入[逐步实现](workshop/capstone/lab/README.md)模式。

## 按课学习

| 讲次 | 内容与入口 |
| --- | --- |
| 01—09 | [Codex SDK 与 Notebook](examples/README.md)：Agent Loop、运行时控制、上下文、Skill 与执行权限 |
| 10 | Hermes 手动观察题，按正文记录 Memory、Skill 与新会话的使用情况 |
| 11—14 | [后台复盘](examples/11-background-review/) · [记忆修订](workshop/oncall/README.md) · [会话检索](examples/13-session-lookup/) · [外部记忆](examples/14-external-memory-provider/) |
| 15—17 | [创建 Skill](examples/15-skill-autocreation/) · [修订 Skill](examples/16-skill-incremental-patch/) · [Curator](examples/17-curator/) |
| 18—20 | [业务评测](examples/18-business-eval/) · [企业数据集](examples/19-enterprise-dataset/) · [GEPA 离线优化](examples/20-gepa-offline-optimization/) |
| 21—22 | [SkillClaw 会话收集](examples/21-skillclaw-session-collection/) · [验证发布与分发](examples/22-skillclaw-shared-revision/) |
| 23 | [双实例 On-call 综合实战](workshop/capstone/README.md) |

各讲的安装、运行命令和验收要求放在对应 README，完整映射见 [COURSE_MAP.md](COURSE_MAP.md)。第 03、08、09 讲需要 Codex 原生 Sandbox；第 18—20 讲的隔离评测需要 macOS `sandbox-exec`，其他平台可先运行各讲的离线自检。

OpenViking、Langfuse 和 GEPA 的综合接入属于[进阶实验](workshop/capstone/README.md#进阶实验)，可在基础流程跑通后按需选择。

## 运行离线检查

```bash
python3.12 -B scripts/check_offline.py
```

检查只需 Python 标准库、Git 和 Bash，覆盖安装、会话证据、Memory、评分器与综合实战边界。[GitHub Actions](.github/workflows/course-checks.yml) 使用同一入口。真实模型效果与原生框架联调按各讲说明单独验证。

## 许可证

[MIT](LICENSE)
