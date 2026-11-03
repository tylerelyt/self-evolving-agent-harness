# 对照第19讲完成练习

本练习对应《企业数据集：怎样把真实经历变成可重跑的评测题？》。沿用正文的跨集群调查：Agent 调查 `prod-eu/checkout-api`，却可能把 `staging-eu` 的同名服务异常当作生产根因。

练习要完成的不是安装一个Langfuse，而是从一项完整调查出发，留下故障现场和独立答案，让指定Skill能够重新调查，并在以后取回同一版本的题。可以先运行[完整示例](README.md#先跑通文章中的完整过程)：两轮都由真实 Hermes 执行，第一轮使用官方插件采集，第二轮由 Skill Up 从取回的数据集启动。示例的故障现场是教学快照，不是生产记录。

| 文章中的步骤 | 本目录怎样完成 | 应当留下什么 |
| --- | --- | --- |
| 从线上取回记录 | Hermes官方插件记录执行，在Langfuse选择完整调查 | Trace ID及原始Observations |
| 记录变成题目 | 明确任务映射，按事故归组，补快照与独立标注 | `pack.json`和`pending.json` |
| 从同一现场重跑 | Skill Up自定义Engine调用Hermes，参数查询只读快照 | `protocol.json`、回答与实际工具回执 |
| 更新后仍可比较 | 固定Langfuse版本，导出并恢复同一评测包 | 数据集时间点、快照哈希、评分器版本 |

## 1. 先取得一项完整调查

Langfuse可以[自行部署](selfhost/README.md)，不要求注册云端账号。文章中的官方插件负责在Hermes运行时记录轮次、模型和工具调用；[已有轨迹导入脚本](langfuse_smoke.py)用于检查历史记录的导入与恢复，不能代替这一步在线采集。

本仓库固定的Hermes提交已经内置插件，启用命令为：

```bash
hermes plugins enable observability/langfuse
hermes plugins list
```

这是该固定版本对正文 `hermes plugins install langfuse` 的具体操作对应。先在**运行值班服务的Hermes Python环境**安装 `langfuse==4.15.2`，并配置 `HERMES_LANGFUSE_PUBLIC_KEY`、`HERMES_LANGFUSE_SECRET_KEY`、`HERMES_LANGFUSE_BASE_URL`。凭据来自自己的Langfuse项目；自部署时地址指向自己的实例。重启后检查是否出现名为 `Hermes turn` 的记录。不要只看插件显示enabled，SDK或凭据缺失时仍可能没有上传。详见[固定版本官方说明](https://github.com/NousResearch/hermes-agent/blob/aaf9688519cca58dd5f76a589a0911aff269b060/plugins/observability/langfuse/README.md)。

这里使用与插件 `set_trace_io()` 接口匹配的 SDK v4，不再使用3.15.0。版本不匹配时，模型和工具记录可能出现，整轮调查的根记录却没有正常结束。相关历史见[官方兼容性问题与修复说明](https://github.com/NousResearch/hermes-agent/issues/94710)。本例预检会检查实际方法，不只检查包是否能导入。

固定版本的插件还会裁剪记录：每次模型输入保留最近12条消息，嵌套超过4层的值可能显示为 `<max-depth>`。所以，Langfuse 记录不能当作完整故障快照。完整示例逐项核对插件保留的内容，并在报告中列出被裁剪的位置；评测所需的现场仍来自另行保存、校验并绑定版本的快照，不从缺失字段猜答案。

故障事件编号、目标集群和Skill版本来自业务接入层，插件不会替企业判断这些归属。接入层已有字段时直接保留；只有原生会话字段时，在下面的任务映射中依据告警和记录补齐。

在网页的Observations或新版Tracing列表中筛选调查，打开记录核对它是否包含完整的任务与工具过程。文章中的 `Add to dataset` 可以将输入加入候选数据集，但**不要把原回答映射成标准答案**，也不要每次模型调用建一道题。网页选中的条目仍须补齐快照与独立标注。下面使用同一批Trace的API路线完成可重跑题，不依赖网页自动补全这些材料。

以下命令均在本讲目录执行，`REAL_TRACE_ID`应替换成刚才确认的编号：

```bash
PY=../../.deps/hermes-agent/.venv/bin/python
"$PY" -B setup.py --langfuse
LF=.deps/sdk/bin/python
"$LF" -B langfuse_io.py collect \
  --trace-id REAL_TRACE_ID --out runtime/collected.json
```

这个SDK环境读取 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 和 `LANGFUSE_BASE_URL`。可与插件使用同一项目的凭据，注意变量名前缀不同。需要采集多条Trace时，重复提供 `--trace-id`。完整示例将本讲 SDK 目录显式加入隔离的 Hermes 进程，用原生插件采集；独立部署值班服务时，则应在该服务自己的 Python 环境安装 SDK。

## 2. 把会话记录整理成调查题

在 `runtime/task-map.json` 中逐一记录选中Trace对应的完整题面与归属。以下只展示字段格式，编号和内容应来自自己刚才核对的调查：

```json
{
  "REAL_TRACE_ID": {
    "input": {
      "prompt": "调查 prod-eu 中 checkout-api 的超时告警，确定根因、影响范围和对应证据。"
    },
    "metadata": {
      "incident_id": "incident-001",
      "cluster": "prod-eu",
      "snapshot_id": "incident-001",
      "scenario": "cross-cluster",
      "split": "dev",
      "skill_version": "本次运行实际使用的Skill版本"
    }
  }
}
```

同一事故的后续追问不应单独成为新题。先确定完整调查要求，再让对应的多条Trace使用相同题面、事故编号、集群、快照和开发分组。不能把某一轮“再查一下”的片段直接当成完整输入。

```bash
"$LF" -B langfuse_io.py map \
  --source runtime/collected.json --tasks runtime/task-map.json \
  --out runtime/investigations.json
```

`map`保留原始Observations、原输入和原metadata，另记映射文件哈希；不读原回答来猜答案，也不调用模型自动归组。若一条Trace包含多个根Observation，采集端全部保留，等待你明确完整题面，不擅自挑一个当作整场调查。

接着准备两类材料：把事故现场保存到 `runtime/snapshots/`，把复核后的答案及依据写入 `runtime/annotations.json`。快照格式参照[构造快照](fixtures/snapshots/incident-001.json)，标注格式参照[构造标注](fixtures/annotations.json)，但真实事故不能直接复用示例中的答案。

快照应声明各集群、时间段和数据类型的覆盖范围。每次查询按参数取数，不固定旧Agent的查询顺序。标注按 `事故编号@集群` 定位，记录快照版本、复核人、独立修复记录或故障时间线，并分别提供生产根因与非目标集群的证据。只有结论、没有现场，或者只有旧Agent回答、没有独立依据，都不能成为正式题。

```bash
"$PY" -B dataset.py \
  --source runtime/investigations.json \
  --annotations runtime/annotations.json \
  --snapshots runtime/snapshots \
  --out runtime/article-pack
```

先检查 `runtime/article-pack/pending.json`。材料不完整的记录停在这里，不能为了让命令通过而填入猜测答案。后面的每一步都使用这次生成的包，不再切回构造示例包。

## 3. 保存题目，固定数据集版本

```bash
"$LF" -B langfuse_io.py publish \
  --pack runtime/article-pack/pack.json \
  --dataset oncall-eval --apply --out runtime/published.json
```

打开Langfuse的 `oncall-eval`，核对input、独立expected_output、快照信息与来源Trace。只有ready项会上传。若先前通过网页建立过候选条目，应使用独立的候选数据集，避免与API生成的正式题重复计数。

从实际发布记录或网页版本列表选择需要复现的时间点，替换下面的 `DATASET_VERSION`，不要照抄文章的示意日期：

```bash
"$LF" -B langfuse_io.py export \
  --dataset oncall-eval --version DATASET_VERSION \
  --out runtime/article-export.json
"$LF" -B langfuse_io.py restore \
  --export runtime/article-export.json \
  --snapshots runtime/snapshots --out runtime/article-restored
```

恢复过程同时核对快照哈希、独立标注、来源与评分版本。网页只有题目和答案、没有这些材料时，会留下pending原因，而不是宣称已经恢复一套可重跑环境。Langfuse不代替故障快照存储。

## 4. 用Skill Up重新调查

先准备根README要求的Hermes环境和本讲Skill Up，再检查恢复后的包：

```bash
"$PY" -B setup.py
"$PY" -B run.py --pack runtime/article-restored/pack.json --probe
"$PY" -B run.py --pack runtime/article-restored/pack.json
```

这些命令不调用模型。`--probe`核对Hermes工具与答案隔离，最后一项执行原生Skill Up配置校验。真实执行目前使用macOS隔离；Langfuse服务可以部署在Linux，不能因此把评测执行器也当成已支持Linux。

配置自己的模型访问与预算以后，显式启动真实调查：

```bash
"$PY" -B run.py --pack runtime/article-restored/pack.json --live
```

Hermes自主决定查询顺序，工具从同一快照取数，评分器在Agent读不到的位置核对答案。检查运行目录里的 `protocol.json`、实际工具回执和评分结果。要比较两版Skill，再用 `--skill`指定各自的 `SKILL.md` 文件，两次仍使用同一个恢复包和模型配置。

## 5. 数据更新以后，取回旧题

修改条目或添加新题之后，以新的输出路径再次执行旧时间点的 `export` 和 `restore`。比较题面、答案、来源及快照内容，而不是只比较条目数量或平均分。历史版本的 `updated_at` 可能仍显示最新修改时间，不能仅凭这个字段判定旧题已改变。

还要保留每次使用的评分器和查询代码版本；如果评分规则变了，不能把旧分数直接与新口径比较。本目录会拒绝沿用不匹配的评测包，避免无声替换条件。

本讲系统设计题最终应回答四件事：哪些记录入选；现场怎样固定；答案由什么独立事实支持；新旧Skill怎样在同一版本条件下比较。提交自己的来源记录、ready/pending及其原因、实际运行结果和固定版本信息，不预设Agent一定答对，也不预设新版Skill一定改善。

练习完成后，按[自部署说明](selfhost/README.md#停止与删除)停止并清理自己的临时实例，先导出需要保留的记录。
