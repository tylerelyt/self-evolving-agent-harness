"""生成第 02 讲 Notebook；生成过程不启动服务，也不调用模型。

场景与正文一致：On-call Agent 巡检到 server-17（已读到凌晨调整数据库连接池的变更）
时，一条只含服务器、时间和异常现象的告警通过 TurnHandle.steer() 进入同一个 Turn；
随后用无副作用慢任务演示 TurnHandle.interrupt()。凭证统一使用智谱 GLM-5.2。
"""

import json
from pathlib import Path


def markdown(source):
    return {"cell_type": "markdown", "metadata": {}, "source": source.splitlines(keepends=True)}


def code(source):
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


cells = [
    markdown(
        """# Lab 02｜一封迟到的信：怎样改变一个正在工作的 Agent

一个 On-call Agent 已经在后台巡检服务器：它逐台读取部署信息、当日变更和监控指标。巡检到 **server-17** 时，它已经知道这台机器运行 `payment-api`，凌晨调整过数据库连接池。就在这时，一条支付告警到达。

我们**不重启** Agent，而是把告警交给这个正在工作的 Turn：

- 用 `TurnHandle.steer()` 让告警进入**同一个 Turn**，Agent 带着巡检阶段已经取得的上下文，从巡检转向故障调查，处理完再回到巡检；
- 再用一个无副作用的慢任务演示 `TurnHandle.interrupt()`，对比“改变接下来怎么继续”和“结束当前执行”。

这次仍由 GLM-5.2 提供模型能力，不需要登录 ChatGPT 或 OpenAI 账号。中途会真实调用模型，巡检、故障调查与中断演示全程约数分钟。请在独立的 Python 3.12 环境中运行，不要放入真实业务材料。
"""
    ),
    markdown("## 1. 安装 Codex Python SDK 与协议适配器"),
    code(
        '''import importlib.util
import subprocess
import sys
from importlib.metadata import version


def ensure(spec, import_name):
    if importlib.util.find_spec(import_name) is None:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
             "--index-url", "https://pypi.org/simple", spec],
            check=False,
        )
    if importlib.util.find_spec(import_name) is None:
        raise RuntimeError(f"缺少依赖 {spec}，请先在本 Notebook 内核环境安装（见仓库根 requirements.txt）。")


ensure("openai-codex==0.154.0", "openai_codex")
ensure('litellm[proxy]==1.101.0', "litellm")

print("openai-codex：", version("openai-codex"))
print("litellm：", version("litellm"))
'''
    ),
    markdown(
        """## 2. 准备巡检环境

构造一个最小但完整的夜间巡检现场（都在系统临时目录中，不写入仓库）：

- `servers/server-17、server-31、server-44`：每台机器一份 `deployment.md`（部署与当日变更）和 `metrics.md`（监控）。
- **server-17** 运行 `payment-api`，凌晨 01:40 下调过数据库连接池——这是**巡检阶段**才会读到的变更信息。
- `tools/patrol_probe.py`：只读性能采集脚本，运行约 25 秒，制造“告警在巡检途中到达”的时间窗口。
- `tools/interrupt_probe.py`：无副作用慢任务，约 40 秒，供后面演示中断。
"""
    ),
    code(
        '''import hashlib
import tempfile
from pathlib import Path

RUNTIME = Path(tempfile.mkdtemp(prefix="codex-lab02-"))
WORK = RUNTIME / "patrol-lab"
(WORK / "servers").mkdir(parents=True)
(WORK / "tools").mkdir()

FILES = {
    "servers/server-17/deployment.md": """# server-17 部署信息

- 业务：payment-api（支付服务），版本 v3.8.2
- 实例：10.0.1.17
- 当日变更：01:40 执行变更单 CHG-2317，将数据库连接池 max_connections 由 200 下调到 60
- 负责人：支付组
""",
    "servers/server-17/metrics.md": """# server-17 监控（近 30 分钟）

- payment-api P95 延迟：01:50 起由 180ms 升至 3.6s
- 5xx 错误率：升至 8.4%
- 数据库连接获取：大量请求等待连接，多次等待超时
""",
    "servers/server-31/deployment.md": """# server-31 部署信息

- 业务：order-api（订单服务），版本 v5.2.0
- 实例：10.0.3.31
- 当日变更：无
""",
    "servers/server-31/metrics.md": """# server-31 监控（近 30 分钟）

- order-api P95 延迟：稳定在 120ms
- 5xx 错误率：0.01%
- 无异常
""",
    "servers/server-44/deployment.md": """# server-44 部署信息

- 业务：inventory-api（库存服务），版本 v2.9.1
- 实例：10.0.5.44
- 当日变更：无
""",
    "servers/server-44/metrics.md": """# server-44 监控（近 30 分钟）

- inventory-api P95 延迟：稳定在 95ms
- 5xx 错误率：0.00%
- 无异常
""",
    "tools/patrol_probe.py": """import pathlib
import time

HERE = pathlib.Path(__file__).resolve().parent
(HERE / ".patrol-probe-started").write_text("started", encoding="utf-8")
for _ in range(25):
    time.sleep(1)
print("patrol probe finished")
""",
    "tools/interrupt_probe.py": """import pathlib
import time

HERE = pathlib.Path(__file__).resolve().parent
(HERE / ".interrupt-probe-started").write_text("started", encoding="utf-8")
for _ in range(40):
    time.sleep(1)
print("interrupt probe finished")
""",
}

for rel, content in FILES.items():
    path = WORK / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def snapshot_inputs():
    """对输入素材（servers 与工具脚本）计算 sha256，用于事后核对未被修改。"""
    return {rel: hashlib.sha256((WORK / rel).read_bytes()).hexdigest() for rel in FILES}


BASELINE = snapshot_inputs()

PATROL_TASK = """
你在执行夜间例行巡检。servers/ 目录下有三台服务器：server-17、server-31、server-44，
每台都有 deployment.md（部署与当日变更）和 metrics.md（监控）。

请严格按 server-17 → server-31 → server-44 的顺序逐台巡检：
1. 先读取该服务器的 deployment.md 和 metrics.md；
2. 巡检 server-17 时，读完材料后必须运行 tools/patrol_probe.py 采集约 25 秒性能基线，不要跳过；
3. 把这台服务器的巡检结论追加写入 patrol-report.md（一台一段）。

完成全部三台后结束。这是只读巡检：不要修改 servers/ 与 tools/ 下的任何文件，不要重启或变更服务。
""".strip()

# 关键：告警只包含服务器、时间和异常现象，不含凌晨的连接池变更。
ALERT = """
【实时告警 02:07】server-17 的 payment-api P95 延迟超过 3 秒，5xx 错误率升至 8.4%。
请立即在当前任务中优先处理这条告警：结合你已经掌握的 server-17 部署与变更情况判断最可能的原因，
把故障调查过程和结论写入 incident-report.md，每条结论注明依据来自哪个文件。
故障处理完后，请继续把 server-31、server-44 的巡检补完，结论仍写入 patrol-report.md。
""".strip()

print("工作目录：", WORK)
print("告警文本本身不含连接池变更信息：", "连接池" not in ALERT)
for rel in FILES:
    print(" -", rel)
'''
    ),
    markdown(
        """## 3. 输入 GLM API Key

从环境变量 `BIGMODEL_API_KEY` 读取；没有则安全输入。Key 只用于启动本地适配器，不会写入配置文件。
"""
    ),
    code(
        '''import getpass
import os

_glm_key = os.environ.get("BIGMODEL_API_KEY") or getpass.getpass("请输入 GLM API Key：")
if not _glm_key.strip():
    raise ValueError("API Key 不能为空。")

print("API Key 已读取；尚未调用模型。")
'''
    ),
    markdown(
        """## 4. 启动 GLM-5.2 协议适配器

在本机启动 LiteLLM 适配器，把 Codex 的 Responses 协议请求转给 GLM-5.2 的 Chat Completions 接口，并挂载预处理钩子移除不兼容的对象形式 reasoning 参数。
"""
    ),
    code(
        '''import atexit
import json
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

if "proxy" in globals() and proxy.poll() is None:
    raise RuntimeError("适配器已经在运行，请先调用 stop_proxy()，再重新执行本单元格。")

LOCAL_TOKEN = "sk-lab02-" + secrets.token_hex(24)

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    PROXY_PORT = sock.getsockname()[1]

handler_path = RUNTIME / "custom_handler.py"
handler_path.write_text(
    """from litellm.integrations.custom_logger import CustomLogger


class _GlmReasoningFix(CustomLogger):
    # Codex 按 Responses 协议发送对象形式的 reasoning 参数；GLM 的 Chat
    # Completions 只接受字符串形式的 reasoning_effort，转发前移除不兼容字段。

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            effort = data.get("reasoning_effort")
            if effort is not None and not isinstance(effort, str):
                data.pop("reasoning_effort", None)
            data.pop("reasoning", None)
        except Exception:
            pass
        return data


proxy_handler = _GlmReasoningFix()
""",
    encoding="utf-8",
)

proxy_config = RUNTIME / "litellm.yaml"
proxy_config.write_text(
    """model_list:
  - model_name: glm-codex
    litellm_params:
      model: openai/chat_completions/glm-5.2
      api_base: https://open.bigmodel.cn/api/paas/v4
      api_key: os.environ/BIGMODEL_API_KEY
general_settings:
  master_key: os.environ/LAB02_PROXY_TOKEN
litellm_settings:
  drop_params: true
  set_verbose: false
  callbacks: custom_handler.proxy_handler
""",
    encoding="utf-8",
)

proxy_log = (RUNTIME / "litellm.log").open("w", encoding="utf-8")
litellm = Path(sys.executable).with_name("litellm")
if os.name == "nt":
    litellm = litellm.with_suffix(".exe")
proxy_env = os.environ.copy()
proxy_env.update({"BIGMODEL_API_KEY": _glm_key, "LAB02_PROXY_TOKEN": LOCAL_TOKEN})
_existing_pythonpath = proxy_env.get("PYTHONPATH", "")
proxy_env["PYTHONPATH"] = str(RUNTIME) + (
    os.pathsep + _existing_pythonpath if _existing_pythonpath else ""
)
proxy = subprocess.Popen(
    [
        str(litellm),
        "--config",
        str(proxy_config),
        "--host",
        "127.0.0.1",
        "--port",
        str(PROXY_PORT),
    ],
    env=proxy_env,
    cwd=RUNTIME,
    stdout=proxy_log,
    stderr=subprocess.STDOUT,
)
del _glm_key, proxy_env


def stop_proxy():
    if proxy.poll() is None:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=5)
    proxy_log.close()


atexit.register(stop_proxy)

for _ in range(90):
    if proxy.poll() is not None:
        stop_proxy()
        raise RuntimeError("协议适配器启动失败。日志只保存在本机临时目录，请勿直接对外分享。")
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PROXY_PORT}/health/liveliness",
            timeout=1,
        ) as response:
            response.read()
        break
    except Exception:
        time.sleep(1)
else:
    stop_proxy()
    raise RuntimeError("GLM 协议适配器没有按时启动。")

print("GLM-5.2 协议适配器已启动。")
'''
    ),
    markdown(
        """## 5. 连接 Codex App Server

把 Codex 的模型 Provider 指向本地适配器，明确不需要 OpenAI 账号，并关闭多智能体功能。先用一个只读的健康检查确认链路通畅，再开始几分钟的巡检。
"""
    ),
    code(
        '''from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

CODEX_HOME = RUNTIME / "codex-home"
ISOLATED_HOME = RUNTIME / "home"
CODEX_HOME.mkdir()
ISOLATED_HOME.mkdir()

config = CodexConfig(
    config_overrides=(
        'model="glm-codex"',
        'model_provider="litellm"',
        'model_providers.litellm.name="GLM through local adapter"',
        f'model_providers.litellm.base_url="http://127.0.0.1:{PROXY_PORT}/v1"',
        'model_providers.litellm.env_key="LITELLM_API_KEY"',
        'model_providers.litellm.wire_api="responses"',
        "model_providers.litellm.requires_openai_auth=false",
        "features.multi_agent=false",
        "features.multi_agent_v2=false",
    ),
    env={
        "CODEX_HOME": str(CODEX_HOME),
        "HOME": str(ISOLATED_HOME),
        "LITELLM_API_KEY": LOCAL_TOKEN,
        "BIGMODEL_API_KEY": "",
        "OPENAI_API_KEY": "",
    },
)
codex = Codex(config)
atexit.register(codex.close)

health_thread = codex.thread_start(
    cwd=str(WORK),
    sandbox=Sandbox.read_only,
    approval_mode=ApprovalMode.deny_all,
)
health = health_thread.run("只回复四个字：连接正常。不要调用任何工具。")
print("Codex App Server 已连接。")
print("健康检查 Turn 状态：", health.status.value)
print("最终回答：", health.final_response)
assert health.status.value == "completed"
'''
    ),
    markdown(
        """## 6. 观察一：巡检途中收到告警，用 `steer()` 进入同一个 Turn

在后台线程启动巡检 Turn（它会读到 server-17 的连接池变更，并启动约 25 秒的只读采集探针）。主线程等到探针启动——也就是 Agent 已取得巡检上下文、正处在一个工具调用的时间窗口——再用 `turn.steer(告警)` 注入告警。

对照 `steer()` 返回的 `turn_id` 与原 Turn：相同，说明告警没有另起一个 Turn，巡检上下文不会丢。
"""
    ),
    code(
        '''import threading
import time

thread = codex.thread_start(
    cwd=str(WORK),
    sandbox=Sandbox.workspace_write,
    approval_mode=ApprovalMode.deny_all,
)
turn = thread.turn(PATROL_TASK, sandbox=Sandbox.workspace_write)

box = {}


def run_patrol():
    box["result"] = turn.run()


worker = threading.Thread(target=run_patrol, daemon=True)
worker.start()
print("巡检 Turn 已启动：", turn.id)

patrol_marker = WORK / "tools" / ".patrol-probe-started"
deadline = time.time() + 180
while time.time() < deadline and not patrol_marker.exists() and worker.is_alive():
    time.sleep(2)
print("server-17 采集探针是否已启动：", patrol_marker.exists())

steer_response = turn.steer(ALERT)
print("steer 返回进入的 Turn：", steer_response.turn_id)
print("是否进入原来的 Turn：", steer_response.turn_id == turn.id)

worker.join(timeout=480)
patrol_result = box.get("result")
patrol_status = getattr(getattr(patrol_result, "status", None), "value", patrol_result)
print("巡检线程是否仍在运行：", worker.is_alive())
print("Turn 最终状态：", patrol_status)
if worker.is_alive():
    print("提示：任务超过等待时间仍在运行，可延长 join 时间后单独等待，不要重复注入告警。")
'''
    ),
    markdown(
        """### 核对正文的四件事

1. 告警是否进入**原来的 Turn**；
2. 故障调查是否使用了**告警到达以前**巡检已取得的变更信息（连接池调整只写在 server-17 的 `deployment.md`，告警文本刻意不含它）；
3. Agent 处理完告警后，是否按要求**继续完成** server-31、server-44 的巡检；
4. 输入文件是否保持不变。
"""
    ),
    code(
        '''incident = WORK / "incident-report.md"
patrol_report = WORK / "patrol-report.md"
incident_txt = incident.read_text(encoding="utf-8") if incident.exists() else ""
patrol_txt = patrol_report.read_text(encoding="utf-8") if patrol_report.exists() else ""

checks = {
    "告警进入原来的 Turn": steer_response.turn_id == turn.id,
    "故障调查用上了告警前巡检取得的变更（连接池）": ("连接池" in incident_txt and "连接池" not in ALERT),
    "处理完告警后继续巡检完三台服务器": all(
        name in patrol_txt for name in ("server-17", "server-31", "server-44")
    ),
    "输入文件保持不变": snapshot_inputs() == BASELINE,
}
for name, ok in checks.items():
    print(("PASS" if ok else "FAIL"), "-", name)

print("\\n===== incident-report.md =====")
print(incident_txt[:1600] if incident_txt else "（未生成）")
print("\\n===== patrol-report.md =====")
print(patrol_txt[:1600] if patrol_txt else "（未生成）")

assert all(checks.values()), "有验收项未通过，请根据上面的 FAIL 项检查模型行为或重跑本格。"
'''
    ),
    markdown(
        """## 7. 观察二：用 `interrupt()` 结束当前执行

`steer()` 是改变接下来怎么继续；`interrupt()` 是直接结束当前执行。再开一个 Turn，让它运行无副作用的慢任务，任务进行中调用 `turn.interrupt()`。

预期：Turn 状态变为 `interrupted`，慢任务要求写的 `interrupt-report.md` **不会**落盘，输入文件也不变。中断只停止 Agent 的执行，**不会回滚**已经产生的副作用——所以这里特意用一个没有业务副作用的只读探针。
"""
    ),
    code(
        '''interrupt_task = (
    "运行 tools/interrupt_probe.py，等待它完整结束后，把运行结果写入 interrupt-report.md；"
    "除这份报告外不要改动其他文件。"
)
turn2 = thread.turn(interrupt_task, sandbox=Sandbox.workspace_write)

box2 = {}


def run_interrupt():
    box2["result"] = turn2.run()


worker2 = threading.Thread(target=run_interrupt, daemon=True)
worker2.start()

interrupt_marker = WORK / "tools" / ".interrupt-probe-started"
deadline = time.time() + 120
while time.time() < deadline and not interrupt_marker.exists() and worker2.is_alive():
    time.sleep(2)
print("慢任务探针是否已启动：", interrupt_marker.exists())

turn2.interrupt()
worker2.join(timeout=60)
interrupt_result = box2.get("result")
interrupt_status = getattr(getattr(interrupt_result, "status", None), "value", interrupt_result)
print("中断后 Turn 状态：", interrupt_status)

interrupt_checks = {
    "Turn 被中断（interrupted）": interrupt_status == "interrupted",
    "没有产出中断报告": not (WORK / "interrupt-report.md").exists(),
    "输入文件保持不变": snapshot_inputs() == BASELINE,
}
for name, ok in interrupt_checks.items():
    print(("PASS" if ok else "FAIL"), "-", name)

assert all(interrupt_checks.values()), "中断演示有验收项未通过，请检查状态或重跑本格。"
print("说明：interrupt 停止的是 Agent 的 Turn；只读探针进程可能自行跑完，但不产生业务副作用、也没有报告落盘。")
'''
    ),
    markdown(
        """## 8. 课后回顾

- **queue（排队）**：当前任务不适合被打断，新消息作为下一条输入排队，稍后处理。
- **steer（转向）**：新消息进入**同一个 Turn**，不丢上下文，在工具结果边界改变接下来怎么做——巡检途中的告警就是 steer。
- **interrupt（中断）**：结束当前执行；已经产生的副作用**不会回滚**。
- **stop（停止）**：关闭整个 Thread，连尚未开始的工作也不再继续（本实验不做破坏性演示）。

这次告警之所以能被快速、准确地处理，正是因为它复用了巡检 Turn 已经建立的上下文（server-17 的连接池变更），而不是重启一个空白 Agent。

运行结束以后，临时工作目录和本地日志仍保留，便于检查失败。分享 Notebook 前清除执行输出，不上传凭证、真实业务材料或完整本机日志。
"""
    ),
    code(
        '''codex.close()
stop_proxy()
print("已关闭 Codex 与本地协议适配器。")
'''
    ),
]

for index, cell in enumerate(cells):
    cell["id"] = f"cell-{index:02d}"

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

if __name__ == "__main__":
    target = Path(__file__).with_name("workshop.ipynb")
    target.write_text(json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)
