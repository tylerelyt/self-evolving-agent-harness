# 第 17 讲：整理诊断技能库

本讲练习连接池与连接泄漏诊断技能的生命周期管理：自动标记陈旧、归档退役技能，让原生 Curator 判断是否需要整理，再检查应急手册、支持文件和诊断能力是否保留。

先在仓库根目录按根 README 准备固定版本的 Hermes 依赖，再进入本目录：

```bash
cd examples/17-curator
bash run.sh --offline  # 原生状态转换、保护和恢复，不调用模型
bash run.sh            # 再执行 GLM 整理及两项诊断检查
```

也可使用固定依赖环境打开 [workshop.ipynb](workshop.ipynb)，逐格运行。没有设置 `GLM_API_KEY` 时会隐藏询问；默认连接智谱官方接口。`GLM_BASE_URL` 可显式指定兼容端点，例如本地预算代理，代理令牌仍经 `GLM_API_KEY` 传入，不写入文件。

每次运行新建 `runtime/curator-*`，终端会给出 `REPORT.md` 路径。查看状态转换、原生整理回执、两项诊断结果以及恢复记录。终端按 `Ctrl-C` 停止，Notebook 使用“中断内核”；本练习不启动常驻服务，重新练习时重启内核。会话与数据库在结束或异常时关闭。`runtime/` 和日志不纳入版本管理。

## 课后练习

Oncall 技能库里有重复或相近的排障流程，也有长期未使用的旧技能。在不逐条人工清理的前提下，设计自动状态管理、语义整理与内置技能修剪的协作过程；说明如何让模型判断分组、保留公共入口和特殊资源，并验证整理后的任务仍然能完成。应急手册如何保护？支持文件或脚本失联时怎样发现和恢复？

本例使用四项**构造教学技能**，不含生产事故或真实应急步骤：

| 技能 | 练习动作 | 应观察的结果 |
| --- | --- | --- |
| `connection-pool-triage` | 检查连接池指标与容量压力 | 保留可发现的诊断入口 |
| `connection-leak-investigation` | 检查连接释放路径 | 特殊检查不因合并而丢失 |
| `disaster-recovery` | 标记 pinned，再执行自动管理和整理 | 不被归档或修改 |
| `retired-db-debug` | 设置为长期未使用 | 原生归档后可以单独恢复 |

[materials/](materials/) 提供全部初始文件。先运行 `apply_automatic_transitions`，再运行 `run_curator_review(consolidate=True, synchronous=True)`。状态转换不需要模型；语义整理使用 GLM。配置同时开启 `prune_builtins`，但这四项都是课程创建的技能，不能拿本例声称已验证内置技能修剪。

整理可以发生合并，也可以保持原样。`no-op` 只有在原生运行完整结束且文件未变时才成立，报错或中断不能算作 no-op。不要以文件减少数量判断成功：继续让 Agent 分别诊断容量压力与疑似泄漏，检查是否真的读到所需支持文件，再演练“单项恢复”和“快照回滚”的区别。

## 实现与边界

[curation_workshop.py](curation_workshop.py) 直接调用原生 `skill_manage`、状态转换、Curator 和快照恢复；[curator_read_adapter.py](curator_read_adapter.py) 是课程适配层，传递真实读取记录，并额外要求归档合并前读过目标包的全部当前文件。工具白名单也是课程限制，不是 Hermes 原生隔离沙箱。这里没有自动采用生产技能目录。

验收看生命周期、应急文件、引用完整性、两种诊断任务和恢复结果。诊断内容、资料读取与纯 JSON 格式分列；唯一 JSON 代码块可以提取，但块外解释须独立核对，多个块或歧义不自动通过。诊断解释是否充分仍须人工阅读。
