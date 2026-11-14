# 集成：来源与验证规则

[六步命令](README.md) · [执行器](steps.py)

## 先建立可观察的起点

`init` 只复制正式基线、建立状态与 `controls.json`，不执行任务、不启动服务。此时应看到 `completed: []`、`next_step: 1`；目录里还没有执行经历、候选、回放或新客户端。已有目录不覆盖，重做整个练习时换一个新目录。

直接运行 `bash run.sh` 只显示帮助。可以在任意一步退出终端，再用相同 `--run` 继续。依赖只在需要它的步骤检查；准备错误发生在执行前，不记作教学失败。

## 第1步：让正式版留下真实执行证据

原生 OpenViking 在独立空间存入本讲手册。A 读取正式 Skill，然后在 Agent 工具调用中实际执行 OpenViking `find/read`，再查询现场。本次任务明确补充了配置与生效值的对比要求；这是教学输入，不是自主发现方法的证明。工具返回的配置值 120、生效值 60、等待数 24 是构造数据，差异仍不能直接写成根因。

查看 `steps/01-baseline.json`、`experience.json`、`online/instance-a/task/messages.json` 和 `context-receipt.json`：找到实际 Skill hash、报告 Item、手册 URI、原生检索回执和现场参数。Client Proxy 的原始归档保存在 `experiences/`。

本步完成后 OpenViking 服务关闭，候选生成还没有开始。后续第2—6步使用此次检索内容的冻结副本，记录为 `frozen`；不能把这些回放说成再次实时查询了 OpenViking。

## 第2步：生成候选，先不让它上场

SkillClaw 原生 Summarize、Aggregate、Execute 在隔离共享目录中生成候选；GEPA 使用本讲自己的训练和验证任务运行搜索、反思与选择。训练、验证、留出分别属于独立事故，地域标签不提示答案。留出题此时没有执行。

查看 `steps/02-candidates.json`、`candidates.json`、`candidate-records/` 和 `learning/gepa/evaluations.json`。应有两个来源的候选记录，状态仍是 `pending_evaluation`；GEPA 的实际 Session 仅属于训练和验证，`holdout_executed` 为 false，正式 hash 保持不变。原生 `validated` 在此只将 SkillClaw 候选排入隔离区；独立门槛稍后决定发布，不冒充原生 PRM 的判定。

候选记录保存的是生成时快照。之后的拒绝、批准和发布记录在相应 `steps/` 文件中，不会回写这份来源证据。

## 第3步：只运行新旧独立回放

两份候选分别与基线比较，共运行八个全新 Hermes Session。两道留出题分别检验配置对比和原有上游异常分支。它们没有进入 GEPA 搜索。

查看 `steps/03-replay.json` 与 `learning/replay/` 中的实际消息和回执。核对同一道题的新旧 `snapshot_sha256`、`context_sha256` 和范围一致，Session 不同，实际读取 hash 分别对应基线与候选。比较查询顺序与最终报告，不用一句“已完成”替代工具证据。

这一步保存原始轨迹和行为检查，不分配评分可用性、不作发布决定。输出应为 `scores_assigned: false`、`publication_decided: false`。正式版仍没有改变。

## 第4步：评分缺失时拒绝，原版继续执行

`controls.json` 初始的 `score_outage: true` 模拟候选上游题的评分不可用。系统只对第3步的同一批轨迹赋分，两份候选均应 `REJECT / unjudgeable_cases`；`score_error` 的 `passed` 是 null，不作为业务失败或通过率中的成功项。

拒绝后还会另开一个真实 Hermes Session，让保留版执行与第3步相同的配置漂移题。查看 `steps/04-reject.json` 和 `rejection/retained-version/task/`：这个任务应实际读取旧 hash，取得两项原有查询回执。正式版继续是执行证据，不只是文件没有变化。

## 第5步：只修复评分输入，再决定和发布

第一条命令只把 `controls.json` 中的 `score_outage` 改成 false；也可以让 Coding Agent 只改这个字段。不要改 Skill、题包、原始轨迹、预期结果或第4步文件。

第5步不再调用模型。它重新核对候选内容、父版本、题包、上下文及原生证据 hash，再使用第3步同一批轨迹恢复评分。如果候选修复了旧版失败、没有回归且全部评分有效，才按内容 hash 通过原生 SkillHub 发布一份批准内容。默认优先选择已经通过门槛的 GEPA 来源；另一来源通过不等于它也被发布。

查看 `steps/05-publish.json`：`raw_replay_sha256` 应与第4步相同，`model_tasks_executed` 应为 0。第4步原 `REJECT` 文件仍保留原字节。共享正式入口已经更新，但这里还不能声称新客户端实际采用。

## 第6步：核对旧 Session 与新客户端各用哪版

原 A 的 SessionDB 备份到另一个目录，由新进程从原报告 Item 后继续；它仍固定原 Skill。C 则通过原生 SkillHub 拉取当前正式版，在新 Session 中实际读取批准 hash、执行新增的配置查询，并经 Client Proxy 归档新经历。两者都使用第1步冻结下来的外部上下文，不重启 OpenViking。

查看 `steps/06-adopt.json`、`continued/instance-a/result.json`、`receiver/instance-c/task/result.json` 和 `next-experience.json`。A 应保持原 Session 与旧 hash，并增加新的报告 Item；C 应是新 Session、读取批准 hash，并实际执行四项现场查询。文件同步、会话加载和行为改变分别核对。本练习停在新经历归档处，不会自动启动下一轮演化。

## 暂停、重跑和复位

每一步是独立进程。`status` 读取进度；重复已经完成的命令先核对证据，再返回 `already_complete`，不重跑模型、搜索或发布。越关会拒绝，程序不会替你补跑缺失步骤。源码题包或证据被改变时也会在执行前拒绝；这不等于目标能力已经完成。

若当前步骤执行失败，原始产物保留，显式复位后再试：

```bash
bash run.sh reset --run "$LESSON23_RUN" --from-step 3
```

这会归档第3步及后续产物，保留第1—2步；若已经发布，会通过本练习自己的 SkillHub 组实际恢复基线，再退回第3步。评分故障重新开启，下一次回放将建立新的独立 Session。只想重新验证采用时，从第6步复位，会保留第5步批准版本。

复位旧证据保存在本练习的 `archive/`，不删除其他 run、外部资源或 OpenViking 依赖环境。reference 目录、错误标记或指向外部的软链接不接受为复位目标。不要通过修改评分预期绕过失败；若要使用新题包，另建练习目录。

## 完整参考与检查

一键完整流程保留给实现对照，必须明确指定 `--reference`，并使用另一个目录：

```bash
bash run.sh --reference --out /tmp/lesson23-reference
bash run.sh --offline
```

reference 会一次跑完整链，A、恢复后的 A、C 均实际查询 OpenViking；它不代替上面的六步学习入口。reference 与分步目录不混用。`--offline` 运行发布门槛、分步守卫及复位回归，不调用模型；其中真实 SkillHub 发布与恢复检查需要固定的 native 依赖。没有这些依赖的标准库检查会明确跳过对应项，不能视为 native 发布验收。

源码中，`steps.py` 只负责本练习的阶段边界；实际执行复用 `run23.py`、`task_worker.py`、`external_context.py` 与 `gepa_search.py`。如果让 Coding Agent 协助验证，要求它只推进你当前指定的一步，带你回看该步的 Session、Item 和参数；不要让它先跑完完整参考，再把结果当成你逐步验证过了。
