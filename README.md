# 自进化 Agent 实战 · 随讲实验

极客时间专栏[《自进化 Agent 实战》](https://time.geekbang.org/column/intro/101188801)的配套练习仓库。每个实验都是一个可从头运行的 Notebook，由 Codex App Server 负责组织任务与执行工具，模型统一使用智谱 **GLM-5.2**，通过本机运行的 LiteLLM 适配器接入，**不需要 OpenAI / ChatGPT 登录**。

## 实验目录

| 讲次 | 实验 | 你会观察到什么 |
| --- | --- | --- |
| 第 01 讲 | [从 Python 启动一次 500 告警调查](examples/01-agent-loop/) | 一个最小 Agent Loop：只给目标和边界，Agent 自己读告警、日志和代码定位结账 500 的根因，并留下可核对的工具事件与调查报告 |
| 第 02 讲 | [一封迟到的信：改变一个正在工作的 Agent](examples/02-instruction-control/) | 巡检途中收到告警，用 `TurnHandle.steer()` 让告警进入**同一个 Turn**、复用已有上下文；再用 `TurnHandle.interrupt()` 对比“转向”和“中断” |

后续讲次的实验会随课程更新补充到 `examples/`。

## 环境要求

- Python 3.12（建议使用独立虚拟环境）
- 智谱 API Key（GLM-5.2），运行时从环境变量 `BIGMODEL_API_KEY` 读取
- Notebook 运行环境 Jupyter

```bash
python -m venv .venv
source .venv/bin/activate          # Windows：.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install jupyterlab ipykernel
export BIGMODEL_API_KEY="你的智谱 API Key"
```

## 运行实验

每个实验目录下都有 `build_notebook.py`（生成干净的 `workshop.ipynb`）、`workshop.ipynb` 和说明 `README.md`。

```bash
cd examples/01-agent-loop          # 或 examples/02-instruction-control
python build_notebook.py
jupyter lab workshop.ipynb
```

在 Notebook 中从上到下依次执行即可。首次运行会在本机启动一个 LiteLLM 协议适配器（只监听 `127.0.0.1`，使用随机端口和随机本地令牌），把 Codex 的 Responses 协议请求转换为 GLM-5.2 的 Chat Completions 请求；最后一格会关闭适配器与 Codex。

## 说明

- 实验会**真实调用 GLM-5.2 付费接口**，运行前请确认账号额度；Agent 的具体动作可能因模型而略有差异，属于正常现象。
- API Key 只存在于当前内存和适配器进程环境中，不会写入 Notebook、配置文件或仓库。
- 所有教学素材（告警、日志、服务器信息等）都是虚构的，在系统临时目录中生成，不写入仓库，也不要放入真实业务材料。
- 分享 Notebook 前请清除执行输出，不要上传凭证、真实业务数据或完整本机日志。

## 许可证

[MIT](LICENSE)
