# 逐关实现自己的 On-call Agent

工作台已经准备了两套正在接收请求的业务环境，Agent 起初显示离线。接下来由你借助 Coding Agent，把巡检、故障调查、跨会话 Memory 和 Skill 学习逐项接进来。四关都修改当前目录的 [self_improvement_agent.py](self_improvement_agent.py)。

先按[工作台启动说明](../README.md#启动本地工作台)打开网页，再从仓库根目录打开 Codex、Claude Code 或其他 Coding Agent。让它先读取本目录的 [AGENTS.md](AGENTS.md)。下面每个请求只完成一关，Coding Agent 会自动运行检查，根据失败信息修改，直到本关通过。然后你回工作台应用代码，亲自观察现场发生的变化。

## 第 1 关，让 Agent 开始巡检

网页上已经有业务请求，但还没有 Agent 检查这些服务。你需要补全 `patrol_config`，让运行时获得服务范围、每轮抽查数量和巡检间隔。当前环境里的订单、支付、库存和通知四个服务都可以被巡检。运行时按这些配置持续调度，Hermes 则通过巡检 Skill 读取指标、查询历史并保存检查点。

把这一段交给 Coding Agent。

> 先读取 workshop/capstone/lab/AGENTS.md 和 README.md，只完成第 1 关。实现 workshop/capstone/lab/self_improvement_agent.py 中的 patrol_config，让两个实例能够按配置抽查服务并持续巡检。运行 `.deps/hermes-agent/.venv/bin/python -B workshop/capstone/lab/check.py --stage 1 --workbench workshop/capstone/runtime/workbench`，按失败信息修正直到通过。结束时用三个短点告诉我修改、检查结果及证据、回工作台应该看哪里。

检查通过后回到工作台，点击「启动 Agent」。在「服务现场」观察 Agent 从离线变为运行，服务逐步出现巡检检查点。对照当前指标和最近一次检查的时间，再找这次检查对应的 Session 和报告 Item。保持页面运行一会儿，确认新的检查点还在产生。

本关只启动巡检。接着把告警送进运行中的任务。

## 第 2 关，接手告警并交给值班工程师

巡检已经在跑，现在给其中一个环境注入故障。监控应该发出告警，Agent 在当前回合读取 On-call Skill 并初判，从报告 Item fork 补查，把结论和恢复建议 page 给值班工程师。交接之后，原巡检应继续运行。

这一关补三个函数。`route_alert` 把原始告警交给当前回合的 `steer`。`branch_report` 从保存的报告 Item 创建子 Session。`notify_operator` 保存引用真实分支报告的 paging 记录。参数和返回值以起始代码的函数约定为准，消息、查询和 SessionDB 接口由上层运行时提供。

> 按 workshop/capstone/lab/AGENTS.md，只完成第 2 关。在同一个 workshop/capstone/lab/self_improvement_agent.py 里实现 route_alert、branch_report 和 notify_operator，接起原始告警、同回合初判、报告分支和工程师交接。运行 `.deps/hermes-agent/.venv/bin/python -B workshop/capstone/lab/check.py --stage 2 --workbench workshop/capstone/runtime/workbench`，根据真实会话与工具证据修正直到通过。结束时用三个短点报告修改、检查结果及证据、回工作台的操作与观察位置。

检查通过后点击「应用代码」，让工作台加载当前实现。先等健康巡检，再在「服务现场」给一个目标服务注入故障。在「事故与调查」里打开这起事故，沿告警、初判工具结果、分支报告和 paging 记录往下看。通知之后再观察巡检时间是否继续更新。

读过恢复建议后，在「服务现场」清除对应故障。等待两个互不重叠的健康窗口，再看这起事故的恢复记录。两个环境可以分别制造故障和恢复，先在一个环境中走通。

## 第 3 关，在新会话里用上历史事实

故障恢复以后，下一次任务需要知道前面发生过什么，也要重新确认眼前的状态。这一关实现 `recall_memory`，让一个新的 Hermes 会话检索有来源的事故事实，再查询当前 HTTP 指标。历史事实应保留服务、环境、时间和源会话，当前判断由本次工具结果支持。

> 按 workshop/capstone/lab/AGENTS.md，只完成第 3 关。在同一个 workshop/capstone/lab/self_improvement_agent.py 里实现 recall_memory，按照提供的接口，让新会话读取带来源的历史事实，并查询当前服务状态。运行 `.deps/hermes-agent/.venv/bin/python -B workshop/capstone/lab/check.py --stage 3 --workbench workshop/capstone/runtime/workbench`，检查包含故障恢复窗口，保持运行直到完成，失败时根据证据修正后重跑。结束时用三个短点报告修改、检查结果及证据、回工作台的观察位置。

检查通过后回工作台点击「应用代码」，处理并恢复一次故障。到「Memory」点击「复盘已恢复事故」，查看生成的历史事实与来源；随后点击「新会话召回」，对照新会话的检索结果和新取得的服务指标。确认历史记录来自哪起事故、哪个 Session，以及本次判断查了什么当前数据。本关检查会实际等待恢复窗口，通常比前两关耗时更长。

## 第 4 关，让评测决定新 Skill 能否发布

两端的事故经历可以进入 SkillClaw，生成共享 Skill 候选。你补全 `release_policy`，用新旧版本在同一批冻结题上的结果决定是否放行。两版都要有可核对的执行证据，候选通过全部用例，并至少修好一道现行版没做对的题。缺证据、出现回归、内容没有变化或没有增益时，都应保留现行版。

> 按 workshop/capstone/lab/AGENTS.md，只完成第 4 关。在同一个 workshop/capstone/lab/self_improvement_agent.py 里实现 release_policy，用成对评测的工具轨迹与业务结果判断候选能否放行。运行 `.deps/hermes-agent/.venv/bin/python -B workshop/capstone/lab/check.py --stage 4 --workbench workshop/capstone/runtime/workbench`，检查有效候选及缺证据、回归、无增益等反例，修正直到通过。结束时用三个短点报告修改、检查结果及证据、回工作台接下来怎样观察学习和采用。

检查通过后「应用代码」。在两个环境分别处理并恢复事故，随后到「Skill 学习」观察会话组批、候选生成、独立评测和版本决定。发布后，再用新的故障任务检查实际加载的 Skill 哈希和新增探测。只有两个实例的新任务都留下对应证据，才算验证了两端采用。

如果还没有形成学习批次，先看两端是否都有已恢复的事故。若候选被拒绝或保留，按逐题结果查原因。已经发布但还没有后续事故时，保留待采用验证的状态。

## 每关完成后怎么核对

Coding Agent 的检查会生成 `check.json`，记录本次实际读取的实现路径、内容指纹和证据位置。请求中的 `--workbench` 指向当前工作台，检查结果和 Session 证据会回到该工作台。检查通过后点击「应用代码」，再到对应分区观察运行结果。工作台里的「运行本关检查」可以用同一检查器再次核对当前代码。

可以单独重跑任一关，默认使用当前目录的学员实现。

```bash
.deps/hermes-agent/.venv/bin/python -B workshop/capstone/lab/check.py --stage 2 \
  --workbench workshop/capstone/runtime/workbench
```

如果启动时使用了其他 `--state` 目录，把上述请求和命令中的 `--workbench` 一并改成面板显示的工作台路径。独立运行检查时，也可以用 `--implementation` 指定实现文件、用 `--out` 指定新输出目录。检查和工作台默认使用本机脚本模型，无需模型密钥。配置真实模型后可显式传入 `--live`。工作台里的旧运行记录保留用于对照，当前服务现场以正在运行的实例为准。

完整系统的命令行参考入口、数据集、离线优化和发布恢复说明见[综合实战配套工程](../README.md#完整参考运行)。
