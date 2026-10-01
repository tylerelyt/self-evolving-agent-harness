# Lab 01｜Agent 执行引擎：一句话怎样变成连续行动？

[打开 notebook](workshop.ipynb)，用 Python 3.12 内核从头运行。未设置 `GLM_API_KEY` 时，准备格会弹出隐藏输入框；全程只需输入这一个密钥。缺失的 `openai-codex==0.154.0`、`litellm[proxy]==1.101.0` 和 `httpx[socks]` 会自动补装。

这是专栏《自进化 Agent 实战》01 讲《Agent 执行引擎：一句话怎样变成连续行动？》的配套练习。通过构造的告警调查材料，观察真实命令回执怎样进入下一次模型调用，并核对报告与输入边界。

| 十二格导览 | 动手做什么 | 跑完看什么 |
| --- | --- | --- |
| cell0—2｜题面、参考答案与准备 | 阅读系统设计题，一键准备材料与连接 | 连续任务与一次模型调用的区别 |
| cell3—5｜运行循环：一句目标怎样变成一轮轮取数 | 展示告警、日志与代码，在工作目录创建会话并调查 | 材料里有什么、模型实际读了什么、下一轮接着做什么 |
| cell6—8｜一条回执怎样进入下一次调用 | 按调用编号配对命令与回执，寻找日志随机编号 | 真实命令、退出码、回执首次进入的请求序号 |
| cell9—11｜报告与输入：结束不等于归因正确 | 读回报告，比较输入目录前后的内容 | 执行结束、报告写入和输入未变分别证明了什么 |

第一阶段先展示 `inputs/alert.md`、`inputs/logs/payment-api.log` 与 `inputs/src/checkout.py`；服务名、时间、请求编号与数值均标注为构造值。展示给读者的文件不会自动进入模型请求。代码直接调用 `codex.thread_start(...)`、`thread.turn(...)`、`turn.run()`，工作目录、沙箱与批准策略在 Notebook 中可见。任务只给调查目标与材料范围，不指定读取顺序，也不预告异常原因。模型可以合并读取多个文件，表格按实际回执展示。`lab.watch()` 只提供等待进度和超时中断，`lab.record_result()` 只记录 SDK 已返回的结果。

第二阶段使用 `command_receipts(lab.requests[request_start:])` 按 `call_id` 配对真实命令与工具回执，再用 `tool_output_contains(request, loop_marker)` 检查随机编号。第一次请求不应包含文件里的编号，工具成功读取后才会带着它进入后续请求。沿着这个编号，可以看清新取得的材料怎样参与下一轮调用。

第三阶段显示 `outputs/incident-report.md` 的原文，并比较输入文件的 SHA-256。结束状态只说明执行停止，非空报告只说明写入发生，输入未变只说明本次没有改动。报告的事实、推断与待验证项仍要对回日志和代码：缺少 `currency` 与直接访问该字段支持异常机制，却不能证明上游发布了错误版本。目录级隔离与审批留到后面的讲次落实。

全 notebook 共十二格、七个代码格，关键 SDK 调用直接展开。每阶段有引导和两个代码格；第三阶段最后一格展示结果后自动关闭连接。中途代码格报错也会释放连接；从准备格重新运行会关闭旧连接、创建新目录。`examples/notebook01_setup.py` 负责依赖和隐藏输入，`examples/notebook01_story.py` 负责材料、表格、留痕与收尾，`examples/notebook_support.py` 提供连接、等待保护与记录，使用 GLM-5.2。

运行沿用 `Sandbox.full_access` 与 `ApprovalMode.deny_all`，外层权限继续生效。本次工作目录不代表操作系统隔离。每次运行在 `.runtime/<运行编号>/` 保存输入快照、模型请求、Turn 结果、三阶段记录与关闭记录。

自动化核验从仓库根目录运行；解释器使用安装了 Jupyter 执行组件的 Python 3.12，例如本项目的 `.venv/bin/python`。实际模型核验先在环境中设置 `GLM_API_KEY`：

```bash
.venv/bin/python examples/01-agent-loop/run_validation.py --mode offline
.venv/bin/python examples/01-agent-loop/run_validation.py --mode live
```

验收器把环境凭据送入隐藏输入分支，检查实际模型路径只输入一次。离线核验设置 `LAB_VALIDATION_RUN_ID`，不需要密钥，不启动 Lab、不发模型请求、不产生 `adapter.log`；它从构造材料整理示例报告，不制造模拟请求或回执，因此不能证明模型行为。

两条路径分别产出 `validation/runs/nb01-v1-<模式>-<时间与随机后缀>/`，包含 `result.json`、`status.json`、`executed.ipynb` 与 `reading.html`；`validation/offline-latest.json`、`validation/live-latest.json` 指向各自最近一次结果。源 notebook 始终不带执行输出。
