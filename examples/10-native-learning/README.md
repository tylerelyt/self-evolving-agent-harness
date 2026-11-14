# 第 10 讲｜原生学习

关键词：**学习**。做一次任务，检查留下的 Memory / Skill，再换新会话验证实际采用。

## Run

在当前终端配置 `GLM_API_KEY`，从仓库根目录执行。`chat --live` 会调用在线模型：

```bash
make setup
bash examples/10-native-learning/run.sh check  # 原生接口检查，不调用模型
RUN=$(bash examples/10-native-learning/run.sh prepare --id-only)
cat > "examples/10-native-learning/runtime/$RUN/workspace/service.log" <<'LOG'
# Constructed input, UTC
09:00 payments status=200
09:01 payments status=503
09:02 payments status=200
LOG
bash examples/10-native-learning/run.sh chat --run "$RUN" --live
```

`chat` 需要当前终端已有 `GLM_API_KEY`，会调用在线模型。在 CLI 中先给它这个任务：

> 读取 service.log，写一份简短巡检记录。区分观测事实和待验证的原因。

观察实际结果，再补充你希望以后沿用的报告约定。等后台结束后用 `/quit` 退出。

```bash
bash examples/10-native-learning/run.sh inspect --run "$RUN"
bash examples/10-native-learning/run.sh chat --run "$RUN" --live
```

第二次 `chat` 是新会话；让它处理相关任务，省略上次补充的约定。比较两个 Session、Memory / Skill 差异与本次实际动作；文件出现和新任务采用分别核对，没有学到也保留结果。

## 改哪里

改这次 `workspace/` 的输入或你的纠正，只改一项再做独立对照。准备、快照与原生 CLI 启动代码在 [lab.py](lab.py)；不会改个人 Hermes Home。

[配置、记录位置与完整观察步骤](REFERENCE.md)
