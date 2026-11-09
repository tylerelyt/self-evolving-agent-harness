# 配套实作｜让 Hermes 巡检正在运行的服务

配合[巡检模块说明](README.md)阅读。你会启动两个持续运行的进程。一个托管订单、支付、库存和通知的 HTTP 接口并发出模拟业务流量。另一个按轮次调度 Hermes Agent，通过只读工具查询实际收到的请求、5xx 和 P95。

## 1. 启动服务和流量

从仓库根目录运行，使用课程固定的 Hermes 依赖。先在终端 A 启动环境。

```bash
bash scripts/setup_deps.sh
.deps/hermes-agent/.venv/bin/python workshop/capstone/patrol/environment.py serve
```

环境会打印四个 loopback 地址。各服务在 `/work` 处理业务请求，在 `/metrics` 报告最近 30 秒的观测。流量发生器实际请求 `/work`。通知服务约每 8 秒收到一个请求，因此通常落在样本不足区间。建议等几秒让其他服务积累第一批请求。

## 2. 启动 Hermes 持续巡检

在终端 B，用课程已配置的模型凭据 `GLM_API_KEY`。可通过 `GLM_MODEL` 和 `GLM_BASE_URL` 选择课程使用的模型与端点。线上模型会按服务巡检持续产生请求，实验结束时用 Ctrl-C 停止。

```bash
PY=.deps/hermes-agent/.venv/bin/python
P=workshop/capstone/patrol/patrol.py
"$PY" "$P" run --interval 10 --sample 2
```

默认每轮抽两个服务，轮次结束后等待 10 秒。Hermes 在巡检会话里执行 `service_snapshot` 和按需执行的 `search_inspections`。宿主只在读回工具回执和报告并核对以后，才写知识库和推进检查点。

手边没有模型凭据时，可给同一条命令加 `--offline`。本地脚本端点负责回复模型请求，Hermes 的工具执行、HTTP 遥测和会话存储照常运行。这个入口适合先查接线。

## 3. 让支付服务出一次故障

在终端 C，确认巡检已记录过健康的支付窗口，然后保持巡检运行。

```bash
E=workshop/capstone/patrol/environment.py
PY=.deps/hermes-agent/.venv/bin/python
P=workshop/capstone/patrol/patrol.py

"$PY" "$E" fault payments on
"$PY" "$E" metrics payments
"$PY" "$P" history payments
```

等到下一次抽到支付、且最近 30 秒的 5xx 比例达到 5%，你会看到 `abnormal`。再关闭故障，等错误请求离开窗口后，下一次巡检会回到 `healthy`。

```bash
"$PY" "$E" fault payments off
"$PY" "$P" history payments
```

对照三条记录，检查观测窗口、5xx 比例、`session_id` 和历史引用。再看通知服务为何仍然是 `insufficient`。

## 4. 从一条检查点恢复和分支

在两轮之间执行以下命令。 `resume` 会从 Hermes SessionDB 读回最近一次已提交的观测和报告。下一次例行巡检也会重用该 Session。`fork` 从保存的报告 Item 截止，复制会话前缀到新的 Hermes Session，并记录父会话。分支里的消息有自己的 Item ID。

```bash
"$PY" "$P" resume payments
"$PY" "$P" fork payments
"$PY" "$P" resume payments
```

两次恢复应该指向同一个巡检 Session，分支则有不同的 Session ID。检查原始回执的内容是否一致。巡检知识库在 `workshop/capstone/runtime/hermes-patrol/patrol.sqlite`，Hermes 原始会话在同目录下的 `hermes/state.db`。需要隔离实验时，给环境与巡检命令都在子命令之前传同一个 `--state` 目录。

验收后，在终端 B 停止巡检，再在终端 A 停止服务。提交健康、异常和恢复各一条巡检记录、一条通知样本不足记录，以及一组 resume 与 fork 的会话坐标。

[environment.py](environment.py) 运行服务和负载。[patrol.py](patrol.py) 负责抽查、检索和提交。[hermes_runtime.py](hermes_runtime.py) 把工具接进 Hermes，并在 SessionDB 中按报告 Item 恢复或创建分支。

接着做[故障注入与 Hermes steer](ONCALL.md)，让指标越过阈值时，正在执行的巡检回合接手 On-call 调查。
