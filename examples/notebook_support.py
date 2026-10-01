"""第 03 至 09 讲共用的本地运行设施；教学机制写在各讲 notebook 中。"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import http.client
import json
import re
import shlex
import signal
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox


class Lab:
    """启动真实 SDK 与 GLM 适配器，并记录不含认证头的模型请求。"""

    def __init__(self, directory: Path):
        key = os.environ.get("GLM_API_KEY", "").strip()
        if not key:
            raise ValueError("请在启动进程前设置 GLM_API_KEY 环境变量。")
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("请使用 Python 3.12 运行本练习。")
        for package, expected in (("openai-codex", "0.154.0"), ("litellm", "1.101.0")):
            if version(package) != expected:
                raise RuntimeError(f"请安装 {package}=={expected}，本练习按该版本核验。")
        self.directory = directory.resolve()
        self.run_id = os.environ.get("LAB_VALIDATION_RUN_ID") or (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(8))
        if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", self.run_id):
            raise ValueError("本次运行编号不合法。")
        self.runtime = self.directory / ".runtime" / self.run_id
        self.runtime.mkdir(parents=True, mode=0o700, exist_ok=False)
        self._secrets = [key]
        self._request_lock = threading.Lock()
        self._extra_clients = []
        self.work = self.runtime / "work"
        self.work.mkdir()
        (self.runtime / "codex-home").mkdir()
        # 标记实验项目根，避免把上层项目规则当成本讲的实验条件。
        (self.work / ".git").mkdir()
        self.requests = []
        self.results = []
        self.turn_objects = []
        self.codex = self.proxy = self.observer = self.log = None
        self._log_thread = None
        self._closed = False
        self._cleanup_errors = []
        atexit.register(self.close)
        try:
            self._start(key)
        except BaseException:
            self.close()
            raise

    def _start(self, key):
        token = "sk-lab-" + secrets.token_hex(24)
        self._secrets.append(token)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (self.runtime / "custom_handler.py").write_text('''from litellm.integrations.custom_logger import CustomLogger
class GlmFix(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data.get("reasoning_effort"), str):
            data.pop("reasoning_effort", None)
        data.pop("reasoning", None)
        return data
proxy_handler = GlmFix()
''', encoding="utf-8")
        config_path = self.runtime / "litellm.yaml"
        config_path.write_text('''model_list:
  - model_name: glm-codex
    litellm_params:
      model: openai/chat_completions/glm-5.2
      api_base: https://open.bigmodel.cn/api/paas/v4
      api_key: os.environ/GLM_API_KEY
general_settings:
  master_key: os.environ/LAB_PROXY_TOKEN
litellm_settings:
  drop_params: true
  set_verbose: false
  callbacks: custom_handler.proxy_handler
''', encoding="utf-8")
        env = os.environ.copy()
        # 不联网拉模型成本表：既避免启动阶段被代理拖住，也减少一次外部依赖。
        env.update(GLM_API_KEY=key, LAB_PROXY_TOKEN=token, PYTHONPATH=str(self.runtime),
                   LITELLM_LOCAL_MODEL_COST_MAP="true")
        self.log = (self.runtime / "adapter.log").open("w", encoding="utf-8")
        self.proxy = subprocess.Popen(
            [str(Path(sys.executable).with_name("litellm")), "--config", str(config_path),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=self.runtime, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
        def capture_log():
            for line in self.proxy.stdout:
                for secret in self._secrets:
                    line = line.replace(secret, "<密钥已隐藏>")
                self.log.write(line)
                self.log.flush()
        self._log_thread = threading.Thread(target=capture_log, daemon=True)
        self._log_thread.start()
        for _ in range(90):
            if self.proxy.poll() is not None:
                raise RuntimeError("适配器启动失败，请检查本讲 .runtime 内的本地日志。")
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        f"http://127.0.0.1:{port}/health/liveliness", timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise TimeoutError("适配器启动超时。")

        lab = self

        class Observer(BaseHTTPRequestHandler):
            # 只记录 JSON 请求体中的教学字段，不记录 HTTP 认证头。
            def do_POST(self):
                self.connection.settimeout(10)
                if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8 * 1024 * 1024:
                        raise ValueError("请求体长度不合法")
                    body = self.rfile.read(length)
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ValueError("请求体必须是对象")
                except (ValueError, OSError):
                    self.send_error(400)
                    return
                observed = {k: v for k, v in payload.items() if k in (
                    "model", "instructions", "input", "tools", "previous_response_id", "reasoning")}
                # 单独复制，避免协议转换和并发处理改写已经保存的请求。
                observed = json.loads(json.dumps(observed))
                with lab._request_lock:
                    lab.requests.append(observed)
                    lab._write_json(f"request-{len(lab.requests):03}.json", observed)
                # Responses 允许 ExternalMessage 没有 call_id；Chat Completions
                # 要求工具结果与一个调用配对。补协议信封，保持工具级权限，
                # 不把外部材料抬成 user/developer 指令，也不执行任何新工具。
                if isinstance(payload.get("input"), list):
                    converted = []
                    for item in payload["input"]:
                        if isinstance(item, dict) and item.get("type") == "function_call_output" and not item.get("call_id"):
                            call_id = "external_" + str(item.get("id") or secrets.token_hex(6))
                            converted.append({"type": "function_call", "call_id": call_id,
                                              "name": item.get("name", "external_delivery"), "arguments": "{}"})
                            item = {**item, "call_id": call_id}
                        converted.append(item)
                    payload["input"] = converted
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
                try:
                    conn.request("POST", self.path, body=body, headers={
                        "Content-Type": "application/json", "Authorization": "Bearer " + token})
                    response = conn.getresponse()
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, http.client.HTTPException) as exc:
                    lab._write_json("adapter-failure.json", {"error_type": type(exc).__name__, "error": lab.clean(exc)})
                    self.close_connection = True
                finally:
                    conn.close()

            def log_message(self, *_args):
                pass

        self.observer = ThreadingHTTPServer(("127.0.0.1", 0), Observer)
        self.observer.daemon_threads = True
        threading.Thread(target=self.observer.serve_forever, daemon=True).start()
        self.config = CodexConfig(
            cwd=str(self.work),
            config_overrides=(
                'model="glm-codex"', 'model_provider="lab"',
                'model_providers.lab.name="GLM-5.2 本地适配器"',
                f'model_providers.lab.base_url="http://127.0.0.1:{self.observer.server_port}/v1"',
                'model_providers.lab.env_key="LAB_LOCAL_TOKEN"',
                'model_providers.lab.wire_api="responses"',
                'model_providers.lab.requires_openai_auth=false',
                'features.multi_agent=false', 'features.multi_agent_v2=false',
                'shell_environment_policy.inherit="core"',
                'sandbox_workspace_write.network_access=false',
                'sandbox_workspace_write.exclude_tmpdir_env_var=true',
                'sandbox_workspace_write.exclude_slash_tmp=true',
                'shell_environment_policy.exclude=["*KEY*","*TOKEN*","*SECRET*","*PASSWORD*"]',
            ),
            env={**{name: "" for name in os.environ
                    if any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))},
                 "CODEX_HOME": str(self.runtime / "codex-home"),
                 "LAB_LOCAL_TOKEN": token, "GLM_API_KEY": "", "BIGMODEL_API_KEY": "",
                 "OPENAI_API_KEY": "",
                 # 有的启动器把中文路径编码损坏后放进 shell 的最近命令变量。
                 # Codex 0.154.0 构造工具环境时要求有效 UTF-8。
                 "_": sys.executable},
        )
        self.codex = Codex(self.config)

    def _write_json(self, name, value):
        # 原始路径仅留在私有运行目录；凭证在任何 JSON 记录中都隐藏。
        text = json.dumps(value, ensure_ascii=False, indent=2)
        for secret in self._secrets:
            text = text.replace(secret, "<密钥已隐藏>")
        (self.runtime / name).write_text(text, encoding="utf-8")

    def manage_client(self, client):
        """额外的底层客户端也由本次 Lab 负责释放。"""
        self._extra_clients.append(client)
        return client

    def thread(self, **kwargs):
        cwd = Path(kwargs.get("cwd", self.work)).resolve()
        if not cwd.is_dir() or not cwd.is_relative_to(self.work):
            raise ValueError("Thread 的 cwd 必须是本次 work 内已存在的目录。")
        kwargs["cwd"] = str(cwd)
        options = dict(cwd=str(self.work), sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all)
        options.update(kwargs)
        return self.codex.thread_start(**options)

    def run(self, thread, prompt, *, timeout=900, progress_every=15, show_result=True, **kwargs):
        """限定一次 Turn 的等待时间；失败不伪装成模型完成。

        等待期间每 progress_every 秒打印一次进度，避免读者误以为卡死。
        超时先请求中断：多数情况下模型轮能停下，本次连接保留，读者可直接重跑本格；
        只有中断后仍无法停止时才关闭连接并整本断链。
        """
        box = {}
        def worker():
            try:
                # turn/start 的协议等待也计入预算，不能在计时线程外阻塞。
                handle = thread.turn(prompt, **kwargs)
                box["handle"] = handle
                box["result"] = handle.run()
            except BaseException as exc:
                box["error"] = exc
        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        waited = 0
        while worker_thread.is_alive() and waited < timeout:
            step = min(progress_every, timeout - waited)
            worker_thread.join(step)
            waited += step
            if worker_thread.is_alive():
                print(f"Turn 仍在运行，已等待 {waited} 秒（上限 {timeout} 秒）……")
        if worker_thread.is_alive():
            interruption = []
            def interrupt():
                try:
                    if "handle" in box:
                        box["handle"].interrupt()
                except Exception as exc:
                    interruption.append(self.clean(exc))
            stopper = threading.Thread(target=interrupt, daemon=True)
            stopper.start()
            stopper.join(5)
            worker_thread.join(10)
            self._write_json("turn-failure.json", {"thread_id": thread.id, "status": "timeout",
                "timeout_seconds": timeout, "interrupt_errors": interruption})
            if worker_thread.is_alive():
                try:
                    self.close()
                finally:
                    raise TimeoutError(f"本次 Turn 超过 {timeout} 秒且中断无效；已关闭本次连接。")
            raise TimeoutError(
                f"本次 Turn 超过 {timeout} 秒；已请求中断，本次连接保留，可直接重跑本格继续。")
        if "error" in box:
            exc = box["error"]
            self._write_json("turn-failure.json", {"thread_id": thread.id,
                "error_type": type(exc).__name__, "error": self.clean(exc)})
            raise RuntimeError(f"Turn 执行失败：{type(exc).__name__}: {self.clean(exc)}") from None
        result = box["result"]
        return self.record_result(box["handle"], result, show_result=show_result)

    @contextmanager
    def watch(self, turn, *, timeout=900, progress_every=15):
        """只提供等待进度与超时中断；SDK 的 turn.run() 仍由 Notebook 调用。"""
        finished, expired = threading.Event(), threading.Event()
        start = time.monotonic()

        def guard():
            while True:
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    break
                if finished.wait(min(progress_every, remaining)):
                    return
                if time.monotonic() - start < timeout:
                    print(f"Turn 仍在运行，已等待 {int(time.monotonic() - start)} 秒（上限 {timeout} 秒）……")
            expired.set()
            self._write_json("turn-failure.json", {"thread_id": turn.thread_id,
                "turn_id": turn.id, "status": "timeout", "timeout_seconds": timeout})
            try:
                turn.interrupt()
            except Exception as exc:
                self._write_json("interrupt-failure.json", {"error": self.clean(exc)})
            if not finished.wait(10):
                self.close()

        watcher = threading.Thread(target=guard, daemon=True)
        watcher.start()
        try:
            yield
        except BaseException as exc:
            self._write_json("turn-failure.json", {"thread_id": turn.thread_id, "turn_id": turn.id,
                "status": "timeout" if expired.is_set() else "failed", "error": self.clean(exc)})
            raise
        finally:
            finished.set()
            watcher.join(1)
        if expired.is_set():
            raise TimeoutError(f"本次 Turn 超过 {timeout} 秒，已请求中断；请查看本次记录。")

    def record_result(self, turn, result, *, show_result=True):
        """保存 SDK 已经返回的结果；不创建任务、不调用模型、不补写业务文件。"""
        self.turn_objects.append(result)
        self.results.append({"thread_id": turn.thread_id, "turn_id": result.id,
                             "status": result.status.value})
        self._write_json(f"turn-{len(self.results):03}.json",
            {"thread_id": turn.thread_id, "turn_id": result.id, "status": result.status.value,
             "final_response": result.final_response,
             "items": [i.model_dump(mode="json", by_alias=True) for i in result.items]})
        if result.status.value != "completed":
            raise AssertionError(f"Turn 未完成：{result.status.value}；请查看本次 turn 记录。")
        if show_result:
            print("Turn：", result.status.value)
            print(self.clean(result.final_response))
        return result

    def clean(self, text):
        text = str(text)
        for secret in self._secrets:
            text = text.replace(secret, "<密钥已隐藏>")
        return text.replace(str(self.runtime), "<本次运行目录>").replace(str(Path.home()), "~")

    def request_text(self, index=-1):
        if not self.requests or not -len(self.requests) <= index < len(self.requests):
            raise AssertionError(f"未捕获预期的模型请求：索引 {index}，实际 {len(self.requests)} 条。")
        return json.dumps(self.requests[index], ensure_ascii=False)

    def show_requests(self, start=0):
        """展示真实请求的组成；不把字节数或字符数称为 token 数。"""
        for i, request in enumerate(self.requests[start:], start + 1):
            inputs = request.get("input", [])
            print(f"请求 {i}：工具 {len(request.get('tools', []))} 个；输入字符 {len(json.dumps(inputs, ensure_ascii=False))}")
            for item in inputs if isinstance(inputs, list) else []:
                print(" ", item.get("type", "message"), item.get("role", ""),
                      self.clean(json.dumps(item, ensure_ascii=False))[:220])

    def verify(self, checks, **facts):
        if not checks or any(type(value) is not bool for value in checks.values()):
            raise ValueError("检查项不能为空，且每项必须是 bool；字符串或计数不能代替判断。")
        reserved = {"model", "sdk", "adapter", "checked_at", "checks", "turns", "request_count", "run_id"}
        if reserved.intersection(facts):
            raise ValueError("附加事实不能覆盖验证记录的保留字段。")
        for name, passed in checks.items():
            print(("PASS" if passed else "FAIL"), name)
        record = {"run_id": self.run_id, "model": "glm-5.2", "sdk": version("openai-codex"),
                  "adapter": version("litellm"), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "checks": checks, "turns": self.results, "request_count": len(self.requests), **facts}
        self._write_json("checks.json", record)
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise AssertionError("检查失败：" + "；".join(failed))
        return record

    def close(self):
        if self._closed:
            if self._cleanup_errors:
                raise RuntimeError("上次进程清理未完全成功：" + "；".join(self._cleanup_errors))
            return
        self._closed = True
        errors = []
        def attempt(action):
            try:
                action()
            except Exception as exc:
                errors.append(self.clean(exc))
        for client in reversed(self._extra_clients):
            attempt(client.close)
        if self.codex:
            attempt(self.codex.close)
        if self.observer:
            attempt(self.observer.shutdown)
            attempt(self.observer.server_close)
        if self.proxy:
            def stop_proxy():
                # 适配器可能派生工作进程，一并结束本次专属进程组。
                try:
                    os.killpg(self.proxy.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.proxy.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proxy.pid, signal.SIGKILL)
                    self.proxy.wait(5)
            attempt(stop_proxy)
        if self._log_thread:
            self._log_thread.join(5)
        if self.proxy and self.proxy.stdout and not (self._log_thread and self._log_thread.is_alive()):
            attempt(self.proxy.stdout.close)
        if self._log_thread and self._log_thread.is_alive():
            errors.append("适配器日志线程未退出")
        elif self.log:
            attempt(self.log.close)
        atexit.unregister(self.close)
        self._cleanup_errors = errors
        if errors:
            self._write_json("cleanup-failure.json", {"errors": errors})
            raise RuntimeError("进程清理未完全成功：" + "；".join(errors))


def tool_output_contains(request, marker):
    """只看工具回执，避免把用户消息或模型复述误认作工具结果。"""
    inputs = request.get("input", [])
    return isinstance(inputs, list) and any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        and marker in str(item.get("output", "")) for item in inputs)


def command_receipts(requests):
    """按 call_id 配对真实命令和回执；重复携带的历史只计一次。"""
    calls, receipts = {}, {}
    for request in requests:
        inputs = request.get("input", [])
        for item in inputs if isinstance(inputs, list) else []:
            if not isinstance(item, dict):
                continue
            call_id = item.get("call_id")
            if not call_id:
                continue
            if item.get("type") == "function_call" and item.get("name") == "exec_command":
                try:
                    args = json.loads(item.get("arguments", "{}"))
                except (TypeError, ValueError):
                    continue
                if isinstance(args, dict) and isinstance(args.get("cmd"), str):
                    calls[call_id] = args
            elif item.get("type") == "function_call_output" and call_id in calls:
                output = str(item.get("output", ""))
                match = re.search(r"^Process exited with code (-?\d+)\s*$", output, re.M)
                receipts[call_id] = {"call_id": call_id, "command": calls[call_id]["cmd"],
                    "arguments": calls[call_id], "output": output,
                    "exit_code": int(match[1]) if match else None}
    return list(receipts.values())


def denied_write(receipts, command, target):
    """必须是指定目标的实际写入拒绝；启动失败、缺路径和无回执均失败。"""
    for receipt in receipts:
        output = receipt["output"].lower()
        if (receipt["command"] == command and receipt["exit_code"] not in (None, 0)
            and str(target).lower() in output
            and not any(word in output for word in ("sandbox_apply", "no such file", "command not found"))
            and any(word in output for word in ("operation not permitted", "permission denied", "read-only file system"))):
            return True
    return False


def exact_command(raw, expected):
    """只展开一个明确的 shell 包装，不接受尾随命令或任意解释器。"""
    try:
        parts = raw if isinstance(raw, list) else shlex.split(raw)
        if not all(isinstance(part, str) for part in parts):
            return False
        return parts == shlex.split(expected) or (
            len(parts) == 3 and parts[0] in ("/bin/sh", "/bin/bash", "/bin/zsh")
            and parts[1] in ("-c", "-lc") and parts[2] == expected)
    except (ValueError, TypeError):
        return False


def read_json_answer(text, required):
    """仅剥离首尾空白和完整 JSON 围栏；对象结构与字段仍严格检查。"""
    text = text.strip()
    fenced = re.fullmatch(r"```(?:[ \t]*json)?[ \t]*(?:\r?\n)?(.*?)(?:\r?\n)?[ \t]*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        answer = json.loads(text)
    except ValueError as exc:
        raise AssertionError("模型回答不是完整 JSON 对象，请核对本次 Turn 输出。") from exc
    if not isinstance(answer, dict) or set(answer) != set(required):
        raise AssertionError(f"回答字段不符合约定，要求：{', '.join(required)}")
    return answer


class SkillExtractionStory:
    """07 讲的准备与过程排版；只展示实际请求、回执和文件采样。"""

    new_task = "请读取 current/case.md，给出当前支付延迟的简短判断与下一步，说明依据，不实施变更。"

    def __init__(self, directory):
        import shutil
        from openai_codex import SkillInput, TextInput
        self.lab = Lab(directory)
        self.work = self.lab.work
        try:
            self.creator = self.work / "tooling" / "skill-creator"
            shutil.copytree(directory / "skill_creator", self.creator)
            self.projects = {name: self.work / (name + "-project") for name in ("plain", "candidate")}
            for project in self.projects.values():
                project.mkdir()
                (project / ".git").mkdir()
                (project / "current").mkdir()
            self.skill_dir = self.projects["candidate"] / ".agents/skills/latency-investigation"
            self.skill_path = self.skill_dir / "SKILL.md"
            old_record = ("教学案例 OLD-184。旧巡检猜测 CPU 饱和。当前 CPU 21%，连接占用 60/60，"
                          "连接池等待 3600ms，下游耗时 110ms。01:40 连接池上限从 200 调到 60。"
                          "这组材料否定 CPU 饱和，但时间关系还不能直接证明根因。材料编号 OLD-" + secrets.token_hex(8))
            new_record = ("同一窗口 CPU 23%，连接占用 18/200，连接等待 9ms，"
                          "下游耗时 4200ms，接口总耗时 4300ms。没有提供配置变更记录。材料编号 NEW-" + secrets.token_hex(8))
            (self.work / "old-case.md").write_text(old_record, encoding="utf-8")
            for project in self.projects.values():
                (project / "current/case.md").write_text(new_record, encoding="utf-8")
            self.extraction_input = [
                SkillInput(name="skill-creator", path=str(self.creator / "SKILL.md")),
                TextInput("使用当前 Thread 作为完整教学案例，完善 " + str(self.skill_path) +
                          "。保留模块边界、查询入口、指标含义和取证方法，入口是每次读取当前项目的 current/case.md。"
                          "删除旧事故编号、材料编号、实例参数、采样数值和最终根因，不预设未来根因。"
                          "只修改这份 SKILL.md 和已有 agents/openai.yaml，内容用中文，name 保持 latency-investigation。"
                          "不要新增 references、scripts、assets，不安装个人 Skill。目录已由原生脚本初始化。"),
            ]
            self.application_input = [SkillInput(name="latency-investigation", path=str(self.skill_path)), TextInput(self.new_task)]
            self.request_states = []
            self.request_times = []
            self.phase_metrics = []
            story = self

            class ObservedRequests(list):
                def append(self, request):
                    # 收到下一次模型请求时，上次工具结果已经返回；在这个边界采样。
                    snapshot = story.file_state()
                    story.request_states.append(snapshot)
                    story.request_times.append(time.monotonic())
                    super().append(request)
                    story.lab._write_json(f"file-state-{len(self):03}.json", snapshot)

            self.lab.requests = ObservedRequests()
            self.details('运行环境',
                         'Python 3.12；openai-codex 0.154.0；LiteLLM 1.101.0；SOCKS 支持。\n'
                         '模型：GLM-5.2；本地适配器负责连接。\n'
                         '事故材料与候选保存在本次临时项目中。')
        except BaseException:
            self.lab.close()
            raise

    def table(self, headers, rows):
        from html import escape
        from IPython.display import HTML, display
        head = "".join("<th>" + escape(str(v)) + "</th>" for v in headers)
        body = "".join("<tr>" + "".join("<td><pre style='white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%;margin:0'>"
                       + escape(self.lab.clean(v)) + "</pre></td>" for v in row) + "</tr>" for row in rows)
        display(HTML("<table style='width:100%;table-layout:fixed'><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>"))

    def details(self, title, value):
        from html import escape
        from IPython.display import HTML, display
        display(HTML("<details><summary>" + escape(title) + "</summary><pre style='white-space:pre-wrap'>"
                     + escape(self.lab.clean(value)) + "</pre></details>"))

    def heading(self, text):
        from IPython.display import Markdown, display
        display(Markdown(text))

    def short(self, value, limit=700):
        text = self.lab.clean(value)
        if len(text) <= limit:
            return text
        excerpt = text[:limit]
        if '\n' in excerpt:
            excerpt = excerpt.rsplit('\n', 1)[0]
        return excerpt + '\n\n……'

    def validate(self, directory):
        result = subprocess.run([sys.executable, str(self.creator / "scripts/quick_validate.py"), str(directory)],
                                capture_output=True, text=True)
        return {"exit_code": result.returncode, "output": result.stdout + result.stderr}

    def file_state(self):
        paths = [self.work / "old-case.md", *(p / "current/case.md" for p in self.projects.values()),
                 self.skill_path, self.skill_dir / "agents/openai.yaml"]
        state = {}
        for path in paths:
            name = str(path.relative_to(self.work))
            if path.is_file():
                data = path.read_bytes()
                import hashlib
                state[name] = {"bytes": len(data), "lines": len(data.splitlines()),
                               "sha256": hashlib.sha256(data).hexdigest()}
            else:
                state[name] = None
        state["quick_validate"] = self.validate(self.skill_dir) if self.skill_path.is_file() else None
        return state

    def validation_text(self, result):
        if result is None:
            return '候选尚不存在，未校验'
        if result['exit_code'] == 0:
            return '通过；只检查结构，不评价方法偏见'
        output = result.get('output', '')
        if 'TODO' in output:
            return '失败：模板仍含未完成的 TODO'
        if 'frontmatter' in output.lower() or 'yaml' in output.lower():
            return '失败：YAML 头部需要修正'
        if 'description' in output.lower():
            return '失败：description 字段需要修正'
        return '失败：结构检查报告问题'

    def show_state(self, before, after, *, validation=False):
        def describe(value):
            return '不存在' if value is None else f"{value['lines']} 行 / {value['bytes']} 字节"
        rows = []
        for name in sorted(set(before) | set(after)):
            if name == 'quick_validate':
                continue
            a, b = before.get(name), after.get(name)
            if a != b:
                change = '新建' if a is None else '删除' if b is None else '内容已改写'
                rows.append((name, describe(a), describe(b), change))
        if rows:
            self.table(['文件采样', '之前', '之后', '变化'], rows)
        if validation:
            result = after.get('quick_validate')
            self.heading('**quick_validate：**' + self.validation_text(result))
            if result and result['exit_code'] != 0:
                self.details('结构校验原文', result['output'])

    def record_phase(self, title, count, seconds):
        self.phase_metrics.append({'stage': title, 'requests': count, 'seconds': round(seconds, 3)})
        self.lab._write_json('narrative-stages.json', self.phase_metrics)

    def phase_end(self, count, note=''):
        self.heading(f'这一阶段模型共发起 **{count} 轮请求**，关键变化如上。' + note)

    def show_materials(self):
        self.table(['现场', '本次材料'], [
            ('旧事故 · old-case.md', (self.work / 'old-case.md').read_text()),
            ('新事故 · 两份相同的 current/case.md', (self.projects['plain'] / 'current/case.md').read_text()),
        ])
        self.heading('候选起点：`SKILL.md` 尚不存在。材料编号只用于核对读取，不应保存在通用方法中。')

    def session(self, project=None):
        return self.lab.thread(cwd=self.projects[project] if project else self.work, sandbox=Sandbox.full_access)

    def turn(self, session, prompt):
        started = time.monotonic()
        before, start = self.file_state(), len(self.lab.requests)
        # 等候记录留在运行目录，读者展示只保留影响判断的过程。
        from contextlib import redirect_stdout
        story, original_stdout = self, sys.stdout
        progress_lines = []

        class Progress:
            def __init__(self):
                self.pending = ''

            def write(self, text):
                self.pending += text
                while '\n' in self.pending:
                    line, self.pending = self.pending.split('\n', 1)
                    if line.startswith('Turn 仍在运行，'):
                        progress_lines.append(line)
                        story.lab._write_json(f'turn-progress-{start + 1:03}.json', progress_lines)
                    else:
                        original_stdout.write(line + '\n')
                return len(text)

            def flush(self):
                original_stdout.flush()

        writer = Progress()
        try:
            with redirect_stdout(writer):
                result = self.lab.run(session, prompt, sandbox=Sandbox.full_access, timeout=900, show_result=False)
        finally:
            if writer.pending:
                original_stdout.write(writer.pending)
        return {'prompt': prompt, 'start': start, 'end': len(self.lab.requests), 'result': result,
                'before': before, 'after': self.file_state(), 'started': started, 'finished': time.monotonic()}

    @staticmethod
    def request_items(request):
        values = request.get('input', [])
        return [x for x in values if isinstance(x, dict)] if isinstance(values, list) else []

    @staticmethod
    def error_kind(text):
        text = str(text)
        if 'Failed to find expected lines' in text:
            return '补丁未匹配当前文件内容'
        if 'Invalid patch' in text or 'invalid hunk' in text.lower() or 'patch must start' in text.lower():
            return '补丁格式不符合工具要求'
        if 'usage:' in text.lower() or 'illegal option' in text.lower():
            return '命令参数不符合工具用法'
        if 'Error parsing function call' in text or 'invalid arguments' in text.lower():
            return '工具参数无法解析'
        if 'Operation not permitted' in text or 'Permission denied' in text:
            return '工具操作被当前环境拒绝'
        if re.search(r'(?:^|\n)(?:Error:|error:|[^\n]{0,60}verification failed:)|Failed to apply patch', text):
            return '工具报告错误'
        return None

    def action(self, name, arguments):
        """只从实际参数辨认动作，不把完整命令放入读者展示。"""
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (ValueError, TypeError):
            args = {}
        command = str(args.get('cmd', args.get('command', arguments))) if isinstance(args, dict) else str(arguments)
        target = 'SKILL.md 与界面配置' if 'openai.yaml' in command and 'SKILL.md' in command else 'agents/openai.yaml' if 'openai.yaml' in command else '候选 SKILL.md'
        if 'apply_patch' in name or '*** Begin Patch' in command:
            return 'write', 'apply_patch', '尝试修改' + target
        if re.search(r'\b(tee|sed\s+-i|write_text|write_bytes)\b|\bcat\s*>|\bcat\b[^\n]*<<|\bpython\b[^\n]*<<', command) or 'write_text(' in command:
            return 'write', name, '尝试重写' + target
        if 'quick_validate' in command:
            return 'validate', 'quick_validate', '校验候选结构'
        if re.search(r'\b(cat|sed|head|read_file)\b', command) or 'read_text(' in command:
            if 'old-case.md' in command:
                target = 'old-case.md，取得旧事故材料'
            elif 'current/case.md' in command:
                target = 'current/case.md，取得新事故材料'
            elif 'tooling/' in command or 'skill-creator/' in command:
                target = 'Skill Creator 的创建说明'
            elif 'openai.yaml' in command:
                target = 'agents/openai.yaml，核对界面配置'
            elif 'SKILL.md' in command:
                target = '候选 SKILL.md，核对当前正文'
            else:
                target = '当前文件'
            return 'read', name, '读取 ' + target
        if re.search(r'\b(find|ls|pwd)\b', command):
            return 'inspect', name, '核对目录与文件范围'
        return 'other', name, '执行辅助操作'

    def timeline(self, task):
        result_items = [wrapped.root.model_dump(mode='json', by_alias=True) for wrapped in task['result'].items]
        executions = {x['id']: x for x in result_items if x.get('type') in ('commandExecution', 'fileChange')}
        initial = self.request_items(self.lab.requests[task['start']]) if task['end'] > task['start'] else []
        seen = {x.get('call_id') for x in initial if x.get('type') == 'function_call'}
        rows = []
        for index in range(task['start'], task['end']):
            following = self.request_items(self.lab.requests[index + 1]) if index + 1 < task['end'] else []
            receipts = {x.get('call_id'): x.get('output', '') for x in following if x.get('type') == 'function_call_output'}
            events = []
            for call in following:
                call_id = call.get('call_id')
                if call.get('type') != 'function_call' or call_id in seen:
                    continue
                seen.add(call_id)
                receipt = str(receipts.get(call_id, ''))
                kind, tool, description = self.action(call.get('name', '工具'), call.get('arguments', ''))
                code = executions.get(call_id, {}).get('exitCode')
                match = re.search(r'Process exited with code (-?\d+)', receipt)
                if code is None and match:
                    code = int(match[1])
                error = self.error_kind(receipt)
                if code not in (None, 0) and error is None:
                    error = '命令未成功退出'
                events.append({'kind': kind, 'tool': tool, 'action': description, 'error': error,
                               'receipt': receipt, 'exit_code': code})
            if index + 1 == task['end']:
                for item in executions.values():
                    if item['id'] in seen:
                        continue
                    seen.add(item['id'])
                    kind, tool, description = self.action('apply_patch' if item['type'] == 'fileChange' else 'exec_command',
                                                         {'cmd': item.get('command', str(item.get('changes', [])))})
                    receipt = str(item.get('aggregatedOutput', item.get('status', '')))
                    code = item.get('exitCode')
                    error = self.error_kind(receipt)
                    if code not in (None, 0) and error is None:
                        error = '命令未成功退出'
                    events.append({'kind': kind, 'tool': tool, 'action': description, 'error': error,
                                   'receipt': receipt, 'exit_code': code})
            before = self.request_states[index]
            after = self.request_states[index + 1] if index + 1 < task['end'] else task['after']
            changed = any(before.get(k) != after.get(k) for k in set(before) | set(after) if k != 'quick_validate')
            rows.append({'index': index, 'events': events, 'before': before, 'after': after, 'changed': changed})
        return rows

    def show_key_actions(self, rows, *, include_errors=False):
        selected, seen, displayed_rounds = [], set(), set()
        for row in rows:
            for event in row['events']:
                signature = (event['kind'], event['action'])
                if event['error']:
                    if not include_errors:
                        continue
                    signature = ('error', event['error'])
                if signature in seen:
                    continue
                seen.add(signature)
                displayed_rounds.add(row['index'])
                result = event['error'] if event['error'] else '动作结果尚不明确' if not event['receipt'] else '工具返回结果' if event['exit_code'] is None else '执行成功' if event['exit_code'] == 0 else '执行失败'
                selected.append((event['tool'] + ' · ' + event['action'], result))
        if selected:
            self.table(['首次动作 / 关键动作', '本次结果'], selected)
        quiet = [row for row in rows if not row['changed'] and row['events']
                 and all(e['kind'] == 'read' and not e['error'] for e in row['events'])]
        repeated = sum(row['index'] not in displayed_rounds for row in quiet)
        if repeated:
            self.heading(f'模型又读取了 **{repeated} 轮**文件，候选内容没有改变。')

    def show_changes(self, rows):
        for row in rows:
            if row['changed']:
                self.show_state(row['before'], row['after'])

    def answer_summary(self, title, task, limit=650):
        self.heading('**' + title + ' · 模型回答（节选）**')
        self.heading(self.short(task['result'].final_response, limit))
        self.details(title + ' · 模型回答', task['result'].final_response)

    def investigate(self):
        started = time.monotonic()
        self.show_materials()
        session = self.session()
        initial = self.turn(session, '旧巡检猜测支付延迟来自 CPU 饱和。请读取 old-case.md，'
                            '依据当前材料提出只读核对计划，区分猜测和事实，不执行变更。')
        self.show_key_actions(self.timeline(initial), include_errors=True)
        self.answer_summary('读取后的判断', initial)
        corrected = self.turn(session, 'CPU 饱和猜测已被当前采样否定。不能仅凭连接池变更在先就确认因果。'
                              '每次新事故都要重取当前材料，不执行重启。请修正刚才的判断，不调用工具。')
        correction_tools = sum(len(row['events']) for row in self.timeline(corrected))
        self.heading(f'同一会话收到纠正：采样否定 CPU 饱和，配置变化的时间先后不足以确认因果。纠正轮实际新增工具调用 **{correction_tools} 次**。')
        self.answer_summary('纠正后的判断', corrected)
        count = initial['end'] - initial['start'] + corrected['end'] - corrected['start']
        self.record_phase('只读调查', count, time.monotonic() - started)
        self.phase_end(count)
        return session

    def initialize_candidate(self):
        before = self.file_state()
        if self.skill_dir.exists():
            self.heading('候选目录已经存在，原有文件保留。')
        else:
            args = [sys.executable, str(self.creator / 'scripts/init_skill.py'), 'latency-investigation',
                    '--path', str(self.skill_dir.parent),
                    '--interface', 'display_name=支付延迟调查',
                    '--interface', 'short_description=调查支付模块延迟告警，核对当前日志、指标、配置与判断依据',
                    '--interface', 'default_prompt=使用 $latency-investigation 调查当前支付告警']
            result = subprocess.run(args, capture_output=True, text=True)
            self.heading('init_skill 建立了候选文件和界面配置。' if result.returncode == 0 else 'init_skill 未能建立候选文件。')
            if result.returncode:
                self.details('初始化报错原文', result.stdout + result.stderr)
        after = self.file_state()
        self.show_state(before, after)
        self._template_state = after

    def extract(self, session):
        started = time.monotonic()
        self.initialize_candidate()
        task = self.turn(session, self.extraction_input)
        rows = self.timeline(task)
        # 同一次提炼任务按实际首次写入尝试分段；每次请求只计入一个阶段。
        boundary = next((i + 1 for i, row in enumerate(rows) if any(e['kind'] == 'write' for e in row['events'])), len(rows))
        self._extraction_rows = rows
        self._extraction_boundary = boundary
        split_time = self.request_times[task['start'] + boundary] if boundary < len(rows) else task['finished']
        self.record_phase('提炼候选', boundary, split_time - started)
        self._correction_seconds = task['finished'] - split_time
        self.show_key_actions(rows[:boundary], include_errors=True)
        self.show_changes(rows[:boundary])
        wrote = any(e['kind'] == 'write' for row in rows for e in row['events'])
        self.heading('Skill Creator 已参与这次提炼，模型开始尝试修改候选。' if wrote
                     else 'Skill Creator 已参与这次提炼，但本次记录中没有写入尝试。')
        self.phase_end(boundary)
        return task

    def review_extraction(self, task):
        started = time.monotonic()
        rows, boundary = self._extraction_rows, self._extraction_boundary
        validations = [('初始化模板', self._template_state.get('quick_validate'))]
        previous = validations[0][1]
        for row in rows:
            result = row['after'].get('quick_validate')
            if result != previous:
                label = '文件修改后的首次通过' if result and result['exit_code'] == 0 else '文件修改后的校验'
                validations.append((label, result))
                previous = result
        final = task['after'].get('quick_validate')
        self.table(['校验转折', 'quick_validate 实际结果'], [(label, self.validation_text(v)) for label, v in validations])
        # 失败按原因归并；外层命令退出码为 0 也不能掩盖内部补丁错误。
        errors = {}
        for row in rows:
            for event in row['events']:
                if event['error']:
                    group = errors.setdefault(event['error'], {'count': 0, 'first': event['receipt']})
                    group['count'] += 1
        if errors:
            self.table(['写入与校验中的问题', '出现次数'], [(reason, f"{v['count']} 次") for reason, v in errors.items()])
            for reason, values in errors.items():
                self.details(reason + ' · 首次报错原文', values['first'])
        else:
            self.heading('本次工具调用没有返回报错。')
        self.show_key_actions(rows[boundary:])
        self.show_changes(rows[boundary:])
        text = self.skill_path.read_text() if self.skill_path.is_file() else '（本次未生成候选）'
        body = re.sub(r'\A---\n.*?\n---\n', '', text, flags=re.S).strip()
        self.heading('**最终候选（原文节选）**\n\n' + self.short(body, 900))
        self.details('候选 SKILL.md · 完整正文', text)
        self.answer_summary('提炼任务最终回答', task, 450)
        self.heading('最终结构结果：' + self.validation_text(final) + '。格式通过不能证明取证方法可靠；下一阶段看它怎样用于新事故。')
        self.record_phase('校验与纠错', len(rows) - boundary, self._correction_seconds + time.monotonic() - started)
        self.phase_end(len(rows) - boundary)

    def compare_new_cases(self):
        started = time.monotonic()
        plain = self.turn(self.session('plain'), self.new_task)
        applied = self.turn(self.session('candidate'), self.application_input)
        for title, task in [('无方法', plain), ('显式加载候选', applied)]:
            self.heading('**' + title + '：实际读取**')
            self.show_key_actions(self.timeline(task), include_errors=True)
        self.table(['同一新事故', '模型回答（节选）'], [
            ('无方法 · 独立新会话', self.short(plain['result'].final_response, 850)),
            ('显式加载候选 · 独立新会话', self.short(applied['result'].final_response, 850)),
        ])
        self.details('无方法 · 完整回答', plain['result'].final_response)
        self.details('显式加载候选 · 完整回答', applied['result'].final_response)
        self.heading('当前材料只有一组采样，不能据此排除整个告警窗口的间歇性问题。两组回答即使相近，这一次对照也不足以证明方法能稳定提升表现。')
        count = plain['end'] - plain['start'] + applied['end'] - applied['start']
        self.record_phase('新事故对照', count, time.monotonic() - started)
        self.phase_end(count)
        return plain, applied

    def show_biased_control(self):
        started = time.monotonic()
        directory = self.work / 'bad-structure-control'
        directory.mkdir(exist_ok=True)
        path = directory / 'SKILL.md'
        path.write_text('---\nname: old-payment-answer\ndescription: 调查支付告警时使用。\n---\n'
                        '# 支付调查\n任何支付延迟都先认定连接池上限被调低。\n', encoding='utf-8')
        result = self.validate(directory)
        self.table(['对照文件的旧结论', 'quick_validate 实际结果'], [
            ('任何支付延迟都先认定连接池上限被调低。', self.validation_text(result)),
        ])
        self.details('bad-structure · 完整文件', path.read_text())
        self.heading('结构检查通过，正文却预设了旧答案。格式合格仍然可能保留方法偏见。' if result['exit_code'] == 0
                     else '这份文件未通过结构检查；正文还预设了旧答案。结构与方法需要分别判断。')
        self.record_phase('夹带旧结论对照', 0, time.monotonic() - started)
        self.phase_end(0)

    def close(self):
        started = time.monotonic()
        self.lab.close()
        self.record_phase('结束与回看', 0, time.monotonic() - started)
        self.heading('候选与调查报告保留在本次临时项目中。这次试用留下了可回看的材料，方法是否可靠仍要依据候选和报告判断。')
        self.details('连接状态', '本次模型客户端与本地适配器已关闭；候选未安装到个人目录。')
        self.phase_end(0)
