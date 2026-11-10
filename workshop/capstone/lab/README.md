# 实作｜让巡检 Agent 接手一次告警

支付服务已经在接收请求，Hermes 也能跑完一轮健康巡检。接下来由你把监控发出的告警接进当前会话，从初判报告继续调查，再把结果交给值班员。最后给学到的新方法加一道发布门槛。

四关都在 `self_improvement_agent.py` 里完成。仓库已经提供 HTTP 服务、监控替身、巡检 Skill、Hermes SessionDB 和业务评分器。 [起始代码](self_improvement_agent.py)

## 先确认巡检能跑

从仓库根目录运行。默认用本地脚本模型，无需模型密钥。

```bash
bash scripts/setup_deps.sh
PY=.deps/hermes-agent/.venv/bin/python
LAB=workshop/capstone/lab
"$PY" -B "$LAB/check.py" --part 0
```

看到 `PASS 0` 后，打开打印出的运行目录。`healthy.json` 里有这次巡检的 `session_id`、工具回执 Item 和报告 Item。先跑 `--part 1`，看看当前巡检在收到监控告警之后缺了哪一步。

| 关卡 | 你要补的函数 | 跑通后看什么 |
| --- | --- | --- |
| 1 · 投递告警 | `route_alert` | `triage.json` 中，同一 Session 收到原始监控事件，读取 On-call Skill，随后得到 HTTP 探测回执 |
| 2 · 从报告继续查 | `branch_report` | `investigation.json` 中，子 Session 指向初判报告 Item，并保留三次新探测 |
| 3 · 把结果交给人 | `notify_operator` | `page.json` 引用真实分支报告，`continued.json` 显示原巡检 Session 又写了一条检查点 |
| 4 · 给新 Skill 放行 | `release_policy` | `evaluation/evaluation.json` 有三题的新旧对照。候选合格，缺证据、回归和无增益的反例被拒绝 |

### 1. 把告警交给正在运行的回合

`route_alert` 收到已核对范围的原始监控事件和当前回合的 `steer` 回调。第一行以 `ONCALL_ALERT ` 开头，后面跟完整的告警 JSON。下一行让 Hermes 通过 `skill_view` 读取 `oncall-alert-triage`。把 `steer` 的返回值交还给宿主，由宿主确认回合是否接收。

```bash
"$PY" -B "$LAB/check.py" --part 1
```

### 2. 从那条初判报告分支

`record` 里有初判 `session_id` 和 `report_item_id`。用 `checkpoint_rows` 取到该 Item 为止的消息，创建一个带父 Session ID 的新 Session，再复制这段前缀。返回新 Session ID，以及原 Item ID 到复制后消息的映射。`checkpoint_rows` 和 SessionDB 的现有用法可以从 [运行时接口](../patrol/hermes_runtime.py) 查到。

```bash
"$PY" -B "$LAB/check.py" --part 2
```

### 3. 交接后继续巡检

把 `notify_operator` 收到的调查结果写到给定 `path`。收件箱回执包含 `incident_id`、`session_id`、`report_item_id` 和 `report`。另外写上调用方给你的 `scope`、`recipient`、`evidence`，以及 `channel` 为 `local_oncall_inbox`、`paged_at` 为当前时间。可以用 `environment.write_json` 原子写入。复跑验收会检查同一事故是否只留下一个通知，以及原巡检会话是否又写入一条检查点。

```bash
"$PY" -B "$LAB/check.py" --part 3
```

前三关通过后，可以用自己的代码持续值守。三个终端都从仓库根目录运行。终端 A 先建一轮独立状态目录，把打印出的路径复制到 B、C。

```bash
# 终端 A
STATE=$(mktemp -d)
echo "$STATE"
.deps/hermes-agent/.venv/bin/python -B workshop/capstone/patrol/environment.py --state "$STATE" serve
```

```bash
# 终端 B
STATE=刚才打印的路径
.deps/hermes-agent/.venv/bin/python -B workshop/capstone/lab/run.py --state "$STATE"
```

```bash
# 终端 C，等终端 B 出现 healthy 再运行
STATE=刚才打印的路径
PY=.deps/hermes-agent/.venv/bin/python
ENV=workshop/capstone/patrol/environment.py
"$PY" -B "$ENV" --state "$STATE" fault payments on
```

等终端 B 打印值班通知，打开 `$STATE/pages/<事故 ID>/page.json` 核对它引用的报告。再用同一条故障命令把 `on` 改成 `off`。观察巡检继续运行，后续两个独立健康窗口提交后可以用 `patrol.py --state "$STATE" status` 查看恢复检查点。结束时先在 B 停巡检，再在 A 停业务环境。

### 4. 让独立评测决定是否采用候选

这关提供一份教学候选。它要求第一次探测为 5xx 时再查一次，用来区分持续失败和间歇失败。评测端让两版 Skill 分别在新 Hermes 会话中查询同一批冻结快照，传给你的 `results` 是逐题的 `baseline` 和 `candidate` 判分。检查两版的 `valid_trace`、候选的 `passed`、是否有至少一道从失败变为通过，以及两份 Skill 的指纹是否不同。只在这几项都成立时返回 `True`。

```bash
"$PY" -B "$LAB/check.py" --part 4
```

这一关把验收决定留在本轮目录。想继续观察事故会话进入 SkillClaw、生成候选、正式发布和两端采用，可接着跑[双实例完整演练](../README.md#运行双实例演练)。

需要核对接口时，可以加 `--solution` 跑参考实现。每次验收会创建新目录，并在结束时停止本轮业务环境。 [参考实现](solutions/self_improvement_agent.py)
