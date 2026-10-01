# Lab 07｜经验技能化：怎样把一次排查经历提炼成可复用的方法？

[打开 notebook](workshop.ipynb)，用 Python 3.12 内核从头运行。未设置 `GLM_API_KEY` 时，准备格会弹出隐藏输入框；全程只需输入这一个密钥。缺失的 `openai-codex==0.154.0`、`litellm[proxy]==1.101.0` 和 `httpx[socks]` 会自动补装。

这是专栏《自进化 Agent 实战》07 讲《经验技能化：怎样把一次排查经历提炼成可复用的方法？》的配套练习。练习使用构造的事故材料，目标是掌握处理真实场景的能力。

| 对应专栏章节 | 动手做什么 | 跑完看什么 |
| --- | --- | --- |
| 第一幕｜Skill Creator 怎样把历史经验整理成 Skill？ | 从练习中的调查 Thread 提炼 `legacy-payment-oncall`，初始化目录并校验结构 | “进入 Skill 的方法”与“留在原 Thread 的现场”两列 |
| 第二幕｜为什么结构合格的 Skill 仍然带着旧事故的答案？ | 审阅本轮实际生成的 Skill 与附件，核对旧根因与一次性参数 | 实际生成稿、审阅意见、文件差异和结构校验结果 |
| 第三幕｜新的调查还需要打开旧 Thread 吗？ | 用新 Thread 调查练习材料构造的第二场事故 | 当前入口的材料怎样改变判断，支持“依赖地址迁移后配置未同步” |

第一幕展示 Skill Creator 的实现过程：先运行 `init_skill.py --resources scripts,references,assets`，直接查看实际创建的目录；以 `codex.thread_start(cwd=...)` 开会话，读取旧调查记录，再向 `session.turn()` 传入 `SkillInput(name="skill-creator", path=...)` 与 `TextInput(...)`，调用 `turn.run()` 等待结果。资源目录只是骨架，是否需要附件、写什么内容由本轮提炼决定，不预填生成结果。随后运行 `quick_validate.py`，将实际生成的整个 Skill 保存到本次记录的 `generated-skill/`，包括附件。

第二幕只审阅这次生成的内容，不用预置稿覆盖它。原调查 Thread 只读检查模块边界、查询入口、指标口径、调查顺序和一次性结论，输出引用原句的审阅意见。读者核对文件和意见，必要时编辑本轮 Skill，再运行下一格：保留实际差异、结构校验和完整的 `reviewed-skill/` 副本。没有修改就如实显示无差异；模型审阅和结构合格都不能代替人工判断。`materials/first-draft-SKILL.md`、`reviewed-SKILL.md` 仅供另外阅读对照，主路径不复制它们。

第三幕先核对文件哈希，确认使用的仍是刚审阅的那份 Skill；再以 `codex.thread_start(cwd=...)` 新建 Thread，通过 `fresh.turn([SkillInput(...), TextInput(...)])` 显式调用 `$legacy-payment-oncall`。执行 `run()` 后展示本轮模型报告及实际查询回执。未查询的入口不会补成成功，也不会用预置根因替换模型报告。

初始目录为 `legacy-payment/.agents/skills/legacy-payment-oncall/`。`--resources` 的实际目录结果会直接打印；离线检查另对比不传该选项与传入三类资源的差别，并确认未完成的初始化骨架不能通过结构校验。随附的 [Skill Creator](skill_creator/来源说明.md) 和脚本用于初始化与结构校验，查询入口使用本地案例材料。

每幕用三行引导说明动手做什么、跑完看什么、对应专栏哪一节，随后由代码格和表格呈现过程。开头是一道面试式系统设计题，只给工作背景、任务和目标；参考答案先说明怎样区分方法与当次结论，再展开方法提炼、组件落地、人工核对和异因事故试用。题面与参考答案已经给出完整的问题和回答，三幕练习结束后直接收尾。

模型连接由 `examples/notebook_support.py` 准备，使用 GLM-5.2。每幕两个代码格，直接执行初始化、SDK 调用、文件读写和校验；`lab.watch()` 只做超时保护，`lab.record_result()` 只记录结果。`examples/notebook07_story.py` 负责材料准备、路径、表格、留痕与收尾。每次运行新建 `.runtime/<运行编号>/`，保存材料、模型请求、Turn 回执、校验结果和报告。第三幕结束时自动关闭连接；中途代码格报错也会释放本次连接。从准备格完整重跑时会关闭旧连接并创建新目录。

运行保持已有 `Sandbox.full_access` 与 `ApprovalMode.deny_all` 设置，外层权限继续生效。案例目录只是本次工作的范围，不代表操作系统隔离。

自动化核验可以运行：

```bash
python examples/07-skill-extraction/run_validation.py --mode offline
python examples/07-skill-extraction/run_validation.py --mode live
```

离线核验只运行源文件检查、真实初始化和快照隔离反例，不安装依赖、不读取密钥、不生成模型 Skill，也不执行 Notebook。需要已有 Python 与 PyYAML；结果打印到终端，临时文件在本讲 `.runtime/` 内创建并清理。

实际模型核验使用 `GLM_API_KEY`，执行四个真实 Turn：回顾调查、生成 Skill、只读审阅、新 Thread 试用。它检查原稿保留、审阅版本与试用版本一致、结构及查询回执，不要求生成文件等于某份预置答案，也不自动判断方法质量。交互学习时应在第二幕停下来审阅；`--mode live` 的批量执行只提供程序核查，仍需读者审读内容。只有 live 才保存完整执行 Notebook 与 `reading.html`；旧离线材料回放不能作为本版模型生成结果。

分享 notebook 时保留无执行输出的源文件；原始运行记录留在本机。
