# 第23讲：把候选、评测与下一轮任务接起来

本讲练习的是组件之间的交接：Hermes 按需读取 OpenViking 上下文并留下执行经历，SkillClaw 与 GEPA 分别生成隔离候选，独立评测作出版本决定，在线实例继续使用正式版，新客户端同步后再留下新的经历。默认运行故意让每份候选各有一项评分不可用，正确结果是 `REJECT`。候选仍可查看，正式版继续服务。

这是第23讲自己的有限演练。输入是本目录定义的连接池只读快照和 `seed/timeout-triage`；输出写入一次独立运行目录。它没有启动双环境 HTTP 服务、持续巡检、告警、paging、审批恢复或工作台。那些属于单独的[综合实战](../../workshop/capstone/README.md)。

先按仓库根 README 准备固定版本 Hermes 与 SkillClaw，再从仓库根目录安装本讲的 GEPA 和独立 OpenViking 服务依赖：

```bash
.deps/hermes-agent/.venv/bin/python -m pip install -r examples/23-oncall-agent/requirements.txt
.deps/hermes-agent/.venv/bin/python -B examples/23-oncall-agent/setup_openviking.py
cd examples/23-oncall-agent
bash run.sh --offline
bash run.sh
```

OpenViking 固定版本安装在本目录的 `runtime/openviking-deps/venv`，不依赖先运行其他讲。安装器不启动服务或调用模型；已有环境可用 `setup_openviking.py --check` 核对。GEPA 固定为 `0.0.27`。`run.sh` 只读预检这两项依赖，缺少时停止，不自动安装。

`--offline` 只检查发布门槛与回执校验，不请求模型。`bash run.sh` 运行本讲完整参考：Hermes、原生 `skill_view`、SessionDB、OpenViking、SkillClaw Client Proxy、Evolve Server、官方 GEPA 优化器和 SkillHub 实际执行。模型、反思、嵌入和摘要均使用本机脚本响应，不需要密钥，也不调用 GLM；这证明框架接线和证据链，不证明真实模型的学习质量。每次建立新的 `runtime/<时间>-<随机值>`，终端打印 `REPORT.md` 的路径；已有目录不覆盖。需要固定输出位置时使用 `bash run.sh --out /tmp/lesson23-first-run`，重跑时换一个新目录。

## 跑完后看什么

本次运行先在独立 OpenViking 空间中存入本讲值班手册。实例 A 读取正式 Skill 后，实际调用 `read_external_context`，由工具处理函数向原生 OpenViking 发出 `find` 和 `read`，留下资源 URI、内容 hash 和本次服务返回回执。随后才查询服务健康与上游延迟。手册是参考上下文，不是本次现场状态。

这次任务还有一条明确的补查要求：前两项正常时，再比较配置文件与实例生效的连接池上限。工具实际返回配置值 120、生效值 60、等待请求 24。这些数值是教学构造；差异是观测，不能直接证明超时根因。补查要求也是显式输入，不能把它说成模型自主发现了新方法。

Client Proxy 原样归档这段会话。后台进程在独立共享目录里调用原生 Evolve Server 的 Summarize、Aggregate、Execute，提出补齐参数对比的候选。摘要和修改回复同样由脚本模型提供，所以这次运行证明的是总装与证据链，不是模型的自主学习质量。原生 `validated` 路径只负责将内容放进候选区；本讲自己的统一评测门槛决定是否发布，不冒充原生 PRM 的验证结论。

另一条后台路径运行官方 GEPA。它使用本讲自己的六道构造任务，按事故分为训练、验证、留出三组，每组各有一题配置漂移和一题上游异常，实例范围和参数也分别冻结。优化器实际调用独立 Hermes 任务取得轨迹，再根据训练反馈请求一次脚本反思，经小批对照和 Pareto 选择导出候选。留出题不会进入搜索，也不会用于反思。

两个来源通过同一份候选交接记录进入统一评测。现行版与各候选分别在全新 Hermes Home 中回放那两道独立留出任务：一题需要比较配置与生效值，另一题上游已经异常，应结束初查、不追加连接池查询。每次对照都使用相同的冻结 OpenViking 内容、任务范围和现场数据，检查实际 Skill hash、工具参数、调用先后与返回结果。默认运行故意让各候选的第二题评分不可用，标为 `score_error`，既不算业务失败，也不能从剩余题的通过率推断可以发布。

后台处理期间，实例 A 的 SessionDB 被备份到另一个目录，由新进程恢复同一个 Session，保留原报告 Item 并继续一次任务。实例 C 随后通过原生 SkillHub 拉取正式版，在新 Session 中实际读取该版本并执行。恢复后的 A 和新实例 C 都在自己的工具调用中重新查询原生 OpenViking；离线优化与评测才读取固定的上下文副本。C 的会话又经 Client Proxy 归档，作为下一批经历保留。本次不自动启动第二批演化，已处理的 Session 和下一批来源分开记录。快照恢复的是会话历史与固定 Skill，不是运行进程或已经发生的业务副作用。

| 文件 | 应能核对的事实 |
| --- | --- |
| `context-manifest.json`、`external-context/native-receipts.json` | 原生 OpenViking 入库与检索结果、独立空间、冻结内容及来源 |
| `online/instance-a/task/context-receipt.json`、`receiver/instance-c/task/context-receipt.json` | 在线 Agent 工具实际执行的 `find`、`read`，不同于离线冻结回放 |
| `experience.json`、`experiences/*.json` | A 的来源实例、Session、报告 Item、原生归档与实际工具回执 |
| `candidates.json`、`candidate-records/*.json` | 两个来源各自的候选 hash、父版本、执行依据、隔离文件与最终决定 |
| `learning/native/summary.json` | 原生候选进入待验证区，未直接改正式版；本次关闭了 session judge |
| `learning/gepa/dataset.json`、`selection.json` | 本讲独立的三组任务、官方优化结果、实际训练与验证 Session、候选选择；留出不参与搜索 |
| `learning/evaluation.json`、`learning/evaluation/*.json` | 两个候选面对同一留出集；新旧独立 Session、实际 hash、相同上下文与现场指纹；PASS、FAIL、`score_error` 分开 |
| `continued/instance-a/checkpoint.json`、`result.json` | 同一个 Session 从原报告后继续，原 Item 与 Skill hash 未变 |
| `receiver/instance-c/task/result.json` | 新客户端实际读取正式版，不能只看文件拉取成功 |
| `next-experience.json`、`learning/processed-sources.json` | 新执行进入后续材料，已处理来源有独立索引；本次不自动重跑下一批 |
| `report.json`、`openviking-service/service.json` | 本轮决定、实际采用的来源、正式版 hash、原生服务退出与本机模型请求计数 |

## 自己改一处，再检查

先打开 `scenario.py` 中的 `decide`，找到“评分不可用时拒绝”的条件。尝试仅凭已经拿到的分数作决定，再运行 `bash run.sh --offline`：相关回归应当失败。恢复条件后，再检查候选通过更多用例、却仍有一项无法判断时，为什么不能放行。

随后可以在新的运行目录中修复这次评分输入：

```bash
bash run.sh --complete-scores
bash run.sh --complete-scores --prefer-source gepa
```

第一条命令只移除明确注入的评分故障，继续使用相同题集、工具和门槛。只有候选修复旧版失败、原有正确行为没有回归、全部评分有效时，才按内容 hash 发布；实例 C 的新任务再检查实际读取与新增工具调用。如果两份候选都通过，默认选择 SkillClaw 来源，第二条命令则优先选择已经通过同一门槛的 GEPA 来源。每次只发布一份批准内容，不把另一个来源的通过记录冒充为已发布或已采用。

它们是本讲的可选正向对照，不替代默认的拒绝路径。脚本响应可以验证调用链、版本治理和固定任务上的行为，不能据此证明真实模型会自主发现方法，或在未见业务上持续改善。

## 本讲的边界

本次实际运行 OpenViking、SkillClaw 和官方 GEPA，三者的数据与运行目录都属于本讲。现场仍是参数驱动的只读教学快照，不访问生产服务。OpenViking 手册与当前现场回执分开保存；GEPA 的验证结果与最终留出评测分开保存；两个生成来源的候选与真正发布的版本分开保存。服务退出后，在线检索不会继续可用，已冻结的离线证据仍可核查。

本目录验证的是一次有限总装，不是生产常驻部署，也没有实现无限自动演化。完整持续 On-call 系统仍由综合实战单独承担。
