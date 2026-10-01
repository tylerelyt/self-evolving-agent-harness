# 第 12 讲：操作常驻 Hermes oncall 服务，观察记忆怎样修订

告警查对了，为什么下一次还可能按旧入口查？当前任务采纳迁移说明、后台改好长期记忆、新会话加载并采用新规则，是三件分别发生的事。本练习让你用真实服务与查询记录，把它们逐一核对。

主路径只需要一个入口。在工程根目录运行：

```bash
bash workshop/oncall/run.sh
# 原第12讲入口也转到这里：bash examples/12-memory-revision/run.sh
```

先用工程的 `scripts/setup_deps.sh` 准备 Hermes 依赖，`bash scripts/setup_deps.sh --check` 可只核对已有环境。源码固定在 `aaf9688519cca58dd5f76a589a0911aff269b060`（`hermes-agent 0.20.0`，以可编辑方式安装），运行解释器为 `.deps/hermes-agent/.venv/bin/python`。启动器从 `~/.hermes/.env` 读取 `GLM_API_KEY` 与代理配置，缺少密钥时会提示。默认使用智谱 `glm-5.2` 和 `https://open.bigmodel.cn/api/paas/v4`。

```bash
# 指定其他凭据来源、同供应商模型或已确认与凭据匹配的地址
./workshop/oncall/run.sh --credentials-env /path/to/credentials.env --model glm-5.2
# 跑完保留服务，用 CLI 继续操作；最后自行 lab down
./workshop/oncall/run.sh --keep-running
```

每次创建独立教学 Home 与支付现场，已有运行资料保留。不要在前一次服务仍在线时另开默认演练；先 `./lab down --wait-reviews`。`--new-run` 仅供明确需要保留原环境的调试，旧进程不会被它停止。模型和工具会消耗调用额度，前台每任务最多 8 次迭代，原生复盘 fork 的上限为 16；等待期限不是计费上限。

## 一键运行会看到什么

1. **服务在线。** 两个 Python 进程分别运行课程 HTTP 服务与教学支付服务，显示 PID、端口和独立 `HERMES_HOME`。健康检查不调用模型，不表示鉴权成功。
2. **真实巡检与第一次记忆。** 在 `/tmp` 建立支付配置、SQLite 订单与 JSONL 日志，实际发送 12 笔虚构付款。向常驻服务发送巡检消息；消息不提供日志入口。Agent 自行查询部署、找入口、读日志。前台结束后观察原生后台，打印完整 `MEMORY.md`、模型 memory 调用及回执、空记忆到旧规则的 diff。记忆可能保存入口名称，也可能保存“从部署信息取得入口”的方法，不要求固定措辞；没有留下记忆则停止。应读完整快照确认与本次部署有关。
3. **业务迁移。** 原子切换到区域日志与 Service Catalog。旧入口返回真实 HTTP 410，迁移前日志保留为归档；服务 PID 和 Memory 文件不变。`migration` 会话在切换前创建，启动时已经加载旧规则。
4. **告警调查与第二次记忆。** 产生真实下游延迟与超时日志，把告警与正式迁移说明全文合并为一条 HTTP 用户消息。说明包含负责人、变更编号、生效时间、影响范围、旧入口退役、目录查询方法与历史归档。前台完成后观察真实后台是否自主选择 `replace`，再打印完整 Memory 与前后 diff，并核对运行中会话仍保留旧快照。
5. **新会话验证。** 在 `eu-west/staging` 产生另一次告警，新建 Agent 与空历史，只发告警，不附迁移说明。核对其启动记忆，以及实际 HTTP 查询是否先查目录、再读目录返回的入口。末尾分别打印九项验收，默认等后台结束后停止本次服务，并给出交互命令。

第四幕接受两种真实轨迹：**旧入口失败 → 查目录 → 完成**，或 **直接查目录 → 完成**。告警自然保留迁移前监控采样的旧入口、来源与采集时间，但没有强制工具顺序。两种轨迹的教学结论相同：当前任务完成后，旧的长期规则仍需由后台修订。不能把尝试旧入口说成必然，也不能据此断言模型被旧记忆误导。

## 记录在哪里，怎样核查

运行资料全部排除出 Git。`runtime/active.json` 只指向当前运行，不包含凭据。实际结构如下：

```text
runtime/
  runs/run-时间-随机串/
    oncall.sqlite3                 持久任务和会话元数据
    hermes/                        独立 HERMES_HOME，配置、state.db 与记忆
    paylab -> /tmp/hermes-paylab-*  临时配置、订单、实际日志、目录与告警
  observations/run-时间-随机串/
    REPORT.md                      本次统一核查入口，九项实际标记
    acceptance.json                逐项核查结果与依据
    run.log                        五幕自然语言日志，完整输入与回复
    tasks/                         真实前台返回、工具参数与回执
    reviews/                       原 worker、复盘模型返回与同编号回执
    memory/                        每任务复盘前/后 MEMORY.md 与 diff
    sessions/                      初始、运行中和最终快照
    traffic/                       实际付款及其统计告警
    business-queries.jsonl         按时间记录真实部署/目录/日志 HTTP 查询
    migration-check.json           切换后退役、PID 与记忆不变的核查
    shutdown.json                  收尾是否完成
```

目录按每次运行分开，保留每次调查和复盘的完整过程。第一次和第二次完整记忆快照位于 `memory/<对应任务编号>-after.md`，修订前为 `-before.md`，差异为 `.diff`。日志中的“修订前 → 模型调用 → 修订后”可与原始复盘参数、同 `tool_call_id` 的回执互相核对。第五幕沿业务 HTTP 查询记录，查看新会话实际选择了哪些入口、取得了什么结果。

程序检查可确认调用成功、旧入口与区域 URI 消失、目录规则存在、快照与查询顺序；旧会话快照与第一次实际写盘内容逐字对应，新会话快照与第二次复盘写盘内容逐字对应。再阅读完整 Memory，核对后台怎样保留仍然有效的信息、替换已经过时的规则，并沿业务查询记录观察新会话怎样使用它。

## 最近验证（2026-10-15）

在 macOS 本机环境、Python 3.11.15、上述固定 Hermes 提交与 `glm-5.2` 上，修复后的入口完整运行一次：三次前台任务与原生复盘正常完成，后台成功替换过时记忆，八项核查通过；工具轨迹为允许的“直接查目录”分支。旧会话保持原快照，新会话使用第二次复盘后的完整记忆，实际先查 eu-west/staging 目录，再读取其当前日志入口。完整 Memory 同时保留查询范围、UTC时间与历史归档方法；演练最后正常停止服务。

验收按实际写盘全文、工具回执与查询行为核对，记录每一步的成功或失败。验收器的四项正反例可在工程根目录运行：`python workshop/oncall/test_acceptance.py`。

## 继续自己操作

下面命令在 `workshop/oncall/` 内执行。读者主路径仍是 `run.sh`；这些 CLI 用于后续探索与调试。

```bash
./lab up
./lab setup
./lab traffic --region us-east --env prod --profile normal --requests 12
./oncall session new --id my-case
./oncall send --session my-case '巡检 payment-api 在 us-east 生产环境的支付日志链路。'
./oncall task wait <任务编号> --timeout 300
./oncall trace <任务编号>
./oncall review wait --after-task <任务编号> --timeout 300
./oncall review show --after-task <任务编号> --with-tool-receipts
./oncall memory show
./oncall memory diff --after-task <任务编号>
./oncall session inspect my-case --memory-snapshot
./oncall report --session my-case
./lab down --wait-reviews
```

业务只读工具：`./payctl services describe payment-api`、`./payctl catalog lookup --service payment-api --region eu-west --env staging`、`./payctl logs read <返回入口> --service payment-api --region eu-west --env staging --since <UTC开始> --until <UTC结束>`。错误服务、区域、环境、版本和退役入口都真实报错。`./lab env use legacy|regional` 仅改变业务状态，不访问 Agent 历史、Memory 或复盘计数。`lab alert latest --region ... --env ...` 读取实际请求产生的告警。

## 接入与运行边界

课程服务每个 Session 持有一个真实 `AIAgent`，调用 `run_conversation()`，直接用返回的完整 `messages` 替换下一轮历史，不重复拼接。所有前台任务排入持久记录，串行执行；前台完成状态先保存，队列等已有复盘结束再接下一个任务。新 Session 新建 Agent，初始历史为空；相同教学 Home 中的 Memory 会在启动时加载。

前台队列线程通过 Hermes 原生工具白名单拒绝 memory 写入，后台线程独立执行原生 memory 操作。任务上下文随 Hermes 原生线程传播，让每次复盘保持与触发任务的对应关系。

前台职责为“巡检与告警前台只读，此限制不适用于独立后台复盘”。业务复盘政策是实例 `_MEMORY_REVIEW_PROMPT`，要求判断长期事实、区分持续规则与一次性结果、自行选操作、避免矛盾规则、存规则不存临时地址，无充分依据不改。它没有给后台指定目标条目、工具参数或必须成功的结果。

`config/hermes.yaml` 将 `nudge_interval` 从官方缺省 10 降到 1，让读者在短演练中观察复盘；关闭 Skill 复盘、压缩与退出保存设置。**触发间隔决定何时提供复盘机会，后台模型决定哪些经验值得保存、哪些旧规则需要替换。** 观察器在进程中安装一次，包裹并调用原 `_run_review_in_thread()`，记录原生后台的返回，并按工具编号区分前台已有操作与后台新增操作。

服务重启会把 queued/running 任务标为中断，重建 Agent 时从 SessionDB 恢复历史并重新加载 Memory。原生 daemon 复盘没有持久重启恢复：遗留 running 记录标为中断、结果未知。等待超时不会杀线程；停止时若还有前台或后台任务，服务保留在线并记录未停止。继续 `task wait` / `review wait` 观察即可，不要把缺少后台摘要认作“没有复盘”。若模型没有修订或修订不正确，保留本次记录，核查后用新的完整演练观察差异；不能拿人工写入的结果替代自主后台验收。

教学模拟的是支付业务和下游延迟。HTTP 请求、订单、日志、入口退役、目录查询、Hermes 模型调用、后台与 memory 工具都走真实路径。工具命令在本机终端执行，使用本机权限。
