# 第 19 讲：把 Rollout 整理成可重跑的评测题

**跟随文章做练习，请从[正文配套操作](ARTICLE_GUIDE.md)开始。** 顺序与正文一致：Hermes官方插件记录调查，在Langfuse筛选，补齐快照与独立标注，生成数据集，再用Skill Up重跑并验证历史版本。下面保留本地自检与实现细节，构造数据不代替真实记录采集。

历史报告可以告诉我们 Agent 怎样出错，却不能直接充当标准答案。本例将同一事故的执行记录归组，用参数驱动的只读快照还原现场，再由独立标注评分。Hermes 自主选择查询顺序，不重放旧 Agent 的工具调用脚本。

场景与正文一致：调查 `prod-eu/checkout-api` 超时，避免把 `staging-eu` 的同名服务故障串进来。公开材料全部是**人工构造的教学数据**，不是生产事故或线上采集结果。只准备一个可运行开发题，以及缺快照、缺独立标注的待处理记录；不预写模型运行成功或 Skill 修改收益。

## 先跑通文章中的完整过程

完整流程需要 macOS `sandbox-exec`、固定版本 Hermes 和 Langfuse 项目。其他平台可先运行下方的数据自检。在本目录执行：

```bash
PY=../../.deps/hermes-agent/.venv/bin/python
"$PY" -B setup.py
"$PY" -B setup.py --langfuse
LF=.deps/sdk/bin/python
```

连接[自部署 Langfuse](selfhost/README.md)，设置 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`。先预检，再配置模型凭据与费用上限，执行两轮真实调查：

```bash
"$LF" -B pipeline.py --probe --out runtime/native-probe
# 设置 GLM_API_KEY、GLM_BASE_URL；建议通过有总额限制的预算代理访问。
"$LF" -B pipeline.py --live --dataset oncall-native-eval \
  --out runtime/native-pipeline
```

`pipeline.py` 先启动 Hermes，在调查过程中由官方 `observability/langfuse` 插件直接上传。程序从服务端取回记录，逐条核对工具结果和模型调用数，再关联入口处保存的事故信息，补入预先准备的独立标注与固定现场。随后将题目写入 Langfuse，按指定时间点取回，交给原生 Skill Up 启动**另一轮 Hermes 调查**。不会重放第一轮工具顺序，也不会把第一轮回答拿来直接重评分。

每次使用新的数据集名和输出目录。`--probe` 不调用模型；`--live` 明确允许两轮模型调用和数据集写入。两轮各自保留独立工作目录，报告列出原生 Trace、数据集时间点、实际查询和 Skill Up 评分。`pipeline_completed` 表示过程跑通，是否答对看 `case_results`，两者不能混为一谈。

调查已经结束、但取回记录失败时，可在相同命令后加 `--resume`。它只允许接续尚未制题、未开始评测的采集失败，保留失败记录并核对原输入、快照、标注和 Skill 没有变化；不会重做第一轮调查，也不能用它反复重跑失败的业务评测。

示例使用公开教学快照和预先写好的独立标注，不冒充生产事故。自己的练习通过 `--task`、`--snapshots`、`--annotations`、`--skill` 替换材料；题面只含调查要求，标准答案不进入被测 Agent。材料格式及选择依据见[正文配套操作](ARTICLE_GUIDE.md)。

官方插件在每次独立 `HERMES_HOME` 中启用，SDK 从本讲隔离环境加载，不改个人 Profile，也不向共用 Hermes 环境安装包。Langfuse 缺记录时流程停止，不以手工补传代替原生采集。插件对消息条数和嵌套深度有裁剪，报告会明确列出工具结果中的缺失位置；缺失内容不从 Trace 猜测，仍须由独立保存的完整快照提供。详情见[采集边界](ARTICLE_GUIDE.md#1-先取得一项完整调查)。

## 单独检查数据处理

数据处理和评分器自检只用 Python 标准库。在仓库根目录开始：

```bash
cd examples/19-enterprise-dataset
python3.12 -B selftest.py
python3.12 -B dataset.py --out runtime/pack-v3
```

`selftest.py` 也可用 Python 3.11 以上运行。评测包默认得到 **1 个 ready、2 个 pending**。同一事故的两段 Trace 合成一道题。重复制题时请使用新目录。

## 在 macOS 验证 Hermes 接线和 Skill Up

按仓库首页准备固定版本 Hermes 后，在本讲目录继续。执行器检查提交号和已跟踪文件状态，有源码改动时会停止。以下两项检查无需模型密钥。

```bash
PY=../../.deps/hermes-agent/.venv/bin/python
"$PY" -B run.py --pack runtime/pack-v3/pack.json --probe
"$PY" -B setup.py
"$PY" -B run.py --pack runtime/pack-v3/pack.json
```

`--probe` 在 macOS `sandbox-exec` 中实际导入 Hermes、加载技能、查询一次快照并探测答案隔离。

`setup.py` 只下载并校验本讲固定的 Skill Up 0.14.0 官方 macOS 二进制，约 21 MB 下载、51 MB 解压，不编译 Go、不安装整个依赖仓库。最后一条命令只调用原生 `skill-up validate`。上游找不到根 `SKILL.md` 的提示不影响自定义 Engine；技能由本讲适配器通过 Hermes 原生预载接口加载。

## 最小真实运行

明确设置 `GLM_API_KEY` 和 `GLM_BASE_URL`，可以使用统一预算代理。程序不会查找个人 `.env`，密钥不写入评测配置。

```bash
"$PY" -B run.py --pack runtime/pack-v3/pack.json --live
```

这条命令实际调用 **Skill Up `run` → 自定义 Engine → Hermes `AIAgent.run_conversation`**，运行 1 题、1 版 Skill。默认 `glm-5.2`、温度 0、最多 12 轮、每轮最多 4096 输出 token、每题工作进程最多 600 秒；Skill Up 不重跑用例，Hermes 自身仍可能进行传输重试。轮数和 token 上限不是美元预算，费用硬上限应由外部预算代理执行。

`Ctrl-C` 会终止当前 Skill Up 进程组，并由 Engine 关闭其 Hermes 子进程组。每次运行保存在新的 `runtime/run-*`，包含 `protocol.json`、生成的 `evals/eval.yaml`、原生结果及 `execution.json` 附件；独立 Hermes 的查询回执和日志保留在 `runtime/actors/`。基础设施失败记为未评分，不拿零分冒充业务失败。

真实执行目前仅支持 **macOS `sandbox-exec`**。其他平台可检查数据处理与评分器，但执行器会拒绝裸跑。Hermes 只看到当次对话、业务快照和指定 Skill，唯一工具是 `monitor_query`；没有 shell、文件读取或编辑工具。评分包、原始 Rollout、独立标注和其他版本均不在可读范围内。

## 看清正文对应的代码

| 正文中的问题 | 代码入口 |
| --- | --- |
| 从实时采集到新一轮调查的完整过程 | [pipeline.py](pipeline.py)、[业务入口输入](fixtures/investigation.json) |
| 同一事故多轮归组、来源与分组冲突 | [dataset.py](dataset.py)、[rollouts.json](fixtures/rollouts.json) |
| 固定现场，不固定工具调用顺序 | [monitor.py](monitor.py)、[故障快照](fixtures/snapshots/incident-001.json) |
| 独立答案、证据归属与缺资料的 pending | [schema.py](schema.py)、[annotations.json](fixtures/annotations.json)、生成的 `pending.json` |
| 真实 Hermes 与答案隔离 | [engine.py](engine.py)、[worker.py](worker.py) |
| Skill Up 原生评测入口 | [run.py](run.py)、[eval 配置模板](evals/eval.yaml) |
| 根因、集群和实际引用评分 | [grader.py](grader.py)、[selftest.py](selftest.py) |
| 官方 Langfuse SDK 导入导出 | [langfuse_io.py](langfuse_io.py)、[固定依赖](dependencies.json) |
| 原生会话字段怎样成为完整题面 | `langfuse_io.py map`、[逐步操作](ARTICLE_GUIDE.md#2-把会话记录整理成调查题) |
| 把同一题包交给第20讲继续优化 | [case_runner.py](case_runner.py)、[第20讲接续入口](../20-gepa-offline-optimization/README.md#接续第19讲的数据集) |

`evals/eval.yaml` 是待填充的模板。`run.py` 将选定评测包生成逐题 YAML（使用 YAML 兼容的 JSON 写法），配置 `context.repo_fixture` 与脚本 judge，再交给原生 CLI。不要直接运行尚无题目的模板。

第20讲可以直接读取本讲导出或恢复后的 `pack.json`，沿用同一执行器、快照和评分器。优化时需另备明确的训练、验证和留出划分；本讲默认只有一道ready题，不能复制成三组来制造独立比较。每次候选预载都核对实际提示中的Skill内容，并保存加载哈希，不只检查磁盘上有没有文件。

可直接改变查询参数，检查快照而不调用模型：

```bash
python3.12 -B monitor.py fixtures/snapshots/incident-001.json logs \
  --cluster prod-eu --service db-gateway \
  --start 2026-10-20T10:00:00Z --end 2026-10-20T10:10:00Z
```

快照不会根据第几次调用返回预排结果。`manifest.clusters` 显式声明各集群已采集的数据类型，覆盖范围不从记录是否非空推断。工具 schema 的集群枚举由该声明生成，不读取标准答案；每个回执的 `scope` 列出集群、数据类型及时间范围。未覆盖的查询返回 `not_covered`，不返回空记录数组；只有已覆盖的查询才可能返回 `rows: []`，其含义是没有匹配记录，不等于服务健康或可以排除。

题面的 `excluded_cluster` 指快照覆盖范围内，与生产目标服务同名、但经查询确认其故障不属于本次生产事故的非目标集群，不是任意没有告警的集群。具体名称须通过工具发现和查询，题面不直接给出。

评分器允许引用实际查得的本题快照证据，不把非目标集群的引用一概当作错误。独立标注将必要证据分为两组：`required_evidence` 只包含目标集群的根因、链路和影响范围证据；`required_exclusion_evidence` 必须属于指定排除集群中的同名服务。两组都必须实际查询并在回答中引用。缺少排除依据、只查不引、只引未查，或拿排除依据代替生产根因证据，均不能通过；标注缺失或归属错误的用例进入 pending。标注只供评分器使用，不进入 actor 输入或工具 schema。

输出接口由执行器固定为 `oncall-json-v2`：在 Skill 之外说明五个结果字段，并通过模型请求的 `response_format: {"type": "json_object"}` 使用 JSON 模式。参数依据见[智谱对话补全文档](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)。所有候选使用同一接口，GEPA 只修改调查方法。

JSON 模式不能代替接收端检查。若回答仍附带说明，执行器只允许一次无工具的 Hermes 格式修正，将原回答和格式问题交回模型，不提供评分、标准答案或新的现场资料。修正后的五项内容必须与原回答中唯一的完整结果对象逐字段一致，否则仍以原回答接受严格评分，不采用改过内容的答案。程序只比较对象，不截取对象冒充模型最终输出。原始调查与格式修正的消息分别保存；额外调用计入总调用数，原生 Langfuse 记录为两个 Trace，修正失败不无限重试。

纯 JSON 格式要求不变。`explanation_nonempty` 只说明解释文本是否非空，`explanation_semantics` 始终为 `not_evaluated`（未作语义评判）。`passed` 表示机器检查的字段、查询回执、两组引用及格式符合要求，不表示因果解释正确，也不能证明解释文本没有混用两类依据。解释是否忠实、结论是否充分仍需人工复核，不调用 LLM 代评。

本版评测包 schema 为 `oncall-pack-v3`，评分语义版本为 `oncall-evidence-v3`，同时记录评分器和标注结构校验代码的 SHA-256。旧包不会被悄悄套用新规则，缺少新标注字段的历史导入项也不会自动补答案。

比较新旧 Skill 时必须共用同一评测包。题面、快照覆盖声明、查询行为或评分规则变化后，应生成新包，并在新口径下重跑所有待比较版本；不能把不同口径的运行结果拼成对照。旧评测包和旧运行记录应保留，不用新工具重新解释旧回执。没有进行新口径的模型运行时，只能报告评分器离线核查，不能宣称模型通过新版评测。

## Langfuse 自部署或云端接入

本地评测不依赖 Langfuse。需要集中查看 Trace、Observations 和数据集时，可以[自行部署](selfhost/README.md)，也可以连接 Langfuse Cloud，两者使用相同的 SDK。运行 `"$PY" -B setup.py --langfuse`，在本目录 `.deps/sdk` 建立可选环境，安装官方 `langfuse==4.15.2`，与当前 Hermes 插件的 `set_trace_io()` 接口保持一致。设置 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` 后使用该环境：

```bash
LF=.deps/sdk/bin/python
"$LF" -B langfuse_io.py collect --trace-id REAL_TRACE_ID --out runtime/collected.json
"$LF" -B langfuse_io.py map --source runtime/collected.json \
  --tasks runtime/task-map.json --out runtime/investigations.json
"$PY" -B dataset.py --source runtime/investigations.json \
  --annotations runtime/annotations.json --snapshots runtime/snapshots \
  --out runtime/article-pack
"$LF" -B langfuse_io.py publish --pack runtime/article-pack/pack.json \
  --dataset oncall-eval --apply --out runtime/published.json
"$LF" -B langfuse_io.py export --dataset oncall-eval \
  --version DATASET_VERSION --out runtime/export.json
"$LF" -B langfuse_io.py restore --export runtime/export.json \
  --snapshots runtime/snapshots --out runtime/restored
```

`DATASET_VERSION`须换成目标数据集实际需要复现的时间点。任务映射、快照与独立标注的准备方法见[正文配套操作](ARTICLE_GUIDE.md)。`collect`按明确Trace ID逐页读取官方Observations v2接口，完整保留返回记录。只有一个根Observation时同时提取输入与输出；有多个根时不猜题面，等待明确映射。Langfuse 4的events-only部署不再提供旧 `api.trace.get` 入口。

`map`将业务层确认的完整题面及 `incident_id`、`cluster`、`snapshot_id`、`split` 等metadata关联到原始Trace，保留原输入、原metadata和所有Observations。程序不会从旧 `output` 生成答案。没有这一步时，原生插件的用户消息或会话字段不一定能直接满足本讲题目格式；资料不齐的记录仍由 `dataset.py`列入pending。

`publish` 必须显式 `--apply` 才写入远端，仅上传 ready 项；以稳定 ID 更新条目，保留全部来源 Trace ID、快照哈希、标注依据与评分器版本。构造 Trace 仅记入 metadata，不伪造远端 Trace 关联。`export` 必须指定带时区的版本时间，调用官方 `get_dataset(version=...)`。`restore` 核验快照哈希及标注与远端答案的一致性，缺失材料仍为 pending。Langfuse 不代管本地快照；独立标注的真实性仍由复核人负责。

官方接口依据：[Langfuse 数据集与版本](https://langfuse.com/docs/evaluation/experiments/datasets)、[SDK 4.15.2](https://github.com/langfuse/langfuse-python/tree/v4.15.2)、[自部署说明](https://langfuse.com/self-hosting/deployment/docker-compose)、[Skill Up 自定义 Engine](https://github.com/alibaba/skill-up/blob/v0.14.0/docs/design/custom-engine.md)。仅通过本地字段映射测试还不够，需对实际连接的实例验证写入、读取和指定版本导出。

### 用已有真实运行验证接入

[langfuse_smoke.py](langfuse_smoke.py) 接受本讲已完成的单题真实运行，不额外调用模型。下面各路径须换成自己的执行记录及评测包：

```bash
"$LF" -B langfuse_smoke.py \
  --execution runtime/actors/YOUR_ACTOR/home/execution.json \
  --run runtime/YOUR_RUN \
  --pack runtime/YOUR_CURRENT_PACK/pack.json \
  --original-pack runtime/YOUR_ORIGINAL_PACK/pack.json \
  --out runtime/langfuse-check --apply
```

脚本先核对原运行的题面、快照、输出及工具回执，使用 OpenTelemetry 写入根 Observation 和工具 Observations，再经当前 API 读回。随后发布由独立标注生成的数据集，固定一个服务端版本时间；仅修改新版 metadata 标记，检查旧时间点仍能取回原来的题面、答案和来源，再恢复评测包并重评分。

这是已有真实轨迹的导入与重评分，不是新一轮模型调查。原回答失败仍然失败。上传时的 span 时间表示导入过程，不能用于分析原模型或工具耗时。版本比较检查条目内容，原样保存 API 返回的时间字段；服务端可能在读取历史版本时仍返回条目最近的 `updated_at`，不能把它误作历史内容已改变。

## 课后练习

按文章的系统设计题完成一次转换：从有权使用且已脱敏的真实调查开始，说明为什么选中它；补齐故障快照和独立标注，再交给Skill Up重跑。检查没有快照或没有可靠答案的记录是否仍停在pending，不能将旧诊断复制为答案。

随后固定数据集、快照和评分器版本，用 `--skill`指定两版方法，在同一包上比较。数据集更新后再次取回原版本，检查是否仍能复现同一批题。提交来源、取舍理由和实际结果，而不只是“命令运行成功”。[正文配套操作](ARTICLE_GUIDE.md)按这个顺序提供命令，未额外增加另一套作业。

正式采用前，还应检查新增收益、旧题退步、调用成本和解释质量。小型构造题只能检验接线与局部行为，不能证明生产效果。

本地测试与无模型探测不能替代真实评测。运行目录、密钥、原始私密记录与下载依赖均不进入公开版本。
