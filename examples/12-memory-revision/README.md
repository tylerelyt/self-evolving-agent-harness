# 第 12 讲 记忆维护

本讲只做一次支付日志迁移。Hermes 启动时仍记着统一入口 `payment-api-prod`，本次任务收到正式迁移说明后改查服务目录。观察后台是否定位旧条目、用完整内容替换，以及已有会话和新会话分别读到了什么。

## 准备与本地检查

从仓库根目录运行。固定版本的 Hermes 与其他讲共用，本讲的记忆、会话和订单另开目录。

```bash
bash scripts/setup_deps.sh
bash examples/12-memory-revision/run.sh check
```

`check` 直接调用原生 memory 工具，核对零命中、多条命中和超出容量时的拒绝，再用两个原生 Agent 核对旧快照与新加载内容。它不调用模型。记录在本目录的 `runtime/native-*/`，`summary.json` 会标出工具检查结果及模型请求为零。

检查里手动传入的 `replace` 用于解释工具约束。下面的正式演练由原生后台模型自行选择操作。

## 运行一次迁移

在 `~/.hermes/.env` 准备 `GLM_API_KEY`，再运行本讲入口。也可以用 `--credentials-env` 指定凭据文件。

```bash
bash examples/12-memory-revision/run.sh
```

默认使用 `glm-5.2`，完整演练会调用模型并产生费用。依次观察五个时点。

| 时点 | 你要核对什么 |
| --- | --- |
| 常驻服务启动 | 本讲自己的 Hermes Home、任务队列与支付现场已经就绪 |
| 迁移前巡检结束 | 后台是否留下与旧部署有关的记忆，保存全文 |
| 正式切换部署 | 旧入口返回 410，迁移会话仍保留启动时的 Memory 快照 |
| 告警与迁移说明处理结束 | 当前任务怎样查询，后台是否发出成功的 `memory replace`，无关条目是否保留 |
| 新会话处理另一条告警 | 新加载的记忆是什么，实际是否按区域、环境查目录并使用返回入口 |

业务角色和记忆复盘政策配置在共享服务实现中。政策要求以正式迁移说明为依据、保存目录查询规则并保留仍有效的信息。它扩展了框架默认偏向用户信息的复盘提示。具体条目、`old_text`、`content` 及是否修改均由后台决定。

如果后台没有生成可用记忆，或者没有正确修订，保留这一轮的原答和回执。一次目录查询成功、一次文件写入和一次新会话采用，分别核对。

## 先看这些记录

主路径保存在本目录的 `runtime/observations/<运行编号>/`。终端打印统一入口 `REPORT.md`。

先打开 `memory/*-before.md`、`*-after.md` 和对应 diff。再用 `reviews/` 的工具调用编号找成功或失败的回执，核对模型到底改了哪一条。最后打开 `sessions/` 的旧会话快照与新会话快照，对照 `business-queries.jsonl` 里新会话真正使用的区域、环境、入口和时间范围。

`acceptance.json` 给出机制检查及其依据。仍需阅读完整 Memory，判断是否把一次临时地址写成了长期规则，或替换整条时丢了仍然有效的内容。

## 继续操作与停止

默认演练结束会等待已有复盘再停止服务。希望继续在同一教学 Home 里操作时使用下面的入口。

```bash
bash examples/12-memory-revision/run.sh --keep-running
bash examples/12-memory-revision/run.sh status
bash examples/12-memory-revision/run.sh down --wait-reviews
```

等待超时只结束当前等待。已有任务或复盘未结束时，服务保留在线。根据终端任务编号继续检查，再执行 `down --wait-reviews`。

本讲入口复用 [常驻服务实现](../../workshop/oncall/oncall_server.py)，并用 `ONCALL_LESSON=12` 将运行资料限定到本目录。`run_memory_revision.py` 提供本地工具检查。其他底层交互命令见[服务命令说明](../../workshop/oncall/README.md#继续自己操作)，手动调用时也要设置 `ONCALL_LESSON=12`。

## 完成练习

交出一段成功或失败的修订依据。说明迁移材料在哪里进入会话，后台当时拿到了什么，实际工具回执与文件差异是否一致，以及新会话的第一条业务查询能证明什么。

本轮核验覆盖原生工具和快照加载。未重新调用真实模型服务，后台是否自主修订及新任务表现需按这次演练的记录判断。
