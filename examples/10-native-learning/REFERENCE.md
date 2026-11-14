# 原生学习：配置与记录

[运行入口与样例输入](README.md) · [实现](lab.py)

## 配置

`prepare` 在本讲的 `runtime/<运行编号>/` 建立独立 `hermes/` 与 `workspace/`，不使用个人 Home。`hermes/config.yaml` 固定这次运行的模型与工作目录；默认 `glm-5.2`，准备前可通过 `GLM_MODEL`、`GLM_BASE_URL` 设置模型及兼容端点。`chat --live` 从当前终端读取 `GLM_API_KEY`，直接启动原生 Hermes CLI，会产生模型调用。

`run.sh check` 禁止网络，只核对原生 Agent、学习工具和 SessionDB 初始化。它证明接口可用，不证明已经学到经验。

## 记录

| 文件 | 看什么 |
| --- | --- |
| `initial.json` | 本次 Home 的初始文件与 hash |
| `chats/<编号>/before.json`、`after.json` | 每次进入、退出 CLI 时的 Memory / Skill 快照 |
| `chats/<编号>/exit.json` | 这次 CLI 的真实退出状态 |
| `inspection.json` | 当前文件全文、hash、相对初始状态的变化 |
| `hermes/state.db` | 原生 Session 与工具调用 |

用同一 `RUN` 再启动 `chat` 会得到新的原生会话。比较两次会话编号、实际加载内容和新任务动作：同会话记住上下文、持久化文件、跨会话实际采用是三个不同结果。框架初始化也可能新增文件，不能只看文件出现就认定模型学会；结合原始工具请求和回执判断。

## 结束与重跑

等待已有后台结束，用 `/quit` 退出，再 `inspect`。退出不删除运行资料；重新 `prepare` 创建另一个独立实验。没有写入、没有修订或没有采用都保留原始结果，不自动重跑到成功。
