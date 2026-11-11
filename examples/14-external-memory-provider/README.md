# 第 14 讲：外部记忆与跨实例共享

本讲先用文件后端实现 Hermes `MemoryProvider`，再换成 SQLite，观察自动召回、按需检索和跨实例共享。最后让共享后端真正不可用，检查当前任务能否继续，以及未发送的完整消息是否仍在本地。

默认运行不调用模型。加 `--with-model` 才使用真实 `AIAgent` 和 GLM。每次运行创建独立目录；结束或按 `Ctrl-C` 后会关闭 Manager、Provider 和 Agent，没有常驻服务需要额外停止。

## 本讲独立练习

让两个会话连接同一个外部记忆后端。A 写入一条资料记录，已经启动的 B 在下一轮召回它，再主动查询来源。随后让后端暂时不可用，检查本地待传队列，恢复后核对补传内容。文件后端和 SQLite 各完成一次。

交付本轮 `summary.json`、`fault-receipt.json` 和 `recovery-receipt.json`。沿记录编号指出哪条来自 A，哪条由 B 新产生，并说明前台正常返回时，后台写入仍可能失败。企业资料接入和分层读取在本目录的 OpenViking 进阶练习中单独完成。

## 如何运行

在干净克隆的仓库根目录准备固定 Hermes `aaf96885`：

```bash
bash scripts/setup_deps.sh
bash scripts/setup_deps.sh --check
cd examples/14-external-memory-provider
bash run.sh file
bash run.sh sqlite
```

两条命令运行相同实验，分别使用 JSON 文件和 SQLite 后端，不需要 OpenViking、Embedding 或历史回放文件。默认模式通过真实 `MemoryManager` 调用接口，但不把构造的助手文本算作模型回答。

真实实验只需在进程环境中提供 `GLM_API_KEY`，不要把密钥写进代码、Notebook 或共享命令记录：

```bash
bash run.sh file --with-model
bash run.sh sqlite --with-model
```

模型固定为 `glm-5.2`，默认官方按量接口，单次最大输出8000 tokens。可显式设置 `GLM_BASE_URL` 为本机费用代理的 base URL；不附加 `/chat/completions`。每种后端包含4轮对话，其中按需检索轮可能产生多次模型请求。

## 动手观察

1. **文件后端与原生接口。** 阅读 [run_provider.py](run_provider.py) 的 `FileBackedMemory`：`initialize` 准备本地队列，`prefetch` 返回相关记录，`sync_turn` 先本地保存、再传后端。外部数据不会改写内置 `MEMORY.md`。
2. **自动召回。** B 先启动，A 再写入教学文档地址。B 不携带 A 的对话历史提问；核对召回记录编号是否来自 A。真实模式还要求 B 回答实际保存的地址。
3. **按需检索。** 通过 `get_tool_schemas` 暴露 `memory_lookup`，由 `handle_tool_call` 查询后端。真实模式中要求 Agent 调用该工具；没有发生调用就不会判作通过。
4. **换用 SQLite。** [run_shared_sqlite.py](run_shared_sqlite.py) 复用同一接口与实验，换成事务写入和按编号去重。A、B 各有独立会话与本地目录，只共享后端；B 每次重新查询，不依赖初始化时的旧副本。
5. **后端故障与补传。** 实验临时移动后端目录，使读取和写入真正失败；当前任务改为简单计算。Manager 隔离 Provider 异常，本地 `outbox.json` 保留用户、助手和消息列表。恢复目录后重新创建 Provider，重复补传并逐项回读，检查正文完整且没有重复。

这验证的是共享存储与调用链，不是“共同进化”或业务处置成功。记录带有来源身份和会话编号，不能把不同实例或错误时序的片段拼成成功经验。

## 看回执、怎样停止

入口会打印本次 `runtime/provider/<编号>/`。`summary.json` 汇总召回、工具查询与队列恢复检查；真实模式的 `A/turn-*.json`、`B/turn-*.json` 保存模型实际回答和工具调用。`backend/` 是共享数据，`A/outbox.json`、`B/outbox.json` 是各实例的本地待传队列。对照 `fault-receipt.json` 与 `recovery-receipt.json` 可以逐字检查故障时保留的消息及补传结果。

前台回答成功不等于写入成功：后端错误单独记录在 Provider，程序在前台返回后等待提交队列结束，再核对文件。失败时保留运行目录，不能删除队列来消除报错。实验用 `finally` 恢复临时移动的后端目录，并关闭 Provider；不要用强制杀进程代替正常中断。

逐步查看关键调用可打开 [workshop.ipynb](workshop.ipynb)。Notebook 默认关闭付费调用，使用独立 Hermes 子进程；输出只留在已忽略的 `runtime/`，不要提交带真实输出或凭据的 Notebook。

## 可选进阶与验证范围

[原生 OpenViking](OPENVIKING.md) 及 [进阶 Notebook](openviking.ipynb) 保留为进一步接入：需要另外的服务环境、GLM 和 embedding-3，不是上述练习的必需依赖。

文件与 SQLite 已各完成一次真实 `glm-5.2` 四轮实验：B 的自动召回命中 A 的记录，模型实际调用 `memory_lookup` 并回答文档地址；后端不可用时，前台仍完成计算，本地队列完整保留，恢复后补传没有重复。后端保存的正文和消息列表已与实际模型回执逐项比对，SQLite 完整性检查通过。

本次也发现：内置 `memory` 已禁用，但工具仍可见，模型调用失败后曾误述“无法持久化”。这不等于外部 Provider 写入失败，应以后端回读为准，不能把模型对保存机制的解释也视为正确。文件后端的自动召回轮还主动调用了查询工具，因此本次没有单独证明答案只依赖自动召回。

上述结果验证共享调用链，不验证业务处置效果。原生 OpenViking 的服务、经验抽取与跨实例使用尚未实跑；已有离线检查不能替代这些验证。补充接口、故障及跨实例/时序检查可运行 `bash run.sh check`。
