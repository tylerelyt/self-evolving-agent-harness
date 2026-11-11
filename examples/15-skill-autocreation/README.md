# 第15讲：从调查经历提炼 Skill

## 系统设计题

本讲只练习一次技能创建。用构造的连接泄漏现场形成调查记录，再把包含工具回执的完整历史交给原生后台复盘。检查它为何选择新建、补充已有方法或保持原样，以及留下的步骤能否用于另一个服务。

### 参考思路

先从实际调查中寻找有效技术、调试路径、工具使用经验和用户纠正，再按 Procedure first 组织方法：步骤说明先做什么、依据什么继续或停止，教训放在对应步骤中。主文件保存通用判断，具体查询方法和工具字段放在参考文件。

复盘时先检查已有同类 Skill，避免重复；剔除一次性任务信息、临时环境错误和未经验证的失败操作。没有可靠的新流程就不必创建文件。文件存在不代表方法正确，还要读完整内容，并对照产生它的工具回执。

先运行默认命令，观察正常分支和异常分支的连接归还差异。模型路径完成后，交付 `investigation.json`、`review.json` 和本轮 Skill 包，逐条指出诊断步骤来自哪个工具结果。若没有完整创建，就说明停止在哪一步。

## 运行与停止

在仓库根目录执行：

```bash
bash scripts/setup_deps.sh
bash scripts/setup_deps.sh --check
# 只执行构造业务代码的正常/异常重放，不调用模型
bash examples/15-skill-autocreation/run.sh
# 模型调查、显式触发原生复盘、检查库变化及新 Session 复用
bash examples/15-skill-autocreation/run.sh --live
```

真实运行只需当前进程设置 `GLM_API_KEY`，缺少时在模型调用前明确报错。默认 `glm-5.2` 和 `https://open.bigmodel.cn/api/paas/v4`，可用 `GLM_MODEL`、`GLM_BASE_URL` 覆盖。不要把密钥放进源码、Notebook或命令参数。

每次运行创建独立 `runtime/native-*/`，不读取个人 Hermes Home。前台是有界任务，不启动常驻HTTP服务；后台复盘使用本次子进程。终端按 Ctrl-C 中断会关闭数据库并回收该复盘进程。Notebook中断或重新运行准备格也应先停止其执行器。

[workshop.ipynb](workshop.ipynb) 展示关键调用和对应源码，默认只检查现场；真实运行格需要明确设置 `RUN_MODEL = True`。Notebook采用仓库Python 3.12环境，实际Agent仍由Hermes独立解释器执行。

## 实际观察

1. **准备已完成的调查。** 模型查询连接池指标、日志和代码，并调用测试工具实际重放正常及异常分支。测试执行的就是所展示的处理函数；正常请求归还连接，异常分支漏归还。这是缩短后的构造调查，不冒充持续数小时的生产事故。
2. **触发后台复盘。** 将完整消息及工具回执快照交给原生 Background Review。看后台是否先找已有同类方法，再决定新建或不更新。脚本不提供新Skill正文，不补写漏掉的引用文件，也不指定新技能名称。
3. **阅读文件与库变化。** 对照 `skills_list`、`skill_view`、`skill_manage` 请求和同编号回执，查看完整 `SKILL.md` 与引用文件。检查是否Procedure first、判断条件是否明确、是否夹带单次服务名或未经验证的操作。没有更新也是应记录的观察结果，不要求每次创建文件。
4. **观察后续使用。** 只有形成完整候选才复制到本次教学库，再建立空历史Session调查另一个服务。核对它在业务查询前读到了实际保存的正文，并用新测试结果识别提前返回分支，而不是照搬异常抛出结论。这是对课后题的扩展观察。

课程只检查回执配对、完成状态、完整文件追溯和构造现场的查询结果。流程是否通用、学习信号是否充分，以及为什么创建或不创建，仍需学员阅读完整记录作判断。

## 核心调用

实际代码在 [native_workshop.py](native_workshop.py) 和 [scenario.py](scenario.py)。前台通过 `AIAgent.run_conversation(..., conversation_history=[])` 完成调查，消息保存在SessionDB；后台执行：

```python
target, prompt = spawn_background_review_thread(
    parent, job["snapshot"],
    review_memory=False, review_skills=True, focus=scenario.FOCUS,
)
thread = threading.Thread(target=target, name="bg-review", daemon=True)
thread.start()
thread.join()
```

这是练习主动触发的原生复盘。本入口关闭自动检查，避免同一次调查被复盘两次。实例的 `_SKILL_REVIEW_PROMPT` 读取本讲[复盘策略](config/native-skill-review-policy.md)，`focus` 补充连接泄漏调查的观察范围。后台派生、工具权限和写入仍由原生机制执行。研究无人提醒时何时自动学习时，可以继续观察 `turn_finalizer.py` 的计数与触发条件。

Hermes固定在 `aaf9688519cca58dd5f76a589a0911aff269b060`。本讲通过它支持的实例提示覆盖点配置学习信号、Procedure first、排除不可靠经验和无可靠流程时停止的策略。`review.json` 的 `prompt` 保存实际发送的完整要求，`policy_source` 与 `policy_sha256` 对应本讲策略文件。模型仍需根据本次经历判断是否写入。

## 记录与离线检查

运行结束打印本次 `REPORT.md` 路径。`investigation.json` 是实际调查，`review.json` 包含原生回复、写入请求及库前后快照，`future-beta.json` 记录新会话。`seed.json` 明确标出课程构造的已有方法，不把它当成本次模型生成结果。运行目录、模型输出和凭据不提交Git。

```bash
PYTHONDONTWRITEBYTECODE=1 .deps/hermes-agent/.venv/bin/python -B \
  examples/15-skill-autocreation/selfcheck.py
```

自检使用明确构造的调用检查追溯、完整包、来源、并发变化与清理，不读取被Git忽略的历史回放。真实模型调用会产生API费用。

2026-10-31 的一次实跑完成了前台调查和原生后台复盘，真实创建了 `connection-pool-leak-diagnosis`，写入引用文件并成功回读两个文件，磁盘内容与写入回执一致。但同一复盘批次还尝试删除受保护的 `database-pool-capacity-check`，Hermes 原生权限拒绝了该操作，旧 Skill 未变。课程因此拒绝采用整个批次，未执行新会话复用。这是含越界尝试的批次被拒绝，不是创建失败或缺少回读；保留这一结果，不补写文件或重跑到成功。生成方法是否通用、是否夹带单次细节仍需阅读正文，文件追溯通过不代表方法质量已通过。
