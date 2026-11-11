# 第21讲：收集多个 Hermes 实例的经历，运行 SkillClaw 蒸馏

本练习对应正文中的两个支付值班实例。A/B 使用同一旧 Skill 和相同的四个只读工具，不能用权限差异伪造方法缺口。B 的用户任务额外提示：健康和上游正常时继续查连接池与连接占用；这是一项明确的教学输入条件，不是自主发现新方法的证明。实际动作由 Hermes 决定，不提供写好的回答或保证 A 停在两步。

业务现场是教学快照，不连接生产系统。工具根据服务、区域和环境参数返回记录，不按预先写好的调用顺序回放。

本轮任务是按 Skill 完成一次初查，不是穷举所有工具。seed 先要求取得健康与上游两项回执，再选择分支：异常时报告现象、结束本轮；正常但仍超时时，旧方法尚无下一步。B 的补充提示只填补后一种情况，不取消异常分支的结束条件。seed 没有预写连接池诊断、现场数值或第22讲的预期答案。

## 准备

本讲自带 `timeout-triage` 旧版和四个只读教学工具，可以独立采集 A/B 两段经历。先运行无模型检查，再配置自己的模型访问启动真实采集。在仓库根目录执行。

```bash
bash scripts/setup_deps.sh
bash scripts/setup_skillclaw.sh
bash examples/21-skillclaw-session-collection/run.sh --offline
export GLM_API_KEY='你的 API key'
export GLM_MODEL=glm-5.2
bash examples/21-skillclaw-session-collection/run.sh
```

采集完成先读 `collection.json` 和两个实例的 `task/messages.json`，区分连接池检查建议与真实工具回执。演化后再看 `evolve_history.jsonl` 和共享 `SKILL.md`。这两处分别回答会话是否汇入、workflow 实际改了什么。

可选 `GLM_BASE_URL` 使用兼容接口或本机计费代理。模型、演化和后续验证都使用这组显式配置，不读取个人凭据。不要把真实 key 写进提交。

默认 `direct` 与正文相同。原生流程若接受修订，会直接写入本次独立共享组；没有改进时也可能不修改。direct 的正式版本也能接续第22讲，不必为了接续而重跑。若要比较先验证后发布，则从一开始选择：

```bash
bash examples/21-skillclaw-session-collection/run.sh \
  --run-dir "$PWD/examples/21-skillclaw-session-collection/runtime/new-oncall" --publish-mode validated
```

目录必须不存在。运行目录包含本次共享存储、各实例的 Hermes Home 和记录，不改 `~/.hermes`、`~/.skillclaw`。代理只绑定本机，用完自动停止；不会启动长期后台服务。

## 顺着代码看原生机制

1. `run21.py` 启动两个独立 Client Proxy。Hermes 的模型请求经过代理，实际诊断工具仍由 Hermes 执行。
2. 代理关闭时使用原生归档逻辑，把会话上传到同一组 `shared/<group>/sessions/`。实例来源分别为 `instance-a` 和 `instance-b`。
3. `skillclaw_lab/runtime.py` 调用原生 `EvolveServer.run_once()`，执行 workflow 的 Summarize、Aggregate、Execute。不是另写一个总结器替代 SkillClaw。
4. `direct` 写入共享技能库；`validated` 先产生验证任务，留给第22讲处理。运行日志、`debug/` 和 `evolve_history.jsonl` 保留本轮实际决定。

为便于逐步观察，练习运行两段会话和一轮演化，不启动周期服务。`--collect-only` 可以先停在会话收集处。`skills` 的自动注入和 Hermes Background Review 在此关闭，避免把两套学习入口混在一起。

收集完成后，可用相同 `--run-dir` 加 `--evolve-only` 继续。此时沿用原来的发布模式，不重复执行在线任务。演化前重新核对两实例的同等工具权限、旧 Skill 内容、实际消息与完整回执、原生归档和待消费队列；不把存在摘要冲突的记录交给演化。一次尝试开始后不会在同目录再次演化，失败也不覆盖。原生服务会移走已处理的队列会话，练习另外在 `collected/` 保留原样副本，便于核对；不要把这个副本再次上传，否则会重复学习。

模型只发出工具调用、没有回复正文时，原生代理会把调用列表的JSON写入 `response_text`。核对器将它与同轮调用逐项比较，并在 `response_text_fallback_turns` 标出；这不是模型回答，不据此编造调查结论。最终回答和原始工具回执仍逐字核对。

## 当前上游边界与无费用检查

固定上游的全文错误正则会把 `timeout-triage` 名称当成 timeout，即使原始 `skill_view` 回执为 `success=true`。新采集在代理启动前启用公开的课程兼容层 `skillclaw_lab/compat.py`：JSON 对象中布尔 `success=true` 且不存在 `error` 键时优先判成功，`success=false` 判失败，其余情况仍调用原分类器。只在采集代理生命周期内适配派生错误摘要，结束或异常时恢复原分类器；不改 `.deps`、业务字段、工具/Skill 名称、模型回答或原始回执。

这不是 SkillClaw 原生配置。启用标志写入各实例 `clients/<alias>/compatibility.json`、`collection.json` 和 `collection-checks.json`，后者也由第22讲继承到报告。`--collect-only` 结束前同样检查归档与实际消息、回执一致，没有冲突才能返回成功。真正失败、真实超时和没有结构化状态的文本不会被忽略。

演化阶段还有一处格式边界：固定上游会把无法解析的演化结果与主动 `skip` 一样处理。课程兼容层在解析失败时抛出异常，让原生 workflow 保留待处理会话，CLI 返回失败；只有模型明确返回合法的 `skip`，才算主动决定不修改。此适配记录在 `evolution-compatibility.json`，不修改模型生成的候选或上游文件。

原生提示明确要求 JSON 的阶段同时启用模型接口的 JSON 模式，并提醒正确转义；会话摘要仍按上游要求返回纯文本。`llm-exchanges/` 保留实际请求选项、提示和原始回答。格式仍然错误时停止，不自动重试到成功，也不提取或补写候选。需要重跑时建立新目录，旧失败记录继续保留。

已有两条旧归档确实存在摘要冲突，仍保持原样，不能被重新解释成兼容层启用后的实跑。它们可证明原生采集和演化曾执行，但不能替新接线作真实验收；若作为新演化输入，检查仍会拒绝这些旧冲突。新采集不再因成功读取同名 Skill 而必然被阻断。

```bash
bash examples/21-skillclaw-session-collection/run.sh --offline
PYTHONDONTWRITEBYTECODE=1 .deps/hermes-agent/.venv/bin/python -B examples/skillclaw_lab/test_offline.py
```

`workshop.ipynb` 调用同一个 `run.sh`，默认仅执行离线检查。在线单元格默认关闭，按 `collect-only`、检查记录、`evolve-only` 分段授权。中断会回收独立子进程组；不提供 `skill_manage` 写工具，任务内使用启动时复制的 Skill。

## 对照课后题检查

打开运行目录中的 `collection.json` 和 `collected/*.json`。分别检查模型发出的工具调用及其回执：A 是否只做了前两项检查？B 是否真的取得了连接池与占用数据？如果实际没有发生，就记录没有发生，不能根据最终回答补造轨迹。

再检查 `evolve_history.jsonl`、`debug/` 和共享的 `skills/timeout-triage/SKILL.md`：后台是否把“尚未执行的建议”和“实际执行的检查”分开处理？新增分支是否保留触发条件？本轮可能创建、修改，也可能保持不变。

`result.json` 只汇总共享文件是否改变，不宣称诊断能力已经提高。文件变化、后续实例读取和业务行为变化，要到第22讲分别检查。

## 文件分工

- `seed/timeout-triage/SKILL.md`：两个实例共用的旧方法。
- `run21.py`：采集、共享组与单轮演化，关键原生调用集中在这里。
- `../skillclaw_lab/hermes_task.py`：独立 Hermes 进程、`skill_view` 与四个只读诊断工具。
- `../skillclaw_lab/runtime.py`：连接、目录和有限次运行，第22讲继续复用。
- `../skillclaw_lab/task_protocol.py`：统一的初查任务提示；不接收场景名称、快照或预期动作。实际传给 Hermes 的 system/user 提示保存在各任务的 `input.json`，`result.json` 标明协议版本。
- `../skillclaw_lab/observations.py`：消息轮次、完整快照回执和原生归档核对。
- `../skillclaw_lab/compat.py`：显式课程兼容层，适配结构化工具状态分类，以及演化结果解析失败的异常处理。
- `workshop.ipynb`：与 CLI 共用入口的可执行练习。

固定版本见 `scripts/skillclaw-revision.txt`。会话包含模型输入、回答和工具结果；本例只上传教学材料到本地目录。换成企业材料或远端存储前，先确定允许共享的范围。
