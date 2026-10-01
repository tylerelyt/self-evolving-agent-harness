# 第13讲：回到原始会话，核对到底做过什么

助手说“已经回滚”，不等于平台执行过回滚。核对旧事时，需要找到当时的工具请求，按调用编号找到真实回执，再检查后续业务状态。Memory 保存长期约定；Session 保存经过，两者不能互相代替。

本例沿用 oncall 支付故事，亲手运行“定位旧会话 → 按窗口展开 → 配对请求和回执 → 核对并纠正总结”。所有业务对象、日志和数值都是构造数据，不连接生产；模型调用和工具执行使用真实接口。

## 对应课后系统设计题

题目要求为 7×24 小时在线的 prod 巡检 Agent 设计一种能力：有人追问或总结有疑点时，它能自己找回当时的记录，核实做过什么、结果怎样，再纠正总结。不依赖人每次翻记录，不全量重读历史，也不为查不到的部分编造故事。这是一道系统设计题；下面的代码用于观察关键机制，不是生产部署方案或自动评分的标准答案。

| 题目要求 | 代码入口与学员动作 | 要观察的请求和回执 |
| --- | --- | --- |
| 留下可核对的真实经过 | 在 Notebook 的事故格提交调查，或运行 `run.sh --live` | `rollback` 的请求编号、平台拒绝及 `task_created=false`；暂停完成后的部署和日志查询 |
| Agent 自己按需找回记录 | 在新核查格提交待核对的草稿，不把旧会话编号或查询结果发给模型 | 模型实际调用 `session_search(query=...)`，从命中位置用 `around_message_id`、`window` 展开，而不是只读另一份总结 |
| 核实做过什么、结果怎样 | 直接运行发现、展开和配对格，对照模型的判断 | `assistant.tool_calls[].id` 与 `tool.tool_call_id` 一一对应；受理、完成、业务复查分开看 |
| 纠正总结，保留未知 | 阅读核查会话的最终回复，自行判断修订是否有依据 | 引用会话和消息位置；不把未命中写成未发生，不把先后关系写成恢复原因 |

主链是事故会话与独立核查会话。演示还增加一个只允许 `memory` 的前台会话，用来观察记忆工具同样能留下可回查的请求和回执，因此完整演示共有三个前台任务。它不重复记忆迁移，也不替学员决定系统该有多少会话。

原生后台复盘会关闭 SessionDB 持久化，不能拿它的观察记录代替原始会话。本例关闭自动复盘，另用前台 memory 展示持久化。核查结果只在回复中修订，不自动写入 Memory 或 Skill。

系统设计仍需考虑在线服务与核查的调度隔离、查询范围，以及何时将核实过的结论沉淀为经验。本例服务使用串行队列，**不证明后台核查不会占用告警处理路径**。这些设计取舍保留给学员分析，不用一次演示通过代替设计论证。

## 准备环境

需要 Git、Bash、Python 和智谱 GLM 调用权限。在仓库根目录执行：

```bash
bash scripts/setup_deps.sh
bash scripts/setup_deps.sh --check
read -r -s GLM_API_KEY
export GLM_API_KEY
```

`read` 隐藏输入凭据，不要把密钥写进 Notebook。也可沿用第12讲的 `~/.hermes/.env`。默认模型为 `glm-5.2`，地址为 `https://open.bigmodel.cn/api/paas/v4`，可通过 `GLM_MODEL`、`GLM_BASE_URL` 改成与凭据匹配的 GLM 配置。

环境脚本获取固定提交的 Hermes，并建立 `.deps/hermes-agent/.venv`。新环境使用 Python 3.12，已有环境支持范围以脚本检查为准。运行器默认使用这套解释器；已有独立环境可以设置 `HERMES_SRC`、`HERMES_PYTHON`。运行本讲不需要旧会话、旧输出或开发目录中的文件。

## 先做无模型检查

从仓库根目录执行：

```bash
bash examples/13-session-lookup/run.sh self-check
```

自检清空继承环境，使用本讲 `runtime/` 内的临时 Home，不读取个人凭据。它实际执行本地原生 memory 工具，把明确标为构造的测试消息写入 SessionDB，检查默认发现、工具检索、窗口展开、同编号回执和重开查询。另测缺失或错配回执、受理与完成的区别、异常退出。**这些测试不是模型自主执行轨迹，不代表完整演示已验收。**

## 运行与停止

完整演示会产生 GLM API 费用，使用显式参数运行：

```bash
bash examples/13-session-lookup/run.sh --live
```

每个 Agent 的 `max_iterations` 默认是 16，可用 `SESSION_LAB_MAX_ITERATIONS` 调整。迭代上限不等于实际 API 请求数，也不是费用上限：达到上限后，Hermes 会额外请求一次不带工具的收尾总结；任务记录中的 `model_requests` 不包含这次总结，实际调用与费用应以供应商或计费代理记录为准。收到收尾总结也不表示任务已经完成。未传 `--live` 且未设置 `SESSION_LOOKUP_ALLOW_LIVE=1` 时，演示在读取凭据或启动服务前退出。

正常结束会停止服务，保留本次记录。需要继续手工检查时使用 `--live --keep-running`，之后执行 `bash examples/13-session-lookup/run.sh down`。`status`、`down` 不触发模型调用。底层 `up`、`send` 用于手动操作；`send` 会提交模型任务，仍会收费。

## Notebook 里直接看关键调用

[workshop.ipynb](workshop.ipynb) 直接展示会话创建、任务提交、`SessionDB(...)`、`session_search(...)`、前后移动窗口和调用编号配对，不再把这些步骤藏在 worker 中。运行前需在内核环境设置 `SESSION_LOOKUP_ALLOW_LIVE=1`，并使用能导入 Hermes 依赖的 Python 内核。

默认发现搜索 `user`、`assistant`，其中也包括 assistant 的工具参数；这不是“只搜自然语言正文”。仅存在于 tool 回执里的词默认不会命中，可以显式设置 `role_filter='tool'`，或先发现会话再展开。展开返回含 tools 的原始消息，不调用第二个模型重新摘要。单个窗口未包含配对回执时，先保持未知，再向前或向后读。

如需在 Notebook 中逐格运行，在 Hermes 解释器环境中另装 Jupyter 依赖，并用同一解释器启动。无需安装前九讲使用的 Codex 适配器：

```bash
.deps/hermes-agent/.venv/bin/python -m pip install jupyterlab nbformat nbclient nbconvert ipykernel jupyter_client
SESSION_LOOKUP_ALLOW_LIVE=1 .deps/hermes-agent/.venv/bin/python -m jupyterlab examples/13-session-lookup/workshop.ipynb
```

如果环境由 `uv` 创建且没有 pip，安装时改用 `uv pip install --python .deps/hermes-agent/.venv/bin/python`，后面接同一组包名。需要批量执行并导出 Notebook 时，用这套解释器执行 `examples/13-session-lookup/run_validation.py --mode live --run-id lesson13-run-01`；它也会调用模型，运行编号不可复用。

Notebook 普通异常会尝试等待本轮任务结束并清理；强杀内核、系统崩溃或清理超时仍须单独核查服务。

## 读结果时保留哪些区别

按 `assistant.tool_calls[].id` 对应 `tool.tool_call_id`，不按相邻消息猜配对。缺少、空缺或重复编号保留 `unknown`；`accepted` 只表示受理，`completed` 只表示那项操作完成。业务是否恢复要看后续部署和日志，操作与恢复之间的因果不能仅靠时间先后认定。

完整运行的统一入口为 `runtime/observations/<本次编号>/REPORT.md`。同目录保存 Session 原始窗口、配对结果、任务、源码哈希和退出回执。程序只检查这些可核对的事实；总结是否修订得当、系统设计是否合理，仍需学员说明依据。端口关闭但没有完整资源清理回执，不会被标为正常退出；等待超时也不等于任务从未执行。

运行目录 `runtime/` 和验证输出 `validation/` 不作为公开附件，也不需要提交。Notebook 源文件不带执行输出，分享前须清空输出并检查凭据、个人路径和会话内容。

兼容入口 `run_session_lookup.py`、`run_incident_lookup.py` 均进入同一演示。旧 `--mode offline` 和 `LAB_VALIDATION_RUN_ID` 回放入口当前不可用：旧记录缺少前台 memory 会话，不能按本版检查逻辑重评。需要无模型检查时使用 `self-check`，不要将旧结果当作当前运行成功。
