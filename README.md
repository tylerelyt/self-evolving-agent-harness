# 自进化 Agent 实战

极客时间专栏[《自进化 Agent 实战》](https://time.geekbang.org/column/intro/101188801)的配套代码，覆盖第 01—22 讲。

Agent 能完成一次任务，还要能从工作中积累经验。这次纠正过的判断，应当影响下一次调查；已经摸清的排查方法，应当能够复用；业务发生变化以后，旧经验也应随之修订。否则，工程师仍要一遍遍补背景、纠正同类错误、整理和搬运经验。

这门专栏从 Harness 入手，把这些反复发生的工作逐步交给系统。前半程先让你能驾驭正在运行的 Agent：看清它依据什么作判断，怎样干预任务，怎样组织上下文、规则和权限。后半程让经验整理成为持续工作的一部分：由后台复盘提取经验，保存为 Memory 或 Skill，再在后续任务中使用和修订。人的目标、授权与业务反馈，成为系统持续改进的依据。

这里用巡检与故障调查贯穿实战。你可以直接运行代码，查看模型请求、工具结果、规则文件和记忆变化，把每一项工程判断落实到系统的实际行为上。

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

完整讲次见[课程与代码对照](COURSE_MAP.md)，第 10 讲负责背景与机制定位。

前 9 讲采用 Codex SDK 与 Notebook，关键接口直接写在代码格里，适合拆开观察 Harness 的运行机制。第 11—18 讲采用 [Hermes](https://github.com/NousResearch/hermes-agent)，从后台复盘和长期记忆继续推进到会话检索、外部记忆、Skill 管理和业务评测。第 19—22 讲接上企业数据集、官方 GEPA 优化器，以及 SkillClaw 的会话采集、演化和分发。各讲说明分别标明框架原生机制与课程增加的场景、检查和评测代码。

## 开始运行

需要 Git、Bash、Python 3.12 和智谱 `GLM_API_KEY`。练习使用构造的业务材料，模型调用和工具执行走真实接口，会产生 API 费用。

### 从第 1 讲开始

在仓库根目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r examples/requirements-notebooks.txt jupyterlab
export GLM_API_KEY="你的智谱 API Key"
python -m jupyterlab examples/01-agent-loop/workshop.ipynb
```

选择同一 Python 3.12 内核，从上到下运行。Notebook 通过 SDK 启动 Codex app-server，再经本地适配器调用 GLM。接口与实验入口见[第 01—09 讲练习导览](examples/README.md)；第 03、08、09 讲需要宿主支持 Codex 原生 Sandbox。

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

第 19 讲把调查记录、环境快照和独立确认的答案分开，再运行同一批评测题；Langfuse 可以使用[随讲提供的 Docker Compose 自部署](examples/19-enterprise-dataset/selfhost/README.md)，也可以连接云端项目。第 20 讲通过明确的三组题号清单接续第19讲的评测包，沿用原快照和评分规则，由官方 GEPA 调用 Hermes 比较候选。也保留可独立运行的九题教学示例；两条路径不混合成绩，每次都核对实际加载、评分和导出的 Skill。

第 21、22 讲共用 SkillClaw 依赖：

```bash
bash scripts/setup_skillclaw.sh
export GLM_API_KEY="你的智谱 API Key"
bash examples/21-skillclaw-session-collection/run.sh \
  --run-dir "$PWD/.runtime/skillclaw-oncall"
```

默认直接发布；希望先做原生客户端验证时，在第21讲启动命令中加 `--publish-mode validated`。两种结果都可以按第22讲继续，不必重跑采集。第22讲观察原生周期同步和新Session的实际使用，并把客户端文本验证与业务工具检查分开记录，不能用文件同步成功代替能力检查。全部操作使用练习自己的共享组和目录，不改个人Hermes或SkillClaw配置。

## 许可证

[MIT](LICENSE)
