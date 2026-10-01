# Lab 07｜经验技能化：怎样把一次排查经历提炼成可复用的方法？

[打开 notebook](workshop.ipynb)，用 Python 3.12 内核从头运行。未设置 `GLM_API_KEY` 时，准备格会弹出隐藏输入框；全程只需输入这一个密钥。缺失的 `openai-codex==0.154.0`、`litellm[proxy]==1.101.0` 和 `httpx[socks]` 会自动补装。

这是专栏《自进化 Agent 实战》07 讲《经验技能化：怎样把一次排查经历提炼成可复用的方法？》的配套练习。练习使用构造的事故材料，目标是掌握处理真实场景的能力。

| 对应专栏章节 | 动手做什么 | 跑完看什么 |
| --- | --- | --- |
| 第一幕｜Skill Creator 怎样把历史经验整理成 Skill？ | 从练习中的调查 Thread 提炼 `legacy-payment-oncall`，初始化目录并校验结构 | “进入 Skill 的方法”与“留在原 Thread 的现场”两列 |
| 第二幕｜为什么结构合格的 Skill 仍然带着旧事故的答案？ | 审阅练习提供的案例初稿，删去旧根因与实例名 | 初稿与修改后文件，以及两次结构校验结果 |
| 第三幕｜新的调查还需要打开旧 Thread 吗？ | 用新 Thread 调查练习材料构造的第二场事故 | 当前入口的材料怎样改变判断，支持“依赖地址迁移后配置未同步” |

第一幕展示 Skill Creator 的实现过程：先运行 `init_skill.py` 建目录，以 `codex.thread_start(cwd=...)` 开会话，读取旧调查记录，再向 `session.turn()` 传入 `SkillInput(name="skill-creator", path=...)` 与 `TextInput(...)`，调用 `turn.run()` 等待结果。随后用子进程运行 `quick_validate.py`；模型实际提炼结果另存为 `model-draft-SKILL.md`。

第二幕把练习提供的 `materials/first-draft-SKILL.md` 写入工作目录，再读回并打印全文，供读者逐条核对；这份案例初稿不是本次模型输出。接着直接读入 `materials/reviewed-SKILL.md` 并写回 `SKILL.md`，删去旧根因与实例参数，保留四类知识与七步调查；修改前后分别运行结构校验。

第三幕以 `codex.thread_start(cwd=...)` 新建 Thread，通过 `fresh.turn([SkillInput(...), TextInput(...)])` 显式调用 `$legacy-payment-oncall`，执行 `run()` 后打印 `result.final_response` 中的模型报告。三幕表格依次呈现方法与现场的区分、文件修改前后对照，以及当前材料怎样改变判断；模型报告原文另存于运行记录。

初始目录为 `legacy-payment/.agents/skills/legacy-payment-oncall/`，只含 `SKILL.md` 与 `agents/openai.yaml`，没有传入 `--resources`。随附的 [Skill Creator](skill_creator/来源说明.md) 和脚本用于初始化与结构校验，查询入口使用本地案例材料。

每幕用三行引导说明动手做什么、跑完看什么、对应专栏哪一节，随后由代码格和表格呈现过程。开头是一道面试式系统设计题，只给工作背景、任务和目标；参考答案先说明怎样区分方法与当次结论，再展开方法提炼、组件落地、人工核对和异因事故试用。题面与参考答案已经给出完整的问题和回答，三幕练习结束后直接收尾。

模型连接由 `examples/notebook_support.py` 准备，使用 GLM-5.2。每幕两个代码格，直接执行初始化、SDK 调用、文件读写和校验；`lab.watch()` 只做超时保护，`lab.record_result()` 只记录结果。`examples/notebook07_story.py` 负责材料准备、路径、表格、留痕与收尾。每次运行新建 `.runtime/<运行编号>/`，保存材料、模型请求、Turn 回执、校验结果和报告。第三幕结束时自动关闭连接；中途代码格报错也会释放本次连接。从准备格完整重跑时会关闭旧连接并创建新目录。

运行保持已有 `Sandbox.full_access` 与 `ApprovalMode.deny_all` 设置，外层权限继续生效。案例目录只是本次工作的范围，不代表操作系统隔离。

自动化核验可以运行：

```bash
python examples/07-skill-extraction/run_validation.py --mode offline
python examples/07-skill-extraction/run_validation.py --mode live
```

离线核验设置 `LAB_VALIDATION_RUN_ID`，运行目录初始化、案例回放、文件修改与结构校验。实际模型核验使用 `GLM_API_KEY` 环境变量，将其送入隐藏输入框后执行整本，记录单元格耗时、模型请求数与 Turn 数。两种模式的执行文件、结果和 `reading.html` 保存在各自的 `validation/runs/` 中。

分享 notebook 时保留无执行输出的源文件；原始运行记录留在本机。
