# 自进化 Agent 实战

极客时间专栏[《自进化 Agent 实战》](https://time.geekbang.org/column/intro/101188801)的配套代码。每讲有自己的练习目录、运行说明和验收记录，按正文顺序进入即可。

[![综合实战 On-call 工作台：服务指标、故障告警与并行调查](assets/images/oncall-workbench.png)](workshop/capstone/README.md#启动本地工作台)

综合实战工作台实拍：两套本地 HTTP 环境、持续巡检、故障告警与并行调查。截图使用脚本模型演示；[启动工作台](workshop/capstone/README.md#启动本地工作台)。

## 逐讲练习

| 讲次 | 文章主题 | 代码与运行说明 |
| --- | --- | --- |
| 01 | Agent 执行引擎：一句话怎样变成连续行动? | [本讲练习](examples/01-agent-loop/README.md) |
| 02 | 一封迟到的信：怎样改变一个正在工作的 Agent？ | [本讲练习](examples/02-instruction-control/README.md) |
| 03 | 上下文引擎：模型每轮对话都会看见什么？ | [本讲练习](examples/03-context-engine/README.md) |
| 04 | AGENTS.md：如何设计规则指导 Agent 未来行动 | [本讲练习](examples/04-agents-md/README.md) |
| 05 | 上下文压缩：如何压缩历史才不影响任务延续？ | [本讲练习](examples/05-context-compression/README.md) |
| 06 | 会话分支：怎样借助旧经历，开始一场新的调查？ | [本讲练习](examples/06-thread-fork/README.md) |
| 07 | 经验技能化：怎样把一次排查经历提炼成可复用的方法？ | [本讲练习](examples/07-skill-extraction/README.md) |
| 08 | 渐进披露：按需调阅关键上下文 | [本讲练习](examples/08-progressive-disclosure/README.md) |
| 09 | 令行禁止：用隔离与审批守住授权边界 | [本讲练习](examples/09-isolation-approval/README.md) |
| 10 | 原生学习：Hermes 与递归自我改进 | [本讲练习](examples/10-native-learning/README.md) |
| 11 | 幕后复盘：Hermes 为什么回答以后还在忙？ | [本讲练习](examples/11-background-review/README.md) |
| 12 | 记忆维护：Hermes 怎样修正过时的记忆？ | [本讲练习](examples/12-memory-revision/README.md) |
| 13 | 会话还原：Agent 怎样按需核对自己的旧记录 | [本讲练习](examples/13-session-lookup/README.md) |
| 14 | 海纳百川：Agent 怎样用上企业知识与共享经验？ | [本讲练习](examples/14-external-memory-provider/README.md) |
| 15 | 自主 skill 学习：从完成任务到领悟方法 | [本讲练习](examples/15-skill-autocreation/README.md) |
| 16 | 增量 Skill 演化：Agent 服务怎样持续适应新需求？ | [本讲练习](examples/16-skill-incremental-patch/README.md) |
| 17 | Skill Curation：方法越积越多，谁来整理整个技能库? | [本讲练习](examples/17-curator/README.md) |
| 18 | 业务 Eval：怎样用对照评测为 Skill 自进化把关？ | [本讲练习](examples/18-business-eval/README.md) |
| 19 | 企业数据集：怎样用 Langfuse 把真实经历变成可重跑的评测题？ | [本讲练习](examples/19-enterprise-dataset/README.md) |
| 20 | 离线优化：怎样利用一批任务，自动寻找更好的 Skill？ | [本讲练习](examples/20-gepa-offline-optimization/README.md) |
| 21 | SkillClaw（上）：怎样从多个在线 Agent 的经历中蒸馏 Skill？ | [本讲练习](examples/21-skillclaw-session-collection/README.md) |
| 22 | SkillClaw（下）：怎样将演化后的 Skill 分发给其他 Agent？ | [本讲练习](examples/22-skillclaw-shared-revision/README.md) |
| 23 | 自进化 On-call Agent：把每一层接成一条线 | [本讲练习](examples/23-oncall-agent/README.md) |

## 独立综合实战

[让 On-call Agent 在生产环境中持续成长](workshop/capstone/README.md)是独立的综合实战，单独运行两套 HTTP 环境与两个持续巡检的 Hermes，串联告警、调查、批准恢复、Memory、独立评测、Skill 发布与下一轮采用。

- [运行完整两轮演练](workshop/capstone/README.md#运行双实例演练)
- [打开完整参考工作台](workshop/capstone/README.md#启动本地工作台)
- [逐步搭建综合系统](workshop/capstone/lab/README.md)

## 运行说明

使用 Git、Bash 和 Python 3.12，各讲所需依赖、模型配置与启动命令见本讲 README。真实模型调用会产生 API 费用；离线自检和脚本模型演示会在对应说明中标明。平台限制见[运行条件对照](COURSE_MAP.md)。

统一离线检查可在仓库根目录运行：

```bash
python3.12 -B scripts/check_offline.py
```

独立的[支付查询与评分器校准练习](workshop/practice/README.md)可作为补充实验。

## 许可证

[MIT](LICENSE)
