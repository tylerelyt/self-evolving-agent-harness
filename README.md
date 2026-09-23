# 自进化 Agent 实战 · 随讲实验

极客时间专栏 [《自进化 Agent 实战》](https://time.geekbang.org/column/intro/101188801) 的配套练习仓库。课程以 [Hermes](https://github.com/OpenHands/hermes-agent) 为主线 Agent 框架，配合 [SkillClaw](https://github.com/tylerelyt/SkillClaw) 实现跨实例经验共享，模型统一使用智谱 **GLM-5.2**。

> 课程入口：https://time.geekbang.org/column/intro/101188801

## 实验目录

### 第一部分：初识 Harness（第 01–02 讲，Codex SDK + Notebook）

| 讲次 | 实验 | 你会观察到什么 |
| --- | --- | --- |
| 第 01 讲 | [从 Python 启动一次 500 告警调查](examples/01-agent-loop/) | 最小 Agent Loop：只给目标和边界，Agent 自己读告警、日志和代码定位根因，留下可核对的工具事件与调查报告 |
| 第 02 讲 | [一封迟到的信：改变一个正在工作的 Agent](examples/02-instruction-control/) | 巡检途中收到告警，用 `steer()` 让告警进入同一个 Turn、复用上下文；再用 `interrupt()` 对比转向与中断 |

## 后续实验（随课程更新公布）

第 11 讲之后的练习正在随课程更新逐步公布，涵盖经验回流、可信可控、群体进化三部分和综合实战，目前仍在更新中，暂不展开。已上线讲次以[课程目录](https://time.geekbang.org/column/intro/101188801?tab=catalog)为准，对应讲次更新后会在这里补全实验说明。

## 环境要求

- Python 3.11+（建议使用独立虚拟环境）
- 智谱 API Key（GLM-5.2），第 11–23 讲从环境变量 `GLM_API_KEY` 读取（兼容 `BIGMODEL_API_KEY`）
- Hermes 源码（作为上游依赖放在 `.deps/hermes-agent/`，已 gitignore）
- SkillClaw 源码（作为上游依赖放在 `.deps/SkillClaw/`，已 gitignore）

```bash
# 第 01–02 讲（Codex SDK + Notebook）
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export BIGMODEL_API_KEY="你的智谱 API Key"

# 第 11–23 讲（Hermes + Python 脚本）
cd .deps/hermes-agent
source .venv/bin/activate
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export GLM_API_KEY="你的智谱 API Key"
export GLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
```

> 环境变量约定：第 11–23 讲统一以 `GLM_API_KEY` 为主变量（与 Hermes 自身的 `GLM_API_KEY`＋`GLM_BASE_URL` 一致），同时兼容旧的 `BIGMODEL_API_KEY`（脚本内 `os.environ.get("GLM_API_KEY") or os.environ.get("BIGMODEL_API_KEY")`）。第 01–02 讲使用 Codex SDK＋LiteLLM 适配器，该适配器读取 `BIGMODEL_API_KEY`。

## 运行实验

### 第 01–02 讲（Notebook）

```bash
cd examples/01-agent-loop
python build_notebook.py
jupyter lab workshop.ipynb
```

### 第 11–23 讲（Python 脚本）

每个练习目录下有 `run_*.py` 脚本和 `README.md` 说明。在 Hermes venv 中运行：

```bash
cd .deps/hermes-agent
source .venv/bin/activate
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export GLM_API_KEY="你的智谱 API Key"
export GLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
python ../../examples/11-background-review/run_background_review.py
```

脚本会在独立临时 `HERMES_HOME` 中运行，不污染你的真实配置。运行输出保存在各目录 `output/` 下。

## 说明

- 所有练习均**真实调用 GLM-5.2 付费接口**，运行前请确认账号额度；Agent 的具体动作可能因模型版本而略有差异，属于正常现象。
- API Key 只从环境变量读取，**不会写入任何脚本、配置文件或仓库**。
- 第 11–23 讲的练习使用独立临时 `HERMES_HOME`，运行结束后可安全删除。
- 所有教学素材（告警、日志、服务器信息等）都是虚构的，在系统临时目录中生成，不写入仓库。
- Hermes 与 SkillClaw 源码放在 `.deps/` 目录（已 gitignore），作为上游依赖引用，不随本仓库分发。

## 许可证

[MIT](LICENSE)
