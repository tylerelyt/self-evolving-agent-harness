# Experiments

每讲独立运行，输入随目录提供；不需要先跑完上一讲。

- **01—09 / Codex**：打开本讲 `workshop.ipynb`；[安装 Notebook 环境](CODEX.md#前置条件与运行)。
- **10—23 / Hermes**：仓库根目录 `make setup`，再按本讲命令运行。
- **完整 On-call 系统**：根目录 `make demo`；[逐步实现](../workshop/capstone/lab/README.md)。

## 逐讲练习

| 讲次 | 关键词 | 文章主题 | 代码与运行说明 |
| --- | --- | --- | --- |
| 01 | 执行 | Agent 执行引擎：一句话怎样变成连续行动? | [本讲练习](01-agent-loop/README.md) |
| 02 | 控制 | 一封迟到的信：怎样改变一个正在工作的 Agent？ | [本讲练习](02-instruction-control/README.md) |
| 03 | 上下文 | 上下文引擎：模型每轮对话都会看见什么？ | [本讲练习](03-context-engine/README.md) |
| 04 | 规则 | AGENTS.md：如何设计规则指导 Agent 未来行动 | [本讲练习](04-agents-md/README.md) |
| 05 | 压缩 | 上下文压缩：如何压缩历史才不影响任务延续？ | [本讲练习](05-context-compression/README.md) |
| 06 | 分支 | 会话分支：怎样借助旧经历，开始一场新的调查？ | [本讲练习](06-thread-fork/README.md) |
| 07 | 提炼 | 经验技能化：怎样把一次排查经历提炼成可复用的方法？ | [本讲练习](07-skill-extraction/README.md) |
| 08 | 披露 | 渐进披露：按需调阅关键上下文 | [本讲练习](08-progressive-disclosure/README.md) |
| 09 | 授权 | 令行禁止：用隔离与审批守住授权边界 | [本讲练习](09-isolation-approval/README.md) |
| 10 | 学习 | 原生学习：Hermes 与递归自我改进 | [本讲练习](10-native-learning/README.md) |
| 11 | 复盘 | 幕后复盘：Hermes 为什么回答以后还在忙？ | [本讲练习](11-background-review/README.md) |
| 12 | 修订 | 记忆维护：Hermes 怎样修正过时的记忆？ | [本讲练习](12-memory-revision/README.md) |
| 13 | 溯源 | 会话还原：Agent 怎样按需核对自己的旧记录 | [本讲练习](13-session-lookup/README.md) |
| 14 | 共享 | 海纳百川：Agent 怎样用上企业知识与共享经验？ | [本讲练习](14-external-memory-provider/README.md) |
| 15 | 创建 | 自主 skill 学习：从完成任务到领悟方法 | [本讲练习](15-skill-autocreation/README.md) |
| 16 | 迭代 | 增量 Skill 演化：Agent 服务怎样持续适应新需求？ | [本讲练习](16-skill-incremental-patch/README.md) |
| 17 | 整理 | Skill Curation：方法越积越多，谁来整理整个技能库? | [本讲练习](17-curator/README.md) |
| 18 | 评测 | 业务 Eval：怎样用对照评测为 Skill 自进化把关？ | [本讲练习](18-business-eval/README.md) |
| 19 | 数据集 | 企业数据集：怎样用 Langfuse 把真实经历变成可重跑的评测题？ | [本讲练习](19-enterprise-dataset/README.md) |
| 20 | 优化 | 离线优化：怎样利用一批任务，自动寻找更好的 Skill？ | [本讲练习](20-gepa-offline-optimization/README.md) |
| 21 | 蒸馏 | SkillClaw（上）：怎样从多个在线 Agent 的经历中蒸馏 Skill？ | [本讲练习](21-skillclaw-session-collection/README.md) |
| 22 | 分发 | SkillClaw（下）：怎样将演化后的 Skill 分发给其他 Agent？ | [本讲练习](22-skillclaw-shared-revision/README.md) |
| 23 | 集成 | 自进化 On-call Agent：把每一层接成一条线 | [本讲练习](23-oncall-agent/README.md) |

每讲 README 提供输入、可修改位置和检查方法；Notebook 中直接展示 SDK 调用。在线模型调用与零调用的自检、脚本模型演练分别标明。[平台与运行条件](../COURSE_MAP.md) · [01—09 接口与记录说明](CODEX.md)。
