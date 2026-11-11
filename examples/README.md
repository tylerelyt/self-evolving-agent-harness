# 第 01 至 09 讲代码练习

这九份引导练习依次检查 Agent Loop、运行时控制、上下文装配、项目规则、压缩、历史分支、经验提炼、按需加载与执行权限。业务输入均为明确构造的教学材料，模型调用、Codex 会话与工具执行使用真实接口。

每讲从自己的 `materials/` 创建独立运行目录，可以单独开始。第 06、07 讲需要的历史调查、第 08 讲使用的 Skill，以及第 09 讲的 Hotfix 代码都已随讲提供。练习之间通过业务场景衔接，运行时不依赖上一讲留下的 Session 或产物。

| 讲次 | 练习入口 | Notebook 直接展示的接口 | 核对的机制 |
| --- | --- | --- | --- |
| 01 | [Agent Loop](01-agent-loop/README.md) | `thread_start()`、`turn()`、`run()` | 模型自主读取材料，工具结果进入后续调用，最终形成调查报告 |
| 02 | [运行时控制](02-instruction-control/README.md) | `thread_start()`、`turn()`、`steer()`、`interrupt()` | 在运行中投递新要求，核对转向、中断终态与已落地文件 |
| 03 | [上下文引擎](03-context-engine/README.md) | `thread_start(developer_instructions=...)`、`turn()`、`run()` | 规则与工具进入请求，文件内容经成功回执进入后续调用 |
| 04 | [AGENTS.md](04-agents-md/README.md) | `thread_start(cwd=...)`、不同会话的实际输入 | 根到当前目录的规则装配，以及新任务的规则范围 |
| 05 | [上下文压缩](05-context-compression/README.md) | `compact()`、`read(include_turns=True)`、完成状态检查 | 原生压缩事件、输入缩短与任务判断状态保留 |
| 06 | [会话分支](06-thread-fork/README.md) | `CodexClient.thread_fork()`、`lastTurnId`、分支历史读取 | 指定 Turn 为包含边界，保留模块背景，排除后续旧结论 |
| 07 | [经验技能化](07-skill-extraction/README.md) | `SkillInput`、`TextInput`、`--resources`、独立 Thread | 审阅本轮实际生成的方法，再在新任务中试用同一版本 |
| 08 | [渐进披露](08-progressive-disclosure/README.md) | 普通任务文本、独立 Thread、`turn()/run()` 与请求记录 | 对比无关任务与匹配任务，观察自然选用及正文、附件的读取时机 |
| 09 | [隔离与审批](09-isolation-approval/README.md) | `turn_start(..., params)` 中的 `sandboxPolicy`、`approval_handler` | 学员确认本轮计划与权限配置以后执行，再观察越界拒绝与一次性审批 |

## 前置条件与运行

使用 Python 3.12。固定模型侧版本为 `openai-codex==0.154.0` 和 `litellm[proxy]==1.101.0`。`requirements-notebooks.txt` 同时固定 nbformat、nbclient 和 ipykernel。GLM-5.2 使用普通 API，需要相应调用权限，运行会产生 API 费用。

从项目根目录在选定的虚拟环境执行：

```bash
python -m pip install -r examples/requirements-notebooks.txt jupyterlab
read -r -s GLM_API_KEY
export GLM_API_KEY
python -m jupyterlab examples/
```

`read` 隐藏输入凭证，再将它导出到当前进程环境。不要把凭证填入 Notebook 或提交到仓库。九讲使用 `GLM_API_KEY`；没有设置时，准备格会弹出隐藏输入框。选择同一 Python 3.12 内核，从上到下运行。

正常结束和单元格异常都会关闭连接；重新运行准备格也会先清理旧连接。每次连接使用新的运行目录，无需保留上次执行输出。

第 03、08、09 讲需要宿主支持 Codex 原生 Sandbox。若宿主禁止嵌套 Sandbox，请先换到支持该运行方式的环境，再执行练习。第 09 讲会实际尝试允许范围内和范围外的写入，核对可写目录与拒绝记录。

## 文件与输出

- `workshop.ipynb`：不含执行输出的教学源文件，各阶段直接展示本讲关键 SDK 调用。运行后的单元格输出可随 Notebook 手动保存，分享前清除输出并检查敏感信息。
- `.runtime/<运行编号>/`：原始请求、命令与会话记录、教学工作目录和适配器日志，由 Git 忽略。此目录权限为当前用户私有，不作为公开附件。
- [build_notebooks.py](build_notebooks.py)：检查现有 Notebook 并清除执行输出，保留当前正文和代码；直接打开练习无需运行它。
- [notebook_support.py](notebook_support.py)：负责连接、请求记录、命令回执配对、超时处理与进程释放，运行 Notebook 时需要保留。

## 从调用记录理解运行过程

`glm-codex` 是本地别名，远程模型为 `glm-5.2`。本地适配器使用回环地址和随机令牌，将 Codex 的 Responses 请求转为 GLM Chat Completions。凭证保留在连接进程环境中，命令环境排除凭证字段，公开输出隐藏密钥、本机路径和真实会话编号。

请求记录显示 Codex 在协议转换前提供的内容。工具结果通过调用编号与前面的命令对应。第 05 讲由模型实际读取构造日志，Notebook 另外读取磁盘原文核对精确数值。

练习使用前面列出的 SDK 版本。理解接口与机制时，可以结合 [Codex App Server 官方文档](https://developers.openai.com/codex/app-server)、[AGENTS.md 规则说明](https://developers.openai.com/codex/guides/agents-md)和 [Skill 官方说明](https://developers.openai.com/codex/skills)，再对照 Notebook 中的实际参数、调用和返回结果。
