# 第二阶段 · 第一项实作｜让巡检回合接手 On-call

支付开始报错时，巡检 Agent 可能正在读指标，准备照常写报告。实验环境里的监控替身在故障出现后发出告警。Harness 把这条告警交给对应的巡检回合，Agent 再核实请求、说明影响，并把证据交给值班工程师。

监控替身将原始事件写入环境目录下的 `payments.alert.json`。Harness 在指标工具完成时读取这条事件，核对它对应的服务和环境，再调用 Hermes 的 `AIAgent.steer()`。Hermes 将任务附在给模型的工具结果后面。模型先通过 `skill_view` 读取当前的 On-call Skill，再按其中的步骤调用 `oncall_probe`，向支付服务实际发请求。整个过程沿用同一个 Session 和巡检回合。

## 注入故障

先按[第一阶段实作](LAB.md)在终端 A 启动服务和流量。在终端 B 启动带 `--oncall` 的巡检。如果第一阶段的巡检还在运行，先在终端 B 按 Ctrl-C，再用下面的命令启动。

```bash
PY=.deps/hermes-agent/.venv/bin/python
P=workshop/capstone/patrol/patrol.py
"$PY" "$P" run --oncall --interval 5
```

如需用本地脚本模型验接线，再加 `--offline`。保持终端 B 的巡检运行。等支付至少有一条健康记录，在终端 C 执行下面的命令。

```bash
PY=.deps/hermes-agent/.venv/bin/python
P=workshop/capstone/patrol/patrol.py
E=workshop/capstone/patrol/environment.py
"$PY" "$E" fault payments on
```

支付接下来会实际返回 HTTP 503。监控替身在这次注入后观察到 5xx，就发出告警。保持巡检进程运行，等支付进入下一次抽查并接收告警。终端出现 `incident_id` 后，可以查事件。

```bash
"$PY" "$P" incidents payments
```

## 核对 Hermes 做了什么

用事件里的 `inspection_id` 找到异常窗口。事件记录的 `steer_delivery` 保存了 Hermes 附加给模型的内容，`steer_item_id` 指向承载这次投递的原始工具回执。用 `skill_item_id` 核对读取的 Skill 内容与 `skill_sha256`，沿 `probe_item_id` 找到探测结果，再用 `report_item_id` 核对 Agent 报告。在终端 C 打开本地会话库。

```bash
sqlite3 workshop/capstone/runtime/hermes-patrol/hermes/state.db
```

在 SQLite 终端运行 `SELECT id, session_id, role, content FROM messages WHERE id IN (Skill Item 编号, 探测 Item 编号, 报告 Item 编号);`，把括号里的内容换成事件给出的数字。这些 Item 应属于同一个巡检 Session，报告要能对上探测到的 HTTP 状态。异常窗口继续出现时，系统保留这一个打开的事件，巡检会继续写入新观察。

## 从这起事故的报告分出长调查

记下 `incidents payments` 给出的 `incident_id`。保留终端 A 的服务和当前故障，在终端 B 按 Ctrl-C 暂停巡检，再在终端 C 启动长调查。把下面的值换成刚才记录的事故 ID。

```bash
INCIDENT_ID='填入事故 ID'
"$PY" workshop/capstone/patrol/investigation.py \
  --service payments --incident-id "$INCIDENT_ID" --offline \
  --out "workshop/capstone/runtime/hermes-patrol/$INCIDENT_ID-investigation.json"
```

`--out` 须指向已有目录下的新文件。`--offline` 使用本机脚本模型，Hermes 工具和 HTTP 请求照常运行。要用已配置的 GLM，省略该参数。自定义实验目录时，用 `--state` 指向事故所在的巡检状态目录。如果 HTTP 环境使用独立目录，用 `--environment-state` 指向那一目录。

命令从指定事故的原报告 Item 截取会话，在同一个 `state.db` 里创建子 Session。Hermes 重新读取事故原来用的 Skill，依次对同一环境里的支付服务发三次新请求。对照输出 JSON 的父 Session、原报告 Item、分支 Session 和新回执。三次都是 5xx 时，报告在这段观测内持续失败；5xx 和成功响应混合时，报告间歇失败；只有成功响应时，报告本次未复现。HTTP 回执用于判断故障表现，根因保留为未知。后续巡检继续使用原巡检会话。

## 观察恢复

长调查完成后，在终端 B 用前面的命令继续巡检，再在终端 C 关闭实验故障。

```bash
"$PY" "$E" fault payments off
"$PY" "$P" incidents payments
```

等下一次采到健康且 5xx 为零的支付窗口，事件会带上 `recovered_inspection_id`。这条记录回答的是“处置以后，巡检实际看到了什么”。

提交一条事件，标出触发阈值、steer 投递内容、初判回执、分支补查和恢复记录。继续做[双实例演练](../README.md)，核对处置审批怎样引用分支报告。
