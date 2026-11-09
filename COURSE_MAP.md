# 课程与代码对照

这里列出第 01—23 讲的学习入口与独立综合实践。第 01—09 讲使用 Codex SDK 与 Notebook，第 10 讲安排 Hermes 手动观察，第 11—18 讲使用 Hermes，第 19—22 讲接上数据集、GEPA 与 SkillClaw，第 23 讲将多实例值守与版本决策连成一次实跑。

| 讲次 | 技术主题 | 练习入口 |
| --- | --- | --- |
| 01 | Agent Loop | [01-agent-loop](examples/01-agent-loop/) |
| 02 | 运行时控制 | [02-instruction-control](examples/02-instruction-control/) |
| 03 | 上下文引擎 | [03-context-engine](examples/03-context-engine/) |
| 04 | AGENTS.md | [04-agents-md](examples/04-agents-md/) |
| 05 | 上下文压缩 | [05-context-compression](examples/05-context-compression/) |
| 06 | 会话分支 | [06-thread-fork](examples/06-thread-fork/) |
| 07 | 经验技能化 | [07-skill-extraction](examples/07-skill-extraction/) |
| 08 | 渐进披露 | [08-progressive-disclosure](examples/08-progressive-disclosure/) |
| 09 | 隔离与审批 | [09-isolation-approval](examples/09-isolation-approval/) |
| 10 | Hermes 与递归自我改进 | 正文手动观察题，查看 Hermes 留下的 Memory、Skill 及新会话中的使用情况 |
| 11 | 后台复盘 | [11-background-review](examples/11-background-review/) |
| 12 | Memory 修订 | [12-memory-revision](examples/12-memory-revision/)，主实现见 [workshop/oncall](workshop/oncall/README.md) |
| 13 | 会话检索与原始回执核对 | [13-session-lookup](examples/13-session-lookup/) |
| 14 | Memory Provider 与共享记忆 | [14-external-memory-provider](examples/14-external-memory-provider/)，文件与 SQLite 为基础练习，OpenViking 为可选进阶 |
| 15 | 从执行经历创建 Skill | [15-skill-autocreation](examples/15-skill-autocreation/) |
| 16 | 修订已有 Skill | [16-skill-incremental-patch](examples/16-skill-incremental-patch/) |
| 17 | Curator 整理技能库 | [17-curator](examples/17-curator/) |
| 18 | Skill 业务评测 | [18-business-eval](examples/18-business-eval/) |
| 19 | 企业数据集：把真实经历变成可重跑的评测题 | [19-enterprise-dataset](examples/19-enterprise-dataset/) |
| 20 | 离线优化：官方 GEPA 搜索 Skill 正文 | [20-gepa-offline-optimization](examples/20-gepa-offline-optimization/) |
| 21 | SkillClaw：多实例会话采集与原生 workflow 演化 | [21-skillclaw-session-collection](examples/21-skillclaw-session-collection/) |
| 22 | SkillClaw：客户端验证、发布、同步与恢复 | [22-skillclaw-shared-revision](examples/22-skillclaw-shared-revision/) |
| 23 | 自进化 On-call Agent：把每一层接成一条线 | [结课大作业 · 双实例巡检与 On-call 演练](workshop/capstone/README.md) |

环境准备见 [README](README.md) 与 [scripts/setup_deps.sh](scripts/setup_deps.sh)。运行状态和前置条件以各讲说明为准。

[综合实践 · 跑完一次学习循环，再决定下一次采用什么](workshop/practice/README.md)使用独立的支付状态查询案例，练习评分器校准、新旧方法的成对比较和版本采用决策。
