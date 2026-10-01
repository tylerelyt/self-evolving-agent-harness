# Lab 05｜压缩以后，任务状态和原始依据各留下什么？

[打开 notebook](workshop.ipynb)，使用 Python 3.12 内核从头运行。未设置 `GLM_API_KEY` 时，准备格会弹出隐藏输入框；全程只需输入这一个密钥。缺失的 `openai-codex==0.154.0`、`litellm[proxy]==1.101.0` 和 `httpx[socks]` 会自动补装。

这是专栏《自进化 Agent 实战》05 讲《上下文压缩：如何压缩历史才不影响任务延续？》的配套练习。练习使用构造的长任务调查材料，目标是掌握处理真实场景的能力。

| 十二格导览 | 动手做什么 | 跑完看什么 |
| --- | --- | --- |
| cell0—2｜题面、参考答案与准备 | 阅读题面并一键准备 | 原调查状态、当前输入与磁盘材料的区别 |
| cell3—5｜历史被压缩以后，调查还能接着做吗？ | Agent 只读读取日志，在同一会话接受纠正 | “压缩前的依据”和“实际内容” |
| cell6—8｜上下文更新后，什么不该随之改变？ | 发起原生压缩，等完成项，再结构化复述 | “比较项”“压缩前”“压缩后” |
| cell9—11｜Codex 怎样做 Compaction？ | 追问精确采样并对照原文，读取完整历史 | “保存位置”和“采样原文是否可找到” |

第一阶段由 [材料生成脚本](materials/generate_capacity.py) 在每次运行目录写入 `capacity.log`：180 行正常采样以及末尾异常记录，保留任务编号 `CASE-527`。服务、版本、时间、编号和全部指标都是构造值，`elapsed_us` 每次随机生成。模型先通过工具完整读取文件，再收到纠正：发布前已有同类报错，发布与故障之间的因果关系仍待验证；连接等待与错误率同时升高，先核对连接池配置；不许重启。任务状态在同一个 Thread 内累积，不把“仍待验证”写成“已排除”。

第二阶段在 notebook 中直接调用 `thread.compact()`，随后循环调用 `thread.read(include_turns=True)`，等原会话中新增的任务轮状态为 `completed`，且包含 `contextCompaction` 项，才允许继续。失败、中断和超时都会停止后续调用。模型随后只输出六个字段：`task`（任务编号）、`release_status`（发布判断）、`reason`（判断依据）、`next_step`（下一步）、`next_reason`（行动理由）、`constraint`（操作限制）。对照表保留实际回答；请求记录用于比较压缩前后 `input` 的字符数，不把字符数解释成 token 或费用。

第三阶段只提出 `sample=0073` 与 `sample=0127` 的数值和差值，不将原文或答案重新交给模型，也不指定读取命令。检查精确追问开始时是否已有两条采样的依据，再按调用编号找本轮新增的成功回执。摘要若保留了所需数值，没有回读也正常；缺少依据时应重新读取。最终回答逐项对照磁盘原文，并检查压缩前的调查轮次仍能从 `thread.read(include_turns=True)` 取得，工作目录全部输入文件 SHA-256 未变。

源 notebook 为十二格、七个代码格，无断言、无执行输出，结尾直接显示表格并关闭连接。[准备脚本](../notebook05_setup.py) 管理依赖、隐藏输入和旧连接；[过程脚本](../notebook05_story.py) 管理构造材料、三张对照表、运行记录和收尾。会话创建 `thread_start()`、提交任务 `turn()`、等待结果 `run()`、原生压缩及其完成检查都在 notebook 中直接执行。`lab.watch()` 只做超时保护，`lab.record_result()` 只保存 SDK 返回的结果。代码格报错也会释放连接，重新准备会创建新的运行目录。

连接由 `notebook_support.py` 准备：openai_codex 的会话对象保存在 Python 进程中，底层 Codex 通过 SDK 启动的 `app-server` 子进程执行，litellm 代理层连接 GLM-5.2。Notebook 明确传入 `Sandbox.full_access` 和 `ApprovalMode.deny_all`，外层权限仍然生效。

用装有 notebook 执行依赖的 Python 3.12 核验：

```bash
python examples/05-context-compression/run_validation.py --mode offline
python examples/05-context-compression/run_validation.py --mode live
```

离线模式读取 `LAB_VALIDATION_RUN_ID`，只准备和展示材料、不启动 Lab，不产生模型请求，也不伪造压缩项、模型复述或工具回执。在线模式从环境取得 `GLM_API_KEY` 后经过隐藏输入分支执行整本，要求输入一次；它验证真实压缩完成、输入变短、历史保留、判断与约束不漂移，以及精确引用有依据。

两种模式保存到 `validation/runs/nb05-v1-<模式>-<时间>-<随机后缀>/`，含执行 notebook、`reading.html`、逐格耗时和检查结果。`offline-latest.json` 与 `live-latest.json` 分别指向最近一次记录。原始材料、请求、任务轮、压缩项、历史与关闭记录保存在独立的 `.runtime/` 目录；分享时使用无输出的源 notebook。
