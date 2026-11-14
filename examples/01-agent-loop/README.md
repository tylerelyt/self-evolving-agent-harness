# Lab 01｜Agent 执行引擎：一句话怎样变成连续行动？

关键词：**执行**。输入是 `inputs/` 里的构造告警、日志和代码；输出是调查报告与每条命令的真实回执。

## Run

按 [Notebook 环境](../CODEX.md#前置条件与运行)准备 Python 3.12 和 `GLM_API_KEY`，然后打开 [workshop.ipynb](workshop.ipynb)，从头运行。这里会调用在线模型。

关键调用在 Notebook 中直接展开：

```python
thread = codex.thread_start(cwd=str(work), sandbox=Sandbox.full_access,
                            approval_mode=ApprovalMode.deny_all)
turn = thread.turn(task, sandbox=Sandbox.full_access)
result = turn.run()
```

## 改哪里，看什么

- 改 cell 5 的 `task`，例如要求报告列出哪些判断仍缺证据，再用新运行目录比较结果。
- 在 cell 7—8 按 `call_id` 找到命令与回执，检查日志随机编号首次进入哪次模型请求。
- 读 `outputs/incident-report.md`，对回日志和代码；执行结束不等于归因正确。

`Sandbox.full_access` 不是目录隔离，外层权限继续生效。Notebook 收尾会关闭连接，每次运行使用独立 `.runtime/`。

[模型、输入边界和验证命令](REFERENCE.md) · [SDK 与留痕实现](../notebook_support.py)
