# 第16讲：把单告警 Skill 扩展到关联调查

已有方法能排查单条告警，遇到多个服务同时异常时却未必够用。本练习先完成一次多服务调查，再主动触发 Hermes 原生后台复盘，观察它是否修订同一个 `oncall-alert-triage`，最后用新会话分别回放多告警和单告警任务。

## 运行与停止

在仓库根目录执行。默认命令只查看构造现场，不调用模型：

```bash
bash scripts/setup_deps.sh
bash scripts/setup_deps.sh --check
bash examples/16-skill-incremental-patch/run.sh
```

真实调查与复盘只需当前进程提供 `GLM_API_KEY`：

```bash
read -r -s GLM_API_KEY
export GLM_API_KEY
bash examples/16-skill-incremental-patch/run.sh --live
```

默认使用 `glm-5.2` 和官方 `https://open.bigmodel.cn/api/paas/v4`，可用 `GLM_MODEL`、`GLM_BASE_URL` 覆盖。缺少密钥时明确报错，不读取个人凭据文件。调用会产生 API 费用，单次前台最多16次迭代，原生复盘上限由固定版本决定；超时不等于计费上限。

终端按 Ctrl-C 停止；脚本只回收自己启动的复盘进程，关闭 Agent 与 SessionDB，保留本次记录。没有常驻服务，不需要 `lab down`。每次使用新的 `runtime/native-时间-随机编号/`，不触碰个人 `~/.hermes`。

也可打开 [workshop.ipynb](workshop.ipynb)，使用仓库的 Python 3.12 Notebook 环境，从上到下运行；Hermes 仍使用 `.deps/hermes-agent/.venv/bin/python`。默认不运行模型，按说明开启 `RUN_MODEL`；中断后执行收尾格。Notebook 不附带历史模型输出。

## 系统设计题

本讲题意是：已有的单告警排查 Skill 面对多服务关联故障，怎样从新的 Rollout 找出原方法的缺口，增补同一项 Skill，同时保留原有能力？进一步考虑，多实例同时修订时怎样避免覆盖彼此的变化，怎样发现新版本在旧任务上的退步？

先区分三种问题。工具暂时故障不等于方法缺口；方法要求执行的步骤被漏掉，属于执行问题；原流程没有覆盖事件级关联，才需要修订。外层 Harness 可以汇总跨时段、跨实例的任务分布，单次 Background Review 只处理传入的 Session。

修订仍以步骤为主：先判断是单告警还是多告警。单条沿用原路径；多条核对时间、依赖、调用链和传播方向，再证实根因。同期告警不一定同源，拓扑上的共同依赖也只是线索。主文件写通用判断，引用文件保存工具细节，不能只在末尾追加一条教训。

并发与退步是需要继续思考的设计问题。原生先读后写不是版本锁；生产环境还要决定同一 Skill 的修订怎样排队、发现冲突后怎样重新审阅，以及用哪些任务和指标比较新旧版本。本练习提供完整包哈希冲突检查和两个任务回放，不实现多实例协调，也不替这道设计题自动评分。

## 亲手观察

1. 阅读课程给定的旧 Skill。它只包含单告警步骤，明确标作构造起点，不声称来自以前的模型学习。
2. 调查事件 `east`。两个服务共享故障组件，另一个服务同时告警但原因不同；用只读工具核对指标、日志、时间线、依赖、调用链与探测结果。
3. 主动触发原生后台复盘。检查 `skills_list`、主文件与引用文件的 `skill_view`，再看同名 `skill_manage(action="patch")` 及按需写引用的回执。比较文件前后内容，确认修改都能追溯且已回读。
4. 用空历史新 Session 回放 `west` 与 `single`。前者包含一个原因仍未知的告警，不能强行归入共同故障；后者检验单告警旧路径仍可用。观察是否在业务查询前读到了本次保存的完整方法，而不只看答案提到了 Skill 名称。

运行会打印本次 `REPORT.md`。重点看同目录的 `investigation.json`、`review.json`、`approval.json`、`adoption.json`、`future-west.json`、`future-single.json` 与 `result.json`；`cleanup.json` 和 `review-process.json` 记录资源关闭情况。原始回答保留，纯 JSON 或唯一明确的 JSON 代码块分别标记格式，不从含混文字猜答案。

没有修订时返回 `no-update`，保留原生说明，不强行补写方法，也不展示后续复用；这不等于练习失败，更不等于已经学会。采用后回放未通过则返回失败，保留实际记录，不自动重跑到成功。

## 核心调用

[native_workshop.py](native_workshop.py) 的 `foreground()` 用新 Session 和空历史调用 `AIAgent.run_conversation()`。调查结束后，`review_job()` 显式执行：

```python
target, prompt = spawn_background_review_thread(
    parent, job["snapshot"], review_memory=False,
    review_skills=True, focus=scenario.FOCUS,
)
thread = threading.Thread(target=target, name="bg-review", daemon=True)
thread.start()
thread.join()
```

这是主动触发的原生后台复盘，不是 Turn 收尾自动触发。练习关闭自动检查，避免重复复盘；不改触发计数，也不替换上游提示。观察器只记录返回结果，不改工具执行器或补写文件。`focus` 明确要求优先修订已有同名方法，因此也不把目标选择说成模型毫无提示地自主决定。

固定 Hermes 为 `aaf9688519cca58dd5f76a589a0911aff269b060`。该版本只有 `_SKILL_REVIEW_PROMPT`，没有 `_LESSON_LAYER_BLOCK` 和 `_DO_NOT_CAPTURE_BLOCK`；练习依据现有提示和原生 `focus` 参数，不补造变量。

默认在本次独立教学库自动采用完整候选，回执为 `teaching-auto`，不算人工审批。若希望先读内容再确认：

```bash
bash examples/16-skill-incremental-patch/run.sh --live --no-auto-approve
```

终端展示候选全文和完整哈希，必须输入同一哈希才采用；空输入或非交互输入不通过，不会回退自动采用。复制前后核对候选、实际暂存文件和库基线，保留来源信息，仅排除查看、使用等观测计数。它是本地采用检查，不是分布式锁。

## 离线验证与边界

```bash
PYTHONDONTWRITEBYTECODE=1 .deps/hermes-agent/.venv/bin/python -B \
  examples/16-skill-incremental-patch/selfcheck.py
python -B examples/16-skill-incremental-patch/run_validation.py \
  --mode offline --run-id my-offline-check
```

自检使用构造调用，覆盖缺失写入、未完成复盘、来源与基线变化、复制失败回滚、人工确认、读取顺序和本次进程清理。不依赖被 Git 忽略的历史文件。Notebook 验证需要先安装仓库的 `examples/requirements-notebooks.txt`，不由执行器自动安装。

业务数据是 [scenario.py](scenario.py) 中的构造现场，不连接生产监控。运行记录可能包含完整输入输出，均不提交 Git；分享前检查凭据和个人信息。旧历史回放不用于本次验收。

2026-10-31 最后一次实跑的前台调查通过：查齐指标、日志、拓扑、调用链与组件探测，正确区分共同故障和独立故障。后台曾完整读取主文件和引用，但后续写入被原生先读后写检查拒绝，重复读取又返回 `unchanged/content_returned=false`。它最终仅用 `edit` 整篇重写了主文件，`patch` 调用均未成功，引用写入仍被拒绝，修改后的主文件也未回读。后台达到16次迭代上限，`completed=false`。

这次记录暴露了固定版本中并行工具读取标记与重复读取去重的交互限制，不是少发一次读取请求。课程保留原生行为和拒绝结果，没有关闭检查、补写引用或重跑到成功。候选未被采用，原教学库未变，多告警与单告警的新会话回放均未执行。因此能确认前台查清了这次故障，不能确认原生增量修订完整生效，也不能宣称保留了旧任务能力。
