"""生成第 01 讲 Notebook；生成过程不启动服务，也不调用模型。"""

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
        """# Lab 01｜从 Python 启动一次 500 告警调查

告警、日志和代码放在同一工作目录中。你只给出调查目标和边界，让 Agent 自己寻找线索，再观察每次工具返回怎样影响后面的行动。不要把文件读取顺序写进任务要求。

这次由 GLM-5.2 提供模型能力，由 Codex App Server 负责组织任务、执行工具和保存运行过程，不需要登录 ChatGPT 或 OpenAI 账号。第 7 步会调用付费 API；前面启动本地服务不会开始调查。请在独立的 Python 3.12 环境中运行，不要放入真实业务材料。
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
        """## 2. 输入 GLM API Key

API Key 只保存在当前内存和本地适配器的进程环境中，不写进 Notebook 或配置文件，也不传给 Codex 的工具执行环境。下面的输入框不会显示密钥。
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
        """## 3. 启动 GLM-5.2 协议适配器

Codex App Server 按 Responses 协议调用模型。下面的单元格在本机启动一个轻量适配器，把请求转给 GLM-5.2 的 Chat Completions 接口。API Key 不会写入配置文件。

适配器还会挂载一个很小的预处理钩子：Codex 按 Responses 协议发送的 `reasoning` 是对象结构，而 GLM 的 Chat Completions 只接受字符串形式的 `reasoning_effort`，钩子在转发前移除这组不兼容字段，其余请求原样转发。
"""
    ),
    code(
        '''import atexit
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

if "proxy" in globals() and proxy.poll() is None:
    raise RuntimeError("适配器已经在运行，请先调用 stop_proxy()，再重新执行本单元格。")

RUNTIME = Path(tempfile.mkdtemp(prefix="codex-lab-"))
LOCAL_TOKEN = "sk-lab01-" + secrets.token_hex(24)

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
  master_key: os.environ/LAB01_PROXY_TOKEN
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
proxy_env.update({"BIGMODEL_API_KEY": _glm_key, "LAB01_PROXY_TOKEN": LOCAL_TOKEN})
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
        """## 4. 连接 Codex App Server

这里把 Codex 的模型 Provider 指向刚才启动的适配器，并明确设置为不需要 OpenAI 账号。第一讲暂时关闭多智能体，只观察一个 Agent 怎样完成一项连续任务。
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

print("Codex App Server 已连接。")
print("Agentic Model：GLM-5.2")
print("OpenAI / ChatGPT 登录：不需要")
'''
    ),
    markdown("## 5. 准备告警、脱敏日志和相关代码"),
    code(
        '''ROOT = Path(tempfile.mkdtemp(prefix="incident-diagnosis-"))
(ROOT / "inputs").mkdir(parents=True)
(ROOT / "outputs").mkdir()

records = {
    "alert.md": """# HTTP 500 告警

- 时间：02:07
- 服务：payment-api
- 接口：POST /checkout
- 状态码：HTTP 500
- 错误率：8.4%
- 样例请求：req-7f3a
""",
    "logs/payment-api.log": """2026-08-24T02:06:58Z INFO request_id=req-7f39 path=/checkout status=200
2026-08-24T02:07:03Z INFO request_id=req-7f3a path=/checkout payload={\"amount\":29900}
2026-08-24T02:07:03Z ERROR request_id=req-7f3a status=500 error=\"KeyError: 'currency'\"
Traceback (most recent call last):
  File \"src/checkout.py\", line 4, in create_order
    \"currency\": payload[\"currency\"],
KeyError: 'currency'
""",
    "src/checkout.py": """def create_order(payload, repository):
    order = {
        \"amount\": payload[\"amount\"],
        \"currency\": payload[\"currency\"],
    }
    return repository.save(order)
""",
}

for name, text in records.items():
    path = ROOT / "inputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

print("工作目录：", ROOT.name)
'''
    ),
    markdown("## 6. 先看看 Agent 会收到哪些材料"),
    code(
        '''from IPython.display import Markdown, display

input_snapshot = {}

for path in sorted((ROOT / "inputs").rglob("*")):
    if not path.is_file():
        continue
    relative = str(path.relative_to(ROOT))
    display_text = path.read_text(encoding="utf-8")
    input_snapshot[relative] = path.read_bytes()
    display(Markdown(f"""### `{relative}`

```text
{display_text}```"""))
'''
    ),
    markdown(
        """## 7. 创建 Thread，启动一次调查 Turn

`cwd` 指定工作目录。`workspace_write` 允许在其中写报告，`deny_all` 拒绝运行中的扩权申请。本讲通过文字要求保护输入，尚未把 `inputs/` 设成只读目录；运行后还要检查输入是否被改动，第 09 讲再落实目录级限制。

下面会调用付费模型。只交给它目标和材料范围，不预先告诉它请求编号、错误类型、代码文件名或调查顺序。实际动作可能与正文中的示例不同，应当按本次事件判断，不能期待每次都执行同一串命令。
"""
    ),
    code(
        '''task = """
请根据 inputs/ 中的告警、日志和代码，定位结账请求失败的最可能原因，
把调查结论写入 outputs/incident-report.md。

每项结论都要注明日志或代码位置。
没有证据的内容不要猜，也不要修改任何输入文件。
只使用这次提供的材料，不查询工作目录之外的文件或网络。
""".strip()

def proxy_failure_hint(log_path):
    if not log_path.is_file():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if "AuthenticationError" in text or "401 Unauthorized" in text:
        return "模型服务拒绝了身份验证。请检查 GLM API Key 及其 API 权限；后续限流可能只是适配器进入冷却期，不能据此判断调查已执行。"
    if "RateLimitError" in text or "429 Too Many Requests" in text:
        return "模型服务或适配器限制了请求。请检查账号额度与服务状态；不要把重试结束当作调查完成。"
    return None

result = None
try:
    thread = codex.thread_start(
        cwd=str(ROOT),
        sandbox=Sandbox.workspace_write,
        approval_mode=ApprovalMode.deny_all,
    )
    result = thread.run(task, sandbox=Sandbox.workspace_write)
except BaseException as exc:
    codex.close()
    stop_proxy()
    hint = proxy_failure_hint(RUNTIME / "litellm.log")
    if hint and isinstance(exc, Exception):
        raise RuntimeError(hint) from exc
    raise

def redact_local_paths(text):
    return (text or "").replace(str(ROOT), ".").replace(str(Path.home()), "~")

print("Thread ID：", thread.id)
print("Turn ID：", result.id)
print("Turn 状态：", result.status.value)
print("最终回答：", redact_local_paths(result.final_response))
'''
    ),
    markdown("## 8. 展开 Harness 留下的事件"),
    code(
        '''from html import escape
from IPython.display import HTML

def render_event_detail(value):
    value = redact_local_paths(value).replace("\\r\\n", "\\n")
    return escape(value)

def event_rows(items):
    rows = []
    for wrapped in items:
        item = wrapped.root
        if item.type == "commandExecution":
            rows.append(("命令", item.command))
            rows.append(("命令结果", item.aggregated_output if item.aggregated_output is not None else "未提供输出"))
            exit_code = item.exit_code if item.exit_code is not None else "未提供"
            rows.append(("退出码", str(exit_code)))
        elif item.type == "fileChange":
            paths = ", ".join(change.path for change in item.changes)
            rows.append(("文件操作", f"状态：{item.status.value}\\n{paths}"))
        elif item.type == "agentMessage" and item.text.strip():
            rows.append(("Agent 消息", item.text))
    return rows

table_rows = "".join(
    f"<tr><td>{escape(kind)}</td><td><pre style='white-space:pre-wrap'>{render_event_detail(detail)}</pre></td></tr>"
    for kind, detail in event_rows(result.items)
)
display(HTML(
    "<table><thead><tr><th>事件</th><th>内容</th></tr></thead>"
    f"<tbody>{table_rows}</tbody></table>"
))
'''
    ),
    markdown("## 9. 查看调查报告"),
    code(
        '''report_path = ROOT / "outputs" / "incident-report.md"
report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
display(Markdown("### `incident-report.md`\\n\\n" + (report or "报告尚未生成。")))
'''
    ),
    markdown("## 10. 回看这次运行"),
    code(
        '''def compare_inputs(root, before):
    current = {
        str(path.relative_to(root)): path.read_bytes()
        for path in (root / "inputs").rglob("*") if path.is_file()
    }
    return {
        "changed": sorted(key for key in before.keys() & current.keys() if before[key] != current[key]),
        "missing": sorted(before.keys() - current.keys()),
        "added": sorted(current.keys() - before.keys()),
    }

def run_observations(result, root, before):
    report = root / "outputs" / "incident-report.md"
    return {
        "turn_status": result.status.value,
        "report_exists": report.is_file() and report.stat().st_size > 0,
        "inputs": compare_inputs(root, before),
    }

output_paths = sorted(
    str(path.relative_to(ROOT))
    for path in (ROOT / "outputs").rglob("*")
    if path.is_file()
)

observed = run_observations(result, ROOT, input_snapshot)
print("Turn 实际状态：", observed["turn_status"])
print("非空报告是否存在：", observed["report_exists"])
print("输入变化：", observed["inputs"])
print("这些检查只反映文件和运行状态，报告的结论及引用仍需对照材料阅读。")

for relative, before in input_snapshot.items():
    path = ROOT / relative
    current = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "[文件已不存在]"
    state = "与运行前一致" if path.is_file() and path.read_bytes() == before else "内容已改变或文件已不存在"
    display(Markdown(f"""### `{relative}` · {state}

```text
{current}```"""))

print("输出文件：")
for path in output_paths:
    print(" -", path)

codex.close()
stop_proxy()
'''
    ),
    markdown(
        """## 继续观察

对照工具事件中的命令和返回内容，找出一处“先得到线索，再改变下一步行动”的过程。如果这次模型合并读取了几个文件，如实记录，不要把它改写成逐步搜索。

运行结束以后，临时工作目录和本地日志仍保留，便于检查失败。分享 Notebook 前清除执行输出，不上传凭证、真实业务材料或完整本机日志。
"""
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
