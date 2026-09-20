# 02｜一封迟到的信：怎样改变一个正在工作的 Agent

本目录是《自进化 Agent 实战》第 02 讲的随讲实验，演示如何在**不重启 Agent、不丢失上下文**的前提下，改变一个正在运行的 Agent。

- 课程：极客时间《自进化 Agent 实战》第 02 讲
- 场景：On-call Agent 夜间巡检服务器，巡检途中收到支付告警
- 模型：智谱 **GLM-5.2**（经本地 LiteLLM 适配器接入 Codex App Server），不需要 OpenAI / ChatGPT 登录
- 主文件：`workshop.ipynb`（由 `build_notebook.py` 生成）

## 你会观察到什么

1. **steer（转向）**：Agent 正在巡检 server-17（已读到凌晨下调数据库连接池的变更），一条只含服务器、时间和异常现象的告警通过 `TurnHandle.steer()` 进入**同一个 Turn**。Agent 带着巡检阶段的上下文转向故障调查，处理完再继续巡检其余服务器。
2. **interrupt（中断）**：用一个无副作用的慢任务演示 `TurnHandle.interrupt()`，Turn 状态变为 `interrupted`，要求产出的报告不会落盘。

实验用可核对的检查项验证：告警进入原 Turn、故障报告引用了告警到达前已取得的变更信息、巡检被完整完成、输入文件未被修改。

## 前置准备

与第 01 讲相同：

- Python 3.12
- 在虚拟环境中安装固定依赖（仓库根 `requirements.txt`）：
  - `openai-codex==0.154.0`
  - `litellm[proxy]==1.101.0`
- 准备智谱 API Key，并设置环境变量：

```bash
export BIGMODEL_API_KEY="你的智谱 API Key"
```

Notebook 首次运行会在本机启动一个 LiteLLM 协议适配器（随机本地端口、随机本地令牌），把 Codex 的 Responses 协议请求转换为 GLM-5.2 的 Chat Completions 请求；适配器只监听 `127.0.0.1`，关闭 Notebook 前的最后一格会清理进程。

## 运行

```bash
python build_notebook.py
jupyter lab workshop.ipynb
```

也可以直接打开已生成的 `workshop.ipynb`，从上到下依次执行。

## 说明与边界

- 巡检素材（服务器信息、只读探针脚本）都在系统临时目录中生成，不写入仓库。
- `steer()` 的注入时机依赖模型按要求先启动约 25 秒的只读采集探针；极少数情况下若模型跳过探针，可重跑该格。
- `interrupt()` 停止的是 Agent 的 Turn，不保证杀死已经启动的只读探针进程；探针没有业务副作用，教学上以 Turn 状态和"报告未落盘、输入未改"为准，这也对应正文强调的"中断不会回滚已产生的副作用"。
- 本实验不演示破坏性的 `stop()`（关闭整个 Thread）。
