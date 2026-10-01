# Lab 02｜一封迟到的信：告警怎样改变正在运行的任务？

[打开 notebook](workshop.ipynb)，用 Python 3.12 内核从头运行。未设置 `GLM_API_KEY` 时，准备格会弹出隐藏输入框；全程只需输入这一个密钥。缺失的 `openai-codex==0.154.0`、`litellm[proxy]==1.101.0` 和 `httpx[socks]` 会自动补装。

这是专栏《自进化 Agent 实战》02 讲《一封迟到的信：怎样改变一个正在工作的 Agent？》的配套练习。巡检正在等待采集时收到告警：先核对输入投递，再观察后续请求怎样同时携带告警与已有材料，最后独立观察中断及其留下的状态。实例名、版本、时间与数值均为构造值，不对应真实事故。

| 十二格导览 | 动手做什么 | 跑完看什么 |
| --- | --- | --- |
| cell0—2｜题面、参考答案与准备 | 阅读设计题，准备构造材料与连接 | 投递、生效、停止是三个不同的观察对象 |
| cell3—5｜巡检进行中：材料读入与告警窗口 | 启动巡检，等待两份成功读取回执与约 25 秒探针的启动标记 | 对象、本次角色、已读材料、探针状态 |
| cell6—8｜告警交给活跃运行：保留上下文、改变方向 | 向活跃运行追加告警，等待调查及后续巡检，定位告警首次进入的请求 | 时间线、动作、运行编号、告警前读取、同一请求中的部署回执 |
| cell9—11｜独立慢任务上的中断：停止不等于回滚 | 新建会话运行约 40 秒探针，请求中断并等待终态 | 运行终态、已有标记、报告、业务材料、脚本与进程状态 |

第一阶段使用 `materials/servers/server-17/` 与 `server-31/` 中的部署和监控材料。支付服务的构造变更为 01:40 将连接池上限从 200 调到 60；订单服务作为剩余巡检对象。每次准备都会复制材料到全新的 `.runtime/<运行编号>/work/`，并追加随机材料编号。代码直接调用 `thread.turn(PATROL_TASK)`，由 `TurnTrace` 的单个消费者记录事件流；`successful_reads` 检查独立 `cat` 的成功回执与完整文件内容，再等待 `patrol_probe.py` 的启动标记。这些读取方式与等待时长是观察条件，不是推荐的通用巡检方法。

第二阶段直接调用 `turn.steer(ALERT)`，核对返回的运行编号与原运行一致。投递前记录已捕获的请求数量；结束后在真实请求中查找随机 `CONTROL-…` 编号首次出现的位置，并用 `tool_output_contains` 检查同一请求的工具结果是否仍带着部署材料编号。表格按本次事件展示动作与报告原文；不把接口返回成功、固定请求序号或报告自述当成转向已经成立。验收还检查告警后没有重新读取支付服务原材料、完成订单服务的后续读取，并生成两份非空报告；根因判断仍须对回材料。

第三阶段在新会话直接调用 `turn2.interrupt()`，等待 `interrupted` 终态。探针不操作业务服务，只写本地启动标记；比较报告是否生成、业务材料与脚本的 SHA-256 是否变化。另用启动标记中的进程号执行零信号查询，记录终态时与关闭连接后的进程状态，不以运行终态代替进程检查。已有标记不会因中断自动回滚。

全 notebook 共十二格、七个代码格，每格 3—12 行。每阶段三行引导、两个代码格，三张过程表格均左对齐并带 `act-result` 样式标记；最后一格显示结果后自动关闭连接。中途代码格报错也会释放连接；从准备格重跑会关闭旧连接、创建新目录。`examples/notebook02_setup.py` 负责依赖和隐藏输入，`examples/notebook02_story.py` 负责材料、表格、留痕与收尾；本讲沿用 `control_validation.py` 中的事件及读取逻辑。模型连接由只读的 `examples/notebook_support.py` 提供，使用 GLM-5.2。

运行沿用 `Sandbox.full_access` 与 `ApprovalMode.deny_all`，外层权限继续生效。工作目录不代表操作系统隔离；本练习只使用构造材料，不对真实服务器操作。运行目录保存模型请求、两项运行的事件与终态、三阶段记录、输入与脚本快照、关闭记录。

从仓库根目录运行自动化核验；使用安装了 Jupyter 执行组件的 Python 3.12。实际模型核验先在环境中设置 `GLM_API_KEY`：

```bash
.venv/bin/python examples/02-instruction-control/run_validation.py --mode offline
.venv/bin/python examples/02-instruction-control/run_validation.py --mode live
```

验收器把环境凭据送入隐藏输入分支，检查实际模型路径只输入一次。离线核验设置 `LAB_VALIDATION_RUN_ID`，无需密钥，不启动 Lab、不发模型请求、不产生 `adapter.log`；只展示构造材料，不生成模拟工具事件、读取回执、报告或中断终态，因此不能证明模型行为。

两条路径分别产出 `validation/runs/nb02-v1-<模式>-<时间与随机后缀>/`，包含 `result.json`、`status.json`、`executed.ipynb` 与 `reading.html`；`validation/offline-latest.json`、`validation/live-latest.json` 指向各自最近一次结果。源 notebook 始终不带执行输出。
