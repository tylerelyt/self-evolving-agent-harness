# 第23讲：把候选、评测与下一轮任务接起来

本讲练习的是组件之间的交接：Hermes 留下执行经历，SkillClaw 生成隔离候选，独立评测作出版本决定，在线实例继续使用正式版，新客户端同步后再留下新的经历。默认运行中有一项评分故意不可用，正确结果是 `REJECT`。候选仍可查看，正式版继续服务。

这是第23讲自己的有限演练。输入是本目录定义的连接池只读快照和 `seed/timeout-triage`；输出写入一次独立运行目录。它没有启动双环境 HTTP 服务、持续巡检、告警、paging、审批恢复或工作台。那些属于单独的[综合实战](../../workshop/capstone/README.md)。

先按仓库根 README 准备固定版本依赖，再进入本目录：

```bash
cd examples/23-oncall-agent
bash run.sh --offline
bash run.sh
```

`--offline` 只检查发布门槛与回执校验，不请求模型。第二条命令运行本讲完整参考：模型响应来自本机脚本 HTTP 服务，Hermes、原生 `skill_view`、SessionDB、SkillClaw Client Proxy、Evolve Server 和 SkillHub 实际执行。它不读取个人模型配置，不需要密钥，也不调用 GLM。每次建立新的 `runtime/<时间>-<随机值>`，终端打印 `REPORT.md` 的路径；已有目录不覆盖。需要固定输出位置时使用 `bash run.sh --out /tmp/lesson23-first-run`，重跑时换一个新目录。

## 跑完后看什么

实例 A 首先读取正式 Skill，查询服务健康和上游延迟。本次任务有一条明确的补查要求：前两项正常时，再比较配置文件与实例生效的连接池上限。工具实际返回配置值 120、生效值 60、等待请求 24。这些数值是教学构造；差异是观测，不能直接证明超时根因。补查要求也是显式输入，不能把它说成模型自主发现了新方法。

Client Proxy 原样归档这段会话。后台进程在独立共享目录里调用原生 Evolve Server 的 Summarize、Aggregate、Execute，提出补齐参数对比的候选。摘要和修改回复同样由脚本模型提供，所以这次运行证明的是总装与证据链，不是模型的自主学习质量。原生 `validated` 路径只负责将内容放进候选区；本讲自己的统一评测门槛决定是否发布，不冒充原生 PRM 的验证结论。

新旧 Skill 分别在全新 Hermes Home 中回放两道冻结任务：一题需要比较配置与生效值，另一题的上游已经异常，应结束初查、不追加连接池查询。评测核对真实 Skill hash、工具参数、调用先后和返回结果。默认运行故意让候选的第二题评分不可用，标为 `score_error`，既不算业务失败，也不能从剩余题的通过率推断可以发布。

后台处理期间，实例 A 的 SessionDB 被备份到另一个目录，由新进程恢复同一个 Session，保留原报告 Item 并继续一次任务。实例 C 随后通过原生 SkillHub 拉取正式版，在新 Session 中实际读取该版本并执行；其会话又经 Client Proxy 归档，作为下一批经历保留。本次不自动启动第二批演化，已处理的 Session 和下一批来源分开记录。快照恢复的是会话历史与固定 Skill，不是运行进程或已经发生的业务副作用。

| 文件 | 应能核对的事实 |
| --- | --- |
| `experience.json`、`experiences/*.json` | A 的来源实例、Session、报告 Item、原生归档与实际工具回执 |
| `candidate.json` | 候选 hash、父版本、生成来源、执行依据、隔离文件、最终决定 |
| `learning/native/summary.json` | 原生候选进入待验证区，未直接改正式版；本次关闭了 session judge |
| `learning/evaluation.json` | 相同冻结输入、不同 Home/Session、实际 Skill hash；PASS、FAIL、`score_error` 分开 |
| `continued/instance-a/checkpoint.json`、`result.json` | 同一个 Session 从原报告后继续，原 Item 与 Skill hash 未变 |
| `receiver/instance-c/task/result.json` | 新客户端实际读取正式版，不能只看文件拉取成功 |
| `next-experience.json`、`learning/processed-sources.json` | 新执行进入后续材料，已处理来源有独立索引；本次不自动重跑下一批 |
| `report.json` | 本轮 `REJECT`、正式版不变，以及本次覆盖和未覆盖的分支 |

## 自己改一处，再检查

先打开 `scenario.py` 中的 `decide`，找到“评分不可用时拒绝”的条件。尝试仅凭已经拿到的分数作决定，再运行 `bash run.sh --offline`：相关回归应当失败。恢复条件后，再检查候选通过更多用例、却仍有一项无法判断时，为什么不能放行。

随后可以在新的运行目录中修复这次评分输入：

```bash
bash run.sh --complete-scores
```

这条命令只移除明确注入的评分故障，继续使用相同题集、工具和门槛。只有候选修复旧版失败、原有正确行为没有回归、全部评分有效时，才按内容 hash 发布；实例 C 的新任务再检查实际读取与新增工具调用。它是本讲的可选正向对照，不替代默认的拒绝路径，也不能据此证明在未见任务上持续改善。

## OpenViking 与 GEPA 的接入边界

本次现场上下文由本讲冻结快照提供，**没有启动或验收 OpenViking**。要接入按需外部上下文，应在本讲独立运行目录中增加只读查询工具，保存返回来源、查询范围和实际内容；回放时将同一内容冻结给新旧版本。第14讲的连接组件可以复用，但不能用历史记忆替代本次现场，也不能共享其他讲的运行状态。本次 `report.coverage.openviking` 明确记录为未执行。

默认运行只执行 SkillClaw 候选分支，**没有运行 GEPA 搜索**。`run23.register_candidate` 是统一交接入口：SkillClaw 候选记录来源 Session 与归档 hash；GEPA 来源则须提供冻结数据集指纹和优化运行标识，之后仍进入相同隔离区和评测门槛。离线回归验证了这份交接契约，没有验证优化器能产生合格候选。将第20讲的优化器接进来时，应使用本讲自己的同类任务、Skill 和输出目录；不能将另一业务题包的候选简单改名后发布。`report.coverage.gepa` 保留这一未覆盖边界。

本目录因此可以独立验证“经历→候选→评分异常拒绝→正式版继续→会话恢复→新实例读取→保留新经历”。完整持续 On-call 系统仍由综合实战单独承担。
