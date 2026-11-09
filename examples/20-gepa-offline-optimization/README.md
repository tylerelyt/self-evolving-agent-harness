# 第20讲：让 GEPA 根据真实执行改进 Skill

优化分数提高，不代表保存下来的 Skill 就是被评测的那一份。本例把 `skill_body` 同时用于 GEPA 候选、Hermes 的实际 Skill 文件和最后导出，逐次核对正文哈希、完整文件哈希和 `skill_view` 回执。优化的是外部指令，不是模型参数。

入口是 [optimize.py](optimize.py)，直接调用官方 **`gepa==0.0.27`** 的 `optimize`。候选接受、反思式修改、Pareto 选择全部由官方库完成，没有自制搜索器，也不预写“优化成功”的候选。

## 先运行无费用检查

先按仓库首页准备固定版本 Hermes。下面用课程的 Python 3.12 环境安装固定版 GEPA，`run.sh` 随后也用同一个解释器。已有装好 GEPA 的 Python 3.10 或更新环境时，可以事先设置 `GEPA_PYTHON` 为它的解释器绝对路径。

```bash
cd examples/20-gepa-offline-optimization
export GEPA_PYTHON="${GEPA_PYTHON:-$PWD/../../.deps/hermes-agent/.venv/bin/python}"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$GEPA_PYTHON" -r requirements.txt
else
  "$GEPA_PYTHON" -m pip --version >/dev/null 2>&1 || "$GEPA_PYTHON" -m ensurepip --upgrade
  "$GEPA_PYTHON" -m pip install -r requirements.txt
fi
bash run.sh --offline
```

在同一终端直接运行 `bash run.sh` 也是离线测试。测试中的回答和反思文本是构造数据。macOS 上另有真实沙箱检查，全程不访问模型。也可打开 [workshop.ipynb](workshop.ipynb)，选择装有 GEPA 的 Python 内核分步执行。

## 获准后才运行模型

### 接续第19讲的数据集

如果已经完成第19讲，请使用其导出或恢复后的 `pack.json`，不要重新抄写成另一套问答数据。[enterprise.py](enterprise.py) 直接调用第19讲的真实Hermes执行器和业务评分器，保留时间窗口、快照覆盖、独立答案和两类证据要求，不改变评分口径。

准备一份清单，路径相对于清单文件，题号来自原包的ready项：

```json
{
  "pack": "restored/pack.json",
  "splits": {
    "train": ["incident-101@prod-eu"],
    "validation": ["incident-102@prod-us"],
    "holdout": ["incident-103@prod-ap"]
  }
}
```

题号只是格式示例。三组至少各有一道不同事故的题，训练和验证从原包dev中划分，留出只能使用原包holdout。同一事故、快照或来源Trace不能重复分配，pending不能入选。第19讲默认只有一道ready题，足以检查一次重跑，**不够做三组优化比较**；程序会停止，不会复制它凑数。

```bash
# 检查原包、真实Hermes加载、工具和答案隔离，不调用模型。
bash run.sh --probe --dataset-manifest runtime/enterprise-splits.json \
  --skill ../19-enterprise-dataset/skills/oncall-investigation/SKILL.md

# 配置模型端点和费用上限以后，才允许真实搜索。
bash run.sh --live --dataset-manifest runtime/enterprise-splits.json \
  --skill ../19-enterprise-dataset/skills/oncall-investigation/SKILL.md \
  --iterations 1 --minibatch 1 --metric-calls 8 --sessions 14 --reflections 1
```

这条路径仍使用同一官方GEPA搜索器。每题通过第19讲的 [case_runner.py](../19-enterprise-dataset/case_runner.py) 在新实例中执行，保存实际候选哈希、完整回执、原评分和包版本，答案留在宿主侧。预检只运行一题，不代表全部题通过业务评测。

第19讲执行器统一指定 JSON 输出接口，新旧候选不能通过改变报告格式来改变评测口径。接口、模型参数、评分器与数据包保持固定，GEPA 修改的是 Skill 中的调查方法。接口调整后的成绩须重新运行，不能和旧聊天输出的失败分数拼成“优化收益”。

这条路径每次调查最多追加一次无工具的格式修正，不能补查证据或改写调查内容。两次回答和各自消息都保留，额外调用也计入预算。若格式仍错误，或修正改变了原有结论，原评分器继续判失败；不是持续重试直到通过。

接续第19讲时，`--sessions` 同时计算调查会话和格式修正会话。开始每题以前预留两个名额，未用到的格式修正名额归还；不足两个名额就停止，不在中途取消格式协议来挤进最后一道题。汇总分别记录 `investigation_sessions`、`format_sessions` 与总数 `hermes_sessions`。这仍不是美元费用上限。

不传清单时，原有九题教学示例仍可独立运行。两条路径在报告中分别记录数据来源和评分版本，不混合比较成绩。

### 模型连接与搜索预算

本例不默认直连任何端点。Hermes 和反思模型共用下面三个环境变量，因此可以统一经过独立预算代理；`GLM_MODEL` 使用代理接受的 GLM 模型名。

```bash
export GLM_MODEL='glm-5.2'
export GLM_BASE_URL='http://127.0.0.1:9000/v1'  # 示例：替换为已批准的兼容端点
read -r -s -p 'GLM 或预算代理密钥: ' GLM_API_KEY; echo
export GLM_API_KEY
bash run.sh --live --iterations 1 --minibatch 1 --metric-calls 8 --sessions 14 --reflections 1 --turns 16
```

`GEPA_PYTHON` 可指定 GEPA 解释器；`HERMES_SRC`、`HERMES_PYTHON` 可指定已准备好的 Hermes 源码和解释器。源码必须与仓库固定提交一致且工作树无改动。当前复用第18讲的 macOS `sandbox-exec` 后端，其他平台明确停止，不裸跑。

这条默认九题命令只做一轮：初始验证3次、父候选与新候选各执行1道训练题；若通过接受检验，再做3次候选验证。之后立即进入留出评测，不为了耗尽预算再搜索。训练比较未接受候选时，总共8个 Hermes 会话；训练接受但验证没有提高时共11个；验证提高后比较两版各3道留出题，最多14个。接入第19讲时，会话数随清单中的题数变化，需相应调整上限。为观察完整流程，显式关闭满分跳过，保证正常执行时也会提出候选；等分仍然不接受。若请求失败或预算代理拒绝，不能保证完成这一流程，也不伪造反思或结果。

`--metric-calls` 是 GEPA 的任务评分停止阈值，不是反思轮数，更不是计费请求上限。官方库按批次检查停止条件，最后一批可能越过阈值。`--sessions` 另外限制全部 Hermes 会话数，包含搜索和留出评测；`--reflections` 限制反思请求数。一个 Hermes 会话可能调用模型多次，底层还可能重试，**费用硬上限须由外部预算代理执行**。达到硬上限、连接故障或进程超时都会中止并保留失败记录，不把未完成任务当作低分继续搜索。`Ctrl-C` 会终止当前执行进程组。

## 代码怎样对应正文

```text
固定的 train / validation / holdout
  |
  +-- train ---- evaluate ---- Hermes 新 Home / SessionDB
  |                |             +-- 本候选 SKILL.md
  |                |             +-- 只读 incidentctl 故障快照
  |                +-- 原数据集的业务评分 + 工具调用与回执
  |                +-- make_reflective_dataset ---- 反思模型
  |
  +-- validation ---- 官方 GEPA 接受检验后的验证与 Pareto 选择
  |
  +-- 选定具体候选并原样导出 ---- holdout 独立比较，不反馈给搜索
```

- [adapter.py](adapter.py) 直接实现 `GEPAAdapter.evaluate` 与 `make_reflective_dataset`，返回官方 `EvaluationBatch`。反思记录包含任务、完整工具参数与回执、业务字段、未通过的检查项和所需引用，错误集群记录不会被裁掉。
- 默认九题路径由 [runner.py](runner.py) 复用第18讲 `isolation.launch`；第19讲数据包路径由 [enterprise.py](enterprise.py) 调用第19讲执行器。两条路径都使用真正的 `AIAgent.run_conversation` 和独立 Session，没有重写 Hermes 循环。
- 默认九题路径使用第18讲 `alert_scorer.grade`：根因、受影响服务、引用归属、请求回执配对、集群过滤和技能读取必须同时通过才记 `1`，任务失败记 `0`。第19讲路径原样使用其 `grader.grade`，保留生产证据、排除证据、时间和快照校验。两条路径都将基础设施故障与业务低分分开，不以关键词重合度替代诊断。
- [reflection.py](reflection.py) 只实现官方允许的模型回调，显式发送到 `GLM_BASE_URL`。候选如何生成与接受仍由 GEPA 决定。
- [support.py](support.py) 原样保留 YAML 文件头。最后只读取 `result.best_candidate['skill_body']`；验证平均分没有严格超过 baseline，或者正文没变，就保留 baseline。不会强挑一个内容不同的候选制造成功。

**输出规则失败不等于业务诊断退步。** 默认九题路径的反馈分别记录业务与工具检查、`schema_pass`（回答字段结构）、`format_pass`（纯 JSON 格式）及 `needs_review`（需要人工复核）。单个 JSON 代码块只要内容检查通过且没有块外文字，仍可得 `1`；块外附加说明映射为 `0` 并保留待复核原因。第19讲的评分协议要求纯 JSON，不沿用这一宽容规则。两条路径都保留原始 `grading`，不能仅凭格式不合规断言根因判断有错。

## 数据与结果的边界

[dataset.py](dataset.py) 的默认示例含九个固定构造事故，训练、验证、留出各三题，都覆盖单告警、多告警及依据不足。不同集合使用不同事故编号、日期、服务和集群，不重复同一快照；这仍是同类任务的教学变体，不能据此宣称对生产分布的泛化效果。实际接入企业数据使用上述清单，直接复用第19讲执行器与评分器；不按对话片段随机拆分，也不把两套评分口径拼成对照。

默认九题的三组已确认根因分别为 `db-gateway`、`orders-db`、`booking-db`，不能靠记住训练答案完成其他集合。种子方法沿点名服务调查，缺少完整多告警聚合步骤；这是可优化的合理缺口，不是要求模型故意查错。快照仅提供业务观测，不包含 `expected`、所需引用清单或“根因角色”评分标注。

默认九题复用的第18讲工具没有时间参数，因此每题固定一个时间窗口，只提供该窗口的快照；不能据此声称测试了多窗口查询过滤。`cluster=all` 会返回其他集群的干扰数据，评分和反思材料保留这一实际错误。第19讲的数据包路径则沿用其带时间参数的 `monitor_query`。两条路径都不向执行进程开放答案、评分器和其他候选目录。

每次运行输出独立 `runtime/<编号>/REPORT.md`。从这里查看配置、题库、逐题执行及评分、反思请求、候选谱系、验证分数、留出对照和导出的 `SKILL.md`。同一 baseline 不重复制造“前后对照”；验证有改进但留出退步时报告拒绝采用，导出仅作为审阅候选，不自动覆盖原 Skill。运行目录可能含完整业务轨迹，已从 Git 排除，不直接发布。

## 系统设计题与练习

你的团队已有 Hermes On-call Agent、Skill 自主修订、业务 Eval 和历史故障数据。请补齐离线优化设计，并沿下面的代码入口检查你的判断。

1. 阅读 `evaluate` 和 `make_reflective_dataset`，指出“漏集群过滤”如何从真实调用进入反馈，为什么只有低分不足以指明修改方向。用离线测试检查参数、回执及具体失败原因都没有丢失。
2. 检查官方 GEPA 结果中的 `val_subscores`、`parents` 和 `per_val_instance_best_candidates`，解释为什么不能只保留平均分最高的候选。练习不要求必然观察到多个候选，也不伪造 Pareto 成绩。
3. 在获准的预算内运行一次，说明哪一步是 Hermes 执行、哪一步是业务评分、哪一步是 GEPA 搜索，并核对 train、validation、holdout 的用途。没有改进也应如实保留 baseline。
4. 假设验证分提高、导出文件却未变化，依次核查优化组件、实际 `skill_view` 回执、候选选择和导出正文哈希。区分“错误地导出旧变量”和“没有严格改进而主动保持 baseline”。

官方接口依据：[GEPA v0.0.27 API](https://github.com/gepa-ai/gepa/blob/v0.0.27/src/gepa/api.py)、[GEPAAdapter](https://github.com/gepa-ai/gepa/blob/v0.0.27/src/gepa/core/adapter.py)、[GEPAResult](https://github.com/gepa-ai/gepa/blob/v0.0.27/src/gepa/core/result.py)。
