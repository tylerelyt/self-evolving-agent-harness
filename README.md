# 自进化 Agent 实战

极客时间专栏[《自进化 Agent 实战》](https://time.geekbang.org/column/intro/101188801)的配套仓库，包含第 01—23 讲的学习入口和独立综合实践。

用巡检与故障调查练习 Agent Harness。先看清 Agent 怎样行动、依据什么作判断，再让调查经历形成 Memory 或 Skill。新的方法要经业务评测，才能进入下一次任务。每项练习都能回查模型请求、工具结果或版本决策。

## 先跑一个练习

| 学习目标 | 入口 | 前置条件 |
| --- | --- | --- |
| 看一次评分器校准和版本采用决定 | [支付状态综合实践](workshop/practice/README.md) | Python 3.12，无需密钥 |
| 按课学习 Agent 的执行过程 | [第 1 讲 Notebook](#从第-1-讲开始) | Python 3.12、Notebook 依赖。运行真实模型时需要 GLM 密钥 |
| 实际运行两个巡检 Agent，看 Skill 如何发布与回退 | [第 23 讲大作业](workshop/capstone/README.md#运行双实例演练) | 固定版本的 Hermes 和 SkillClaw，默认模式无需密钥 |

在仓库根目录运行下面的命令，可先用构造订单对照两版方法的评分。终端会打印保留或采用的决定，以及本轮记录的目录。

```bash
python3.12 workshop/practice/replay.py --judge calibrated
```

## 从哪里读起

| 你要掌握的能力 | 对应练习 |
| --- | --- |
| 把一项任务交给 Agent，并在运行中掌握方向 | [01 Agent Loop](examples/01-agent-loop/) · [02 运行时控制](examples/02-instruction-control/) · [03 上下文引擎](examples/03-context-engine/) |
| 让规则和经验进入后续行动，同时守住任务与权限范围 | [04—09 规则、上下文、Skill 与执行权限](examples/README.md) |
| 让经验整理在后台持续发生，并随业务变化修订 | [11 后台复盘](examples/11-background-review/) · [12 记忆修订](workshop/oncall/README.md) |
| 找回调查依据，并在多个实例间共享所需知识 | [13 会话检索](examples/13-session-lookup/) · [14 外部记忆](examples/14-external-memory-provider/) |
| 从实际调查中形成方法，再修订和整理技能库 | [15 创建 Skill](examples/15-skill-autocreation/) · [16 修订 Skill](examples/16-skill-incremental-patch/) · [17 Curator](examples/17-curator/) |
| 判断新方法是否改善业务表现，又是否损害原有能力 | [18 业务评测](examples/18-business-eval/) |
| 把真实经历整理成可重跑的题，再搜索更好的方法 | [19 企业数据集](examples/19-enterprise-dataset/) · [20 GEPA 离线优化](examples/20-gepa-offline-optimization/) |
| 汇集多个实例的经历，让共同方法到达下一次任务 | [21 SkillClaw 收集与蒸馏](examples/21-skillclaw-session-collection/) · [22 验证发布与分发](examples/22-skillclaw-shared-revision/) |
| 从持续巡检跑到候选评测、版本采用与恢复 | [23 双实例 On-call 大作业](workshop/capstone/README.md) |
| 用固定订单成对比较两版方法，依据评分决定是否切换 | [综合实践 · 跑完一次学习循环](workshop/practice/README.md) |

完整讲次见[课程与代码对照](COURSE_MAP.md)。第 10 讲在正文中安排 Hermes 手动观察题，记录一次任务留下的 Memory、Skill 和新会话中的使用情况。

前 9 讲采用 Codex SDK 与 Notebook，关键接口直接写在代码格里，适合拆开观察 Harness 的运行机制。第 11—18 讲采用 [Hermes](https://github.com/NousResearch/hermes-agent)，从后台复盘和长期记忆继续推进到会话检索、外部记忆、Skill 管理和业务评测。第 19—22 讲接上企业数据集、官方 GEPA 优化器，以及 SkillClaw 的会话采集、演化和分发。各讲说明分别标明框架原生机制与课程增加的场景、检查和评测代码。

## 开始运行

准备 Git、Bash 和 Python 3.12。练习使用构造的业务材料。运行真实模型的练习时，按各讲说明配置智谱 `GLM_API_KEY`，模型调用会产生 API 费用。各讲的离线检查和第 23 讲默认双实例演练无需模型密钥。

### 从第 1 讲开始

在仓库根目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r examples/requirements-notebooks.txt jupyterlab
printf '智谱 API Key: '
read -r -s GLM_API_KEY
printf '\n'
export GLM_API_KEY
python -m jupyterlab examples/01-agent-loop/workshop.ipynb
```

选择同一 Python 3.12 内核，从上到下运行。Notebook 通过 SDK 启动 Codex app-server，再经本地适配器调用 GLM。接口与实验入口见[第 01—09 讲练习导览](examples/README.md)；第 03、08、09 讲需要宿主支持 Codex 原生 Sandbox。

想先检查 Notebook 和材料是否可用，可以在安装依赖后运行 `.venv/bin/python examples/01-agent-loop/run_validation.py --mode offline`。这一步无需密钥，验证构造材料、Notebook 单元格和离线产物。

### 体验持续工作的值班助手

第 12 讲可以独立运行。在 `~/.hermes/.env` 中准备 `GLM_API_KEY`，然后执行：

```bash
bash scripts/setup_deps.sh
bash examples/12-memory-revision/run.sh
```

它会在本机启动支付业务与 Hermes 值班服务。一次部署迁移会使旧经验过时，你可以观察后台怎样修订记忆，以及新会话怎样使用修订后的经验完成调查。环境准备、交互命令和停止方式见[第 12 讲说明](workshop/oncall/README.md)。

各讲会保留独立运行记录，便于回看判断依据与前后变化。分享记录前，请移除其中的凭据和个人信息。

### 继续第 13—18 讲

先完成相同的 Hermes 依赖准备，再按各讲 README 运行。第 14 讲先用文件后端和 SQLite 观察共享、召回与故障恢复，OpenViking 是可选进阶接入，不需要为基础练习安装它。

练习观察模型实际作出的选择，不保证每次都生成 Memory、Skill 或更优版本。后台不保存、候选没有改善、检查发现退步，也都是需要阅读和解释的结果，不能由脚本补成成功。

### 继续第 19—22 讲

第 18—20 讲的隔离评测需要 macOS `sandbox-exec`。可以先运行各讲的数据和评分器自检。第 19 讲的 Skill Up 校验与 Hermes 接线预检也需要 macOS。[第 18 讲](examples/18-business-eval/README.md) · [第 19 讲](examples/19-enterprise-dataset/README.md#单独检查数据处理) · [第 20 讲](examples/20-gepa-offline-optimization/README.md#先运行无费用检查)

第 19 讲把调查记录、环境快照和独立确认的答案分开，再运行同一批评测题。跟随正文使用 Langfuse 时，可以连接云端项目，也可以用随讲提供的 Docker Compose 自部署。[Langfuse 自部署](examples/19-enterprise-dataset/selfhost/README.md)

第一次做第 20 讲，先用随讲提供的九题教学数据，按其 README 的默认路径运行。第 19 讲默认只导出一道可运行的题，适合检查一次重跑。接续自己的第 19 讲评测包时，要先准备来自不同事故的训练、验证和留出题，再把划分清单交给 GEPA，沿用原快照和评分规则。[第 20 讲九题运行命令](examples/20-gepa-offline-optimization/README.md#模型连接与搜索预算) · [第 19 讲题包接续条件](examples/20-gepa-offline-optimization/README.md#接续第19讲的数据集)

第 21、22 讲共用 Hermes 和 SkillClaw 依赖。在仓库根目录按以下顺序准备环境并开始第 21 讲。

```bash
bash scripts/setup_deps.sh
bash scripts/setup_skillclaw.sh
export GLM_API_KEY="你的智谱 API Key"
bash examples/21-skillclaw-session-collection/run.sh \
  --run-dir "$PWD/.runtime/skillclaw-oncall"
```

默认直接发布；希望先做原生客户端验证时，在第21讲启动命令中加 `--publish-mode validated`。两种结果都可以按第22讲继续，不必重跑采集。第22讲观察原生周期同步和新Session的实际使用，并把客户端文本验证与业务工具检查分开记录，不能用文件同步成功代替能力检查。全部操作使用练习自己的共享组和目录，不改个人Hermes或SkillClaw配置。

### 运行第 23 讲大作业与综合实践

第 23 讲的双实例演练从持续巡检和检查点起步，注入模拟故障，再把真实 Hermes 工具回执和原生 SkillClaw 归档送进候选评测、发布与采用验证。默认使用脚本模型。可另跑 OpenViking 交班、跨服务根因评测、Langfuse 采集和 GEPA 离线对照。[第 23 讲大作业](workshop/capstone/README.md) · [OpenViking 交班](workshop/capstone/README.md#用-openviking-做跨实例交班) · [根因评测](workshop/capstone/README.md#把巡检事故做成根因评测题) · [Langfuse 采集](workshop/capstone/README.md#把巡检与分支记录接入-langfuse) · [GEPA 离线对照](workshop/capstone/README.md#用-gepa-做一轮离线对照)

综合实践使用独立的支付状态查询案例，观察经历回流、评分器自检、同题比较和保留基线的决定。[综合实践](workshop/practice/README.md)

## 运行离线检查

在仓库根目录执行 `python3.12 -B scripts/check_offline.py`。这组检查只用 Python 标准库、Git 和 Bash，覆盖安装、会话证据、Memory 修订、业务评分和大作业的本地 HTTP 边界。GitHub Actions 使用同一入口在 Linux 和 macOS 上检查变更。真实 Hermes 和模型运行按各讲的命令单独验收。 [工作流定义](.github/workflows/course-checks.yml)

## 许可证

[MIT](LICENSE)
