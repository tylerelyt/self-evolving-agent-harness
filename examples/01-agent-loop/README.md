# 第 01 讲：从告警开始一次调查

对应当前第 01 讲的支付接口 HTTP 500 案例。把告警、日志和代码交给 Codex，只说明目标和边界，不指定文件读取顺序，让模型根据工具返回继续决定下一步。

打开 [workshop.ipynb](workshop.ipynb)，使用独立的 Python 3.12 内核运行。Notebook 安装 Codex Python SDK 和 LiteLLM，通过本地协议适配器连接 GLM-5.2；不需要登录 OpenAI 或 ChatGPT。第 7 步会调用付费模型，费用由 GLM API 账号承担。普通 API 与 Coding Plan 的端点不同，本例使用普通 API。

## 看什么

先看任务要求有没有提前泄露调查路径，再看工具实际返回了什么，以及后面的行动是否利用了这些信息。命令输出、退出码、文件操作状态和最终回答分别展示，不把一条命令当作已经取得了调查结果。

最后检查报告是否存在、输入是否改变，再阅读报告的引用和结论。Turn 结束不等于结论正确，报告存在也不等于已经完成调查。模型可能一次读取多个文件，不能把它的实际轨迹改写成正文中的示范顺序。

## 范围与状态

这是构造的教学材料，不连接生产系统。输入不变在本讲是任务要求和事后检查，并非目录级只读保证；第 09 讲再落实硬权限。

Notebook 的生成文件不包含执行输出。当前已实现本例，验证情况见 [逐讲实现与稿件核对](../../docs/IMPLEMENTATION_REVIEW.md)。本地准备、单元测试和模型实跑分开记录。

无需模型账号的检查：

```bash
python3 examples/01-agent-loop/build_notebook.py
python3 -m unittest discover -s tests -p 'test_agent_loop.py' -v
```

安装 Notebook 中的依赖以后，还可以仅启动本地服务、创建 Thread，不执行 Turn：

```bash
python3 scripts/check_agent_loop_startup.py
```

这条命令使用无效测试密钥和本地不可用上游地址，不会调用 GLM。它不能证明模型已经完成事故调查。

## 接口依据

本轮核对使用 `openai-codex==0.154.0`、`litellm[proxy]==1.101.0`。SDK 自带对应 Codex runtime，不再沿用旧实验中手动创建二进制别名的做法。

- [Codex Python SDK](https://github.com/openai/codex/tree/main/sdk/python)：`Codex`、`thread_start`、`Thread.run`、`TurnResult`。
- [GLM-5.2 调用说明](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2)：模型名与普通 API 端点。
- [LiteLLM](https://github.com/BerriAI/litellm)：本机协议适配器，模型密钥通过子进程环境传入。

日志只保存在本机临时目录。分享前检查并清除 Notebook 执行输出，不上传凭证、真实业务材料或完整本机日志。

## 鉴权失败时

第一次请求如果返回 401，适配器可能随后进入冷却期，使 SDK 最后只显示 429。Notebook 会检查本次适配器日志，优先提示身份验证失败，但不会打印完整日志或凭证。请检查 API Key 和普通 API 权限，不要反复重试，也不要把该结果当作 Agent 已经开始调查。
