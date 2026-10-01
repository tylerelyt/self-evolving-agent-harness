# Lab 06｜怎样借助旧经历，开始一场新的调查？

[打开 notebook](workshop.ipynb)，使用 Python 3.12 内核。这是专栏《自进化 Agent 实战》06 讲的配套练习，以会话上下文归属、历史边界继承和新时间线为主线。材料中的平台、实例、版本、时间、编号与数值均为构造值，不对应真实事故。

默认从 Notebook 在线运行。安装了 Jupyter 执行组件的 Python 3.12 也可以从仓库根目录分别验证离线材料与真实调用：

```bash
.venv/bin/python examples/06-thread-fork/run_validation.py --mode offline
.venv/bin/python examples/06-thread-fork/run_validation.py --mode live
```

离线不需要密钥，不安装依赖，不启动公共 Lab、litellm 代理或模型客户端；验证器还阻止加载模型设施、连接外网及启动无关进程。它完整执行七个代码格，实际查询材料、切分构造历史前缀并运行内存重放，不制造模型回答、Thread ID、请求或工具回执。也可以设置 `LAB_NOTEBOOK06_MODE=offline` 后启动 Jupyter，依次运行 notebook。

| 十二格导览 | 实际操作 | 跑完核对 |
| --- | --- | --- |
| cell0—2｜题面、答案与准备 | 阅读设计题，准备两套材料目录 | 上下文归属和当前目录为何需要分开考虑 |
| cell3—5｜Agent 为什么一直在排查另一套系统？ | 给会话积累新平台背景，换到历史模块目录提交告警 | 会话背景、当前目录、查询入口与实际回答 |
| cell6—8｜Fork 怎样为新的事故建立独立的 Thread？ | 选择背景轮，按包含边界创建分支 | 保留的前缀、排除的旧观察、来源与独立编号 |
| cell9—11｜新 Thread 怎样接回模块背景，重新调查当前告警？ | 重新确认入口，读取当前发布、日志、指标、配置和代码 | 当前调查、父记录未变、材料未变、连接关闭 |

离线路径中的“会话积累”和“创建分支”只展示相应构造材料及边界选择，表格明确标示未运行模型、未创建真实分支。在线路径才执行这些会话动作，二者的验证结论分别记录。

`materials/new-platform/` 提供灰度发布、服务网格与 Sidecar 背景；`materials/legacy-payment/` 提供独立部署的历史模块告警与当前材料。`materials/old-thread.json` 将旧调查分为六轮：第一轮只有模块背景，后续才有旧事故的发布观察、连接等待、连接池上限从 800 降到 200、恢复与收尾。背景与旧结论若写在同一轮，分岔接口不能按句子筛除旧结论。

当前材料中，连接池上限仍为 800，近期没有调整；新版本 2.8 启动后出现错误，旧版实例正常。`retry.py` 构造了先取连接再校验请求、校验失败时没有归还连接的路径；`replay_retry.py` 用内存计数，以缩小的构造上限 8 重放 12 次请求，显示活跃连接 8、等待 4。结合当前日志、配置和这次重放，可以看清未归还的连接怎样逐步占满连接池。

在线准备沿用 `openai-codex==0.154.0`、`litellm[proxy]==1.101.0` 和公共 `notebook_support.py`，需要 `GLM_API_KEY`；未设置时弹出隐藏输入框，仅输入一次。准备时可补装缺失依赖。在线共九轮：新平台背景一轮、错误会话调查一轮、旧事故材料六轮、新调查一轮；错误会话是否沿用新平台入口，以实际轨迹为准，未出现时验证会失败。

固定版本的高层 Thread 没有直接暴露选轮分岔方法。cell8 通过 SDK 底层 `CodexClient.thread_fork` 发送 `thread/fork`，用 `lastTurnId` 选择已结束的第一轮；这个位置包含在继承范围内。读取新分支核对 `forkedFromId`、继承轮次与工作目录，再用新分支启动今天的调查：保留模块背景，让今天的观察和判断沿自己的时间线继续。

Notebook 直接展示 `Codex.thread_start()`、`Thread.turn()`、`TurnHandle.run()`，并将 SDK 返回的历史轮编号传给 `thread_fork()`。`lab.watch()` 只负责超时保护；`lab.record_result()` 只保存返回结果，不代替模型执行调查。

在线采用 `Sandbox.full_access` 和拒绝审批策略，外层权限继续生效。会话分支不提供文件快照或目录隔离；本练习只调查构造材料，不修改业务输入。最后一格显示结果后自动关闭模型、分岔客户端和代理连接；代码格报错也会收尾，从准备格重跑会关闭旧连接并另建目录。

两种模式分别保存到 `validation/runs/nb06-v1-<模式>-<时间>-<随机后缀>/`，包括 `result.json`、`status.json`、`executed.ipynb` 与 `reading.html`；`offline-latest.json`、`live-latest.json` 分别指向各自记录。源 notebook 保持无输出。独立的 `.runtime/` 保存材料快照、阶段记录、历史边界及关闭记录；在线才有模型请求和 Turn 结果。
