# 第 10 讲 原生学习

这一讲先把 Hermes 用起来。选一件你平时会交给助手的小事，让它独立完成，再检查它留下的 Memory、Skill 和会话记录。开一个新会话，观察上次留下的事实或方法有没有进入这次任务。

## 准备环境

在仓库根目录运行。安装使用课程固定的 Hermes 提交，本讲另建自己的 Home。

```bash
bash scripts/setup_deps.sh
bash examples/10-native-learning/run.sh check
```

`check` 禁止网络连接，只初始化原生 Agent、学习工具和 SessionDB。终端会打印 `check.json`，其中模型请求为零。它用于确认接口就绪。

`prepare` 会建立一个运行编号。下面先把它保存在当前终端的 `RUN`。这个编号下有独立的 `hermes/` 和 `workspace/`。

## 把第一件事交出去

创建本次运行，把与小任务相关的材料放进终端显示的 `workspace/`。可以用本地样例日志、你自己整理的资料，或一个临时文件夹。

```bash
RUN=$(bash examples/10-native-learning/run.sh prepare --id-only)
echo "examples/10-native-learning/runtime/$RUN/workspace"
```

在当前终端配置 `GLM_API_KEY`，再启动原生 Hermes CLI。

```bash
bash examples/10-native-learning/run.sh chat --run "$RUN" --live
```

默认模型为 `glm-5.2`。配置来自这次运行的 `hermes/config.yaml`，模型调用会产生费用。程序直接启动原生 CLI，Memory 和 Skill 的创建、使用及后台复盘仍由 Hermes 执行。

把任务完整交代给它，观察它自己如何推进。比如让它读几份样例日志，按你平时使用的格式写一份简短调查。提供已核实的环境说明，遇到事实错误再自然地纠正。先观察它实际做了什么。

任务结束后等已有后台完成，在 CLI 中使用 `/quit` 退出。再运行下面的观察命令。

```bash
bash examples/10-native-learning/run.sh inspect --run "$RUN"
```

`inspection.json` 保存当前 Memory 和 Skill 文本及指纹，`chats/<编号>/before.json` 与 `after.json` 保存每次进入、退出 CLI 时的文件快照。新增文件可能包含框架初始化材料，结合实际工具请求和回执判断哪些是本次任务产生的内容。

## 换一个新会话

沿用同一个 `RUN`，再次执行 `chat`。这次会启动新的原生会话，复用这次教学 Home 中的 Memory 和 Skill。

```bash
bash examples/10-native-learning/run.sh chat --run "$RUN" --live
```

让它做一件相关的小事，提问里省掉上一轮那句纠正。观察它加载了什么、第一次查询用了什么参数，以及是否主动取用了上次留下的方法。

原生会话保存在本次 `hermes/state.db`。CLI 会显示会话编号。比较两个会话编号和实际查询，区分同一会话记住上下文、文件已经保存，以及新会话真正使用三个结果。没有生成文件或没有采用，也作为这一次观察保留。

退出 CLI 后不会删除本次资料。重新执行 `prepare` 会建立新的独立实验。

## 完成练习

留下第一件任务的输入和结果，一份 Memory 或 Skill 的变化，以及新会话的一次实际行动。说明这次经验属于事实、操作方法，还是需要修改 Harness 或训练模型才能处理的问题。

本轮验证只覆盖原生接口与文件观察，未调用真实模型。自主保存和跨会话采用须按你这次运行的记录判断。
