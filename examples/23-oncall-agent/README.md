# 第 23 讲｜集成

关键词：**集成**。六条命令验证候选、独立评测、拒绝、发布与采用的交接；持续 On-call 业务系统在[综合实战](../../workshop/capstone/README.md)。

Hermes、OpenViking、SkillClaw、GEPA、SkillHub 真正运行；模型、反思和索引使用本机脚本。固定任务，不调用付费模型。

## 准备

```bash
make setup
cd examples/23-oncall-agent
LESSON23_RUN=/tmp/lesson23-my-practice
bash run.sh init --out "$LESSON23_RUN"
```

`init` 只准备基线，不执行后续步骤；已有目录不覆盖。

第 1 步前准备 OpenViking；第 2 步前再安装 GEPA：

```bash
../../.deps/hermes-agent/.venv/bin/python -B setup_openviking.py
# 到第 2 步再执行这两条：
../../.deps/hermes-agent/.venv/bin/python -m ensurepip
../../.deps/hermes-agent/.venv/bin/python -m pip install -r requirements.txt
```

## 一步一查

每次只执行当前这一步，读输出里的记录，再继续下一步。

| 步骤 | 命令 | 核对什么 |
| --- | --- | --- |
| 1 | `bash run.sh step 1 --run "$LESSON23_RUN"` | 正式版的 Session / Item、真实 OpenViking 检索和现场查询 |
| 2 | `bash run.sh step 2 --run "$LESSON23_RUN"` | SkillClaw 与 GEPA 两个隔离候选；正式 hash 不变，留出题未执行 |
| 3 | `bash run.sh step 3 --run "$LESSON23_RUN"` | 同题新旧回放、不同 Session；没有评分与发布决定 |
| 4 | `bash run.sh step 4 --run "$LESSON23_RUN"` | 评分缺失，`REJECT / unjudgeable_cases`；原版另开任务继续执行 |
| 5 | 修复下述评分输入后运行 `step 5` | 同一批轨迹重新评分与发布，不重跑模型 |
| 6 | `bash run.sh step 6 --run "$LESSON23_RUN"` | A 恢复原 Session 仍用旧版，C 新会话拉取并实际采用新版 |

## 只改一个变量

第 4 步故意遇到 `score_outage: true`。只修复评分输入，不改 Skill、题包、预期结果或原始轨迹：

```bash
bash run.sh scoring --run "$LESSON23_RUN" --available
bash run.sh step 5 --run "$LESSON23_RUN"
```

比较 `steps/04-reject.json` 与 `steps/05-publish.json`：回放 hash 相同，旧拒绝记录保留，新增模型任务为 0。第 2—6 步用第 1 步的冻结上下文，不冒充再次实时查询。

```bash
bash run.sh status --run "$LESSON23_RUN"
bash run.sh reset --run "$LESSON23_RUN" --from-step 3
```

复位会归档受影响步骤；已发布时先恢复本练习的正式基线。越关或证据被改会拒绝。[完整来源、复位规则与参考入口](REFERENCE.md)。

## 读代码

[steps.py](steps.py) 管步骤边界；[run23.py](run23.py) 连接实际组件；[task_worker.py](task_worker.py) 执行任务；[gepa_search.py](gepa_search.py) 负责优化。模型质量与生产泛化需另做在线评测。
