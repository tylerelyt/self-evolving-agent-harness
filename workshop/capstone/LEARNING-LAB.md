# 结课大作业｜让值班 Agent 从两班工作中学会新方法

你接手一个负责支付服务告警初判的 Agent。单条告警通常能查清。一次事故同时影响两个服务时，它容易漏掉其中一个。扩大查询范围后，它又可能把同集群里的另一场事故合进来。

这次作业交付一套能持续修订方法的值班系统。先让 Agent 用当前 Skill 完成调查，保存它实际取得的证据。由后台根据经历提出修改，用独立题判断是否采用。新班次读取正式版继续工作，产生第二轮材料。最后用留出题核查业务表现，并在独立副本中恢复旧版、启动一个新会话确认恢复生效。

这是第 23 讲大作业的独立快照练习。**验收的是证据和采用决定。** 一份候选没有带来增益，保留当前版本，同样可以交出完整的实验结果。

## 先跑通参考项目

在配套仓库根目录执行。需要 macOS、Git、Bash，以及 Python 3.12 或 `uv`。本项目复用第 18 讲的 macOS 沙箱，固定 Hermes 提交由 [scripts/hermes-revision.txt](../../scripts/hermes-revision.txt) 指定。

```bash
bash scripts/setup_deps.sh
PY=.deps/hermes-agent/.venv/bin/python
"$PY" -B examples/18-business-eval/alert_selftest.py
"$PY" -B workshop/capstone/capstone.py
```

默认模式使用构造故障快照和本机脚本模型。脚本模型通过本机 HTTP 接收 Hermes 的模型请求，根据实际 Skill 回执选择查询，最终答案由工具结果组成。**Hermes 的工具循环、Skill 读取、只读命令、SessionDB 和业务评分器都实际运行。** 候选由明确的教学复盘规则产生。这个模式用来验证流程和证据，模型自主性和真实任务收益要看在线运行。

终端会给出 `runtime/<运行编号>/REPORT.md`。重复运行会创建新目录。指定 `--out` 时选一个尚不存在的目录。参考运行默认最多 24 个独立 Hermes 会话，每个会话最多 12 轮模型调用。可用 `--sessions` 和 `--turns` 收紧预算。

### 单独演练事故审批和交班

```bash
"$PY" -B workshop/capstone/system_demo.py
```

这条路径用构造的观测窗口和处置回执驱动事故状态机。调查由 Hermes 执行，第一份调查进入随后的学习循环。在 `SYSTEM_REPORT.md` 和 `incident-lifecycle.json` 查看未批准动作被拒绝、同一动作重复提交、交班恢复和两个健康窗口的关闭条件。

### 接上课程中的原生共享

在同一个课程环境中准备 SkillClaw，再选择原生共享模式。

```bash
bash scripts/setup_skillclaw.sh
"$PY" -B workshop/capstone/capstone.py --skillclaw
```

本作业的业务评分负责决定采用哪个版本。正式内容经原生 `SkillHub.push_skills` 写入本次运行的本地共享组，接收端经 `pull_skills` 取得。检查共享文件、接收端和新会话实际读取的内容三者是否一致。这里使用原生手动同步，周期轮询与原生候选验证的练习入口见[第 22 讲](../../examples/22-skillclaw-shared-revision/README.md)。

### 用真实模型做一轮完整实验

从已有的课程连接或预算代理设置 `GLM_API_KEY`、`GLM_MODEL`、`GLM_BASE_URL`，再显式开启在线模式。连接只从环境读取，作业使用独立运行目录。真实模式会产生模型费用，货币支出由所用代理控制。

```bash
"$PY" -B workshop/capstone/capstone.py --live --skillclaw --sessions 24 --turns 12
```

真实模式下，由 Hermes 选择工具，由独立模型请求复盘已落盘的经历并提出 Skill 正文。允许返回 `KEEP`，也允许候选不通过。协议不把连接故障记为业务低分。遇到中断时查看 `status.json` 和已产生的记录，另建一次运行可以重新比较。

## 学员要完成的四项工作

### 1. 交付一次可以追查的值班调查

先阅读 [cases.py](cases.py) 和课程的 [incidentctl](../../examples/18-business-eval/incidentctl.py)。把题面、工具可查的快照和评分依据分别识别出来。任选一项任务，从 `proof.json` 找到会话和执行目录，按调用编号把一次查询与回执对上。说明 Agent 为什么确认了根因，或者为什么留下未知。

每题使用独立 Home 和 SessionDB。只读工具查询当前快照，独立标注保存在宿主的评测包里。训练与复盘使用业务经历，采用题和留出题的成绩另行保存。试着从工具回执指出一个原始总结可能漏掉的事实。

### 2. 用新旧对照决定是否采用一项修改

第一轮工作题只有一个事故，涉及多个服务。基线先查题面点名的服务。复盘候选扩展同一集群内的查询，以补全共同受影响的服务。通过 `round-1.json` 核对逐题结果。采用规则要求每题有可比较的结果，原先通过的题继续通过，并且至少修复一道题。

默认运行还会试一个去掉日志集群约束的反例。它走同一套评分器。找出被拒绝的原因，核对正式指针的历史，解释为什么某道新增任务变好不足以抵消关键旧题退步。

现在自己准备一项待测修改。复制[种子 Skill](../../examples/20-gepa-offline-optimization/skills/oncall-alert-triage/SKILL.md)，保留完整 YAML 文件头，改写正文，再导入一轮。用真实模式验证模型是否理解你写的方法。本机脚本模型只识别随项目给出的教学规则，适合检查接线与拒绝逻辑。

```bash
"$PY" -B workshop/capstone/capstone.py --candidate 1=/你的目录/SKILL.md
```

`--candidate` 也能接收第 20 讲导出的 GEPA Skill。记录原运行、候选哈希和测试题的关系，确保本项目的验收和留出题未参与搜索。第 21 讲原生 SkillClaw workflow 也能提供候选。先把方法适配到本作业的工具、输出和种子文件头，再进入同一验收流程。导入记录保留文件哈希，候选来源的证明由原运行记录提供。

### 3. 用第二班的经历证明递归发生在哪里

在 `round-2.json` 找到第二班的 `loaded_sha256`。追查它是不是第一班获准采用的版本，再比较第二轮工作题的原版影子运行。两边使用同一份快照。观察哪一边真正查到了同集群的另一场事故。

接着打开 `experience/round-2.json`，核对复盘拿到的实际回执。第二轮候选增加了对当前工单 `incident_id` 的核对。把正式版读取、工具调用、回执进入复盘和候选差异四个环节串起来。若第一轮没有采用，在报告中说明第二班实际继承了什么，并如实给出这一轮的停点。

### 4. 给出独立验收和恢复证据

对照 `report.json` 里的两份留出结果。留出题含同集群干扰、跨集群干扰及证据不足的场景。发布后审计发现退步或结果不可比较时，项目恢复种子版本。核对正式指针与接收端得到的版本。默认参考运行还会在 `recovery-rehearsal/` 建独立副本，恢复原版，启动新的 Hermes 会话并确认读取的哈希。

再加一道和你关心的故障有关的题。写清独立确认的答案、必须引用的证据和一项容易被错判的相反样例。先校准评分口径，再比较方法。第 19 讲的[数据集操作](../../examples/19-enterprise-dataset/ARTICLE_GUIDE.md)可以用来把自己的原始记录制成快照和独立标注。

## 交付与评分

| 交付物 | 验收问题 | 分值 |
| --- | --- | --- |
| 运行说明、固定配置和数据划分 | 同学能否在独立目录重跑，知道题面、快照、标注各自放在哪里 | 15 |
| 一条完整值班轨迹 | 能否从会话定位实际读取的版本、查询参数、回执及结论 | 20 |
| 候选与逐题采用决定 | 能否用可信的新旧对照解释采用、保留或拒绝，并展示旧能力检查 | 25 |
| 第二轮材料和方法谱系 | 能否指出前一次方法怎样改变执行、带来新材料并参与下一轮学习 | 25 |
| 留出结果和恢复证据 | 能否把留出表现与开发得分分开，并核对恢复后新会话实际读取的内容 | 15 |

提交一份 Markdown 报告、代码与运行入口、你新增的场景及标注、候选差异和对应证据路径。报告中用几句话交代输入、发现、依据与决定。附上必要的调用和回执片段即可。训练材料、完整轨迹与凭据按所在环境的规则处理，公开提交使用脱敏后的材料。

| 查看位置 | 对应证据 |
| --- | --- |
| `protocol.json`、`dataset.json` | 固定依赖、执行配置、分组和快照 |
| `cases/*/home/state.db`、`cases/*/proof.json` | 会话复开、实际 Skill 读取、逐题评分 |
| `experience/round-*.json`、`reflection/` | 复盘材料及真实模式的模型响应 |
| `round-1.json`、`round-2.json`、`registry/official.json` | 候选谱系、逐题结果和正式版本历史 |
| `registry/receivers/`、`recovery-rehearsal/` | 接收、共享记录和独立副本恢复 |
| `REPORT.md`、`report.json`、`status.json` | 两轮进展、留出结果和运行状态 |

## 代码入口

- [capstone.py](capstone.py) 组织两次值班、复盘、验收、版本分发和恢复。
- [cases.py](cases.py) 在第 20 讲的题库结构上增加同集群另一事故，隔开工作题、采用题和留出题。
- [local_model.py](local_model.py) 提供本机脚本模型与确定性的教学复盘规则。
- [system_demo.py](system_demo.py) 用[事故状态机](incident_runtime.py)演练审批与交班，再复用同一调查会话进入学习。
- [第 20 讲 HermesRunner](../../examples/20-gepa-offline-optimization/runner.py) 负责隔离运行，[第 18 讲评分器](../../examples/18-business-eval/alert_scorer.py) 核对业务字段、实际工具调用与证据归属。
