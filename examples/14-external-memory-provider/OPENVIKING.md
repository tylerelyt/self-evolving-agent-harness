# 进阶：接入原生 OpenViking

这是完成[文件与 SQLite 主练习](README.md)之后的可选实验，不是本讲主练习的安装前提。用 Hermes 原生 OpenViking Provider 连接资料与外部记忆，区分前台回答、消息保存和经验提炼。

默认命令只做无费用的消息转换。真实实验需要单独运行 OpenViking 服务和远程 GLM；客户端完成后自动释放 Provider，服务在自己的终端按 `Ctrl-C` 停止。

## 准备环境

在仓库根目录执行，使用 Python 3.12：

```bash
bash scripts/setup_deps.sh --check
# 尚未准备 Hermes 时，先运行 bash scripts/setup_deps.sh
PYTHON=python3.12 bash scripts/setup_openviking.sh
PYTHON=python3.12 bash scripts/setup_openviking.sh --check
```

Hermes 固定为 `aaf96885`。OpenViking 使用官方 [PyPI 0.4.12](https://pypi.org/project/openviking/0.4.12/)，版本来源见 `scripts/openviking-revision.txt`。安装器只接受 wheel，不 clone 源码、不编译、不下载本地模型；没有兼容 wheel 时会退出。服务环境位于本目录 `runtime/native/venv`，不向 Hermes 或 Notebook 环境安装 OpenViking。

安装器会先下载依赖、报告解压大小，再安装；`--download-only` 只完成前一步，`--wheelhouse <目录>` 可选用完整离线包。每步至少保留1GiB可用空间。本机 macOS arm64 / Python 3.12 的依赖预估为下载156MiB、解压511MiB，实际占用以安装器报告为准。

安装默认直连；需要代理时，以 `PROXY=<你的HTTP代理地址> PYTHON=python3.12 bash scripts/setup_openviking.sh` 显式指定。它只作用于本次安装进程，不修改系统设置。

## 先看消息转换

```bash
bash examples/14-external-memory-provider/run.sh native
```

这里不启动 Agent 或服务，而是直接调用固定 Hermes 的 `_messages_to_openviking_batch()`。构造输入包含用户、助手、业务工具和 `viking_search` 回执。检查输出中的 `tool_id`、`tool_input`、`tool_output` 和助手 `peer_id`：业务工具按调用ID配对，检索自身的调用与回执被排除，不把旧资料重复保存成新经历。

## 运行原生 OpenViking

需要智谱 `GLM_API_KEY`，只通过进程环境提供，不写入源码或 Notebook。服务与 Agent 默认使用官方按量接口，模型固定为 `glm-5.2` 和 `embedding-3`。只接入本例的纯文本资料，不使用外部视觉模型。

在服务终端中执行：

```bash
cd examples/14-external-memory-provider
# 在当前终端安全地设置 GLM_API_KEY，避免把真实值写入共享命令记录。
CONFIG=$(python3.12 openviking_config.py --write)
runtime/native/venv/bin/python -B run_openviking_server.py \
  --config "$CONFIG" --allow-model-calls
```

服务只监听 `127.0.0.1:1933`。需要本机转发时，在生成配置前设置 `GLM_BASE_URL`；Embedding 可单独设置 `GLM_EMBEDDING_BASE_URL`，未设置则沿用前者。两者均传入 base URL，不附加 `/chat/completions` 或 `/embeddings`。客户端终端也要设置相同的 GLM 地址与凭据。

另开终端，在本目录运行：

```bash
# 资料接入、模型检索和回答是真实请求，会产生费用。
bash run.sh native --live --agent-run --ingest --agent investigator-A \
  --query '请根据企业资料给出报表延迟的排查顺序，引用资料地址，并说明哪些结论还需要现场确认。'
```

`viking_add_resource(url=..., wait=True)` 上传随仓库提供的 [报表手册](resources/report-runbook.md)。随后 `AIAgent.run_conversation()` 通过原生 `prefetch` 取得初始线索，也可自主调用 `viking_search`、`viking_read` 补充资料。代码不替模型编造工具调用或回答。

想单独观察分层读取，可以使用搜索回执中**真实返回的 URI**：

```bash
bash run.sh native --live --recall-only --read-uri '<搜索返回的viking://地址>'
```

该命令是接口实验，依次读取摘要、概览和原文，不代表 Agent 自主选择了层级。

## 会话提交与第二个实例

`sync_turn` 保存本轮新信息，不等于会话提交。先核对同步回读；需要让本次会话进入抽取，再在运行命令中加 `--commit`。提交可能产生额外模型调用。配置显式开启经验抽取，并在 `memory_policy.memory_types` 中包含 `experiences`；仅列出 `cases`、`trajectories` 不够。

```bash
bash run.sh native --live --agent-run --agent investigator-A --commit \
  --query '根据共享资料解释报表延迟时怎样避免把历史原因误当成当前原因。引用来源，不声称已经完成生产处置。'

# 等后端完成抽取和索引后，使用独立进程、独立 Session 和新身份。
bash run.sh native --live --agent-run --agent investigator-B \
  --query '报表再次延迟。请结合可检索的资料和既有经验，列出排查顺序与必须重新核实的条件。'
```

A、B 使用相同 account/user 共享授权范围，`OPENVIKING_AGENT` 是不同的 peer 身份。默认是仅限本机的教学部署，不能用这个配置替代生产鉴权。B能否检索并正确使用A的新经验，必须看本次回执；检索到同一份企业手册，不算证明经验复用。

## 看什么、怎样停止

关键代码在 [native_openviking.py](native_openviking.py)，逐步实验见 [openviking.ipynb](openviking.ipynb)。Notebook仍使用原来的内核，实际调用放到 Hermes 子进程，服务保持在独立环境。

每次客户端使用全新 `runtime/native/clients/lesson14-.../`。重点看 `conversation.json` 中模型实际调用和回答、`prefetch.json` 中召回内容、`write-receipt.json` 中后端消息与 `write_verified`，以及显式提交后的 `commit-receipt.json`。`commit_accepted` 只表示提交被接受，不表示经验已经生成。读取失败可以继续前台任务；写入失败或回读不完整必须单独检查，不能用“模型正常回答”代替保存成功。

客户端用 `with NativeSession(...)` 清理 Manager、Provider 和 AIAgent；异常退出不自动 commit。服务在前台按 `Ctrl-C` 停止，运行文件保留便于检查。`runtime/` 已忽略，包含会话、资料和日志；不要提交这些文件，也不要发布带真实输出的 Notebook。

## 旧业务案例的可靠性对照

`oncall_memory.py`、`shared_store.py` 是自定义 SQLite Provider，不是 OpenViking。它只用来观察本地待传队列、真实后端打不开、恢复后的幂等补传，以及跨实例或错误时序不能拼成成功案例。无需历史回放文件或模型凭据：

```bash
bash run.sh check SQLiteTests
```

完整离线接口检查运行 `../../.deps/hermes-agent/.venv/bin/python -B test_offline.py`。SQLite 对照与原生 OpenViking 分别进入，不启动旧常驻服务或依赖历史回放。

## 验证状态

已完成原生消息转换、无网络 AIAgent 初始化与接口故障测试，并检查真实 SQLite 文件故障后的正文与数据库完整性。模型检索、OpenViking经验抽取和跨实例使用尚未实跑；这些结果须在启动服务并完成真实调用后核对，不能从离线测试推定。
