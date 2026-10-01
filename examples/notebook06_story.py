"""06 讲的构造材料、过程表格与记录；会话及分岔动作在 notebook 中执行。"""
import atexit
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time

STAGES = ["Agent 为什么一直在排查另一套系统？",
          "Fork 怎样为新的事故建立独立的 Thread？",
          "新 Thread 怎样接回模块背景，重新调查当前告警？"]
ENTRIES = ["legacy_release_history", "legacy_payment_logs",
           "legacy_payment_db_pool", "legacy_payment_runtime_config"]


class ThreadForkStory:
    def __init__(self, directory, mode):
        self.directory = directory.resolve()
        self.offline = mode == "offline"
        self.lab = self.fork_client = self._ipython = None
        self.closed = False
        self.metrics = []
        self.runtime = None
        self.background_marker = "BACKGROUND-" + secrets.token_hex(6)
        self.current_marker = "CURRENT-" + secrets.token_hex(6)
        try:
            if self.offline:
                run_id = os.environ.get("LAB_VALIDATION_RUN_ID") or "nb06-v1-offline-" + secrets.token_hex(8)
                if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", run_id):
                    raise ValueError("本次运行编号不合法。")
                self.runtime = self.directory / ".runtime" / run_id
                self.runtime.mkdir(parents=True, exist_ok=False)
                self.work = self.runtime / "work"
                self.work.mkdir()
            else:
                from notebook_support import Lab
                from openai_codex.client import CodexClient
                self.lab = Lab(self.directory)
                self.runtime, self.work = self.lab.runtime, self.lab.work
                self.fork_client = self.lab.manage_client(CodexClient(config=self.lab.config))
                self.fork_client.start()
                self.fork_client.initialize()
            self.materials = self.directory / "materials"
            self.platform = self.work / "new-platform"
            self.legacy = self.work / "legacy-payment"
            for project in (self.platform, self.legacy):
                shutil.copytree(self.materials / project.name, project)
                (project / ".git").mkdir()
                shutil.copyfile(self.materials / "query.py", project / "query.py")
            current = self.legacy / "current.json"
            current.write_text(current.read_text(encoding="utf-8").replace("{{CURRENT_MARKER}}", self.current_marker), encoding="utf-8")
            self.platform_context = (self.platform / "background.md").read_text(encoding="utf-8")
            self.alert = (self.legacy / "alert.md").read_text(encoding="utf-8")
            self.old_turns = json.loads((self.materials / "old-thread.json").read_text(encoding="utf-8")
                                      .replace("{{BACKGROUND_MARKER}}", self.background_marker))
            self.input_snapshot = self.snapshot(self.work)
            self._save("inputs-before.json", self.input_snapshot)
            self._save("run.json", {"mode": "material-replay" if self.offline else "live",
                                    "model": None if self.offline else "glm-5.2",
                                    "background_marker": self.background_marker, "current_marker": self.current_marker})
            self._phase_start, self._phase_requests = time.monotonic(), 0
            from IPython import get_ipython
            self._ipython = get_ipython()
            if self._ipython:
                self._ipython.events.register("post_run_cell", self._on_cell_finished)
            atexit.register(self.close)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def snapshot(folder):
        return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}

    def _save(self, name, value):
        if self.lab:
            self.lab._write_json(name, value)
        elif self.runtime:
            (self.runtime / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _table(self, headers, rows, note):
        from IPython.display import HTML, display
        def clean(value):
            return escape(self.lab.clean(value) if self.lab else str(value))
        head = "".join("<th style='text-align:left;font-size:14px'>" + clean(v) + "</th>" for v in headers)
        body = "".join("<tr>" + "".join("<td style='text-align:left;font-size:14px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top'>"
                       + clean(v) + "</td>" for v in row) + "</tr>" for row in rows)
        display(HTML("<div class='act-result'><table style='width:100%;table-layout:fixed'><thead><tr>" + head
                     + "</tr></thead><tbody>" + body + "</tbody></table><p>" + clean(note) + "</p></div>"))

    def _phase(self, title):
        count = len(self.lab.requests) if self.lab else 0
        self.metrics.append({"stage": title, "seconds": round(time.monotonic() - self._phase_start, 3),
                             "requests": count - self._phase_requests,
                             "turns_total": len(self.lab.results) if self.lab else 0})
        self._save("stages.json", self.metrics)
        self._phase_start, self._phase_requests = time.monotonic(), count

    def _on_cell_finished(self, result):
        if result.error_before_exec or result.error_in_exec:
            self.close()

    def probe(self, *args):
        result = subprocess.run([sys.executable, "-B", "query.py", *args], cwd=self.legacy,
                                capture_output=True, text=True, timeout=10)
        return {"entry": args[0], "exit_code": result.returncode,
                "output": result.stdout, "stderr": result.stderr, "source": "Python 材料查询"}

    def replay_wrong(self):
        probes = [self.probe(entry) for entry in ("platform_release_history", "platform_mesh_metrics")]
        self._save("offline-wrong-entry-probes.json", probes)
        return probes

    @staticmethod
    def events(result):
        return [item.root.model_dump(mode="json", by_alias=True) for item in result.items] if result else []

    def show_wrong(self, result, requests, probes):
        receipts = []
        if self.lab:
            from notebook_support import command_receipts
            receipts = command_receipts(requests)
        attempted = [e for e in self.events(result) if e.get("type") == "commandExecution"]
        old_direction = any(any(name in e.get("command", "") for name in ("platform_release_history", "platform_mesh_metrics")) for e in attempted)
        answer = result.final_response if result else None
        self._save("wrong-thread.json", {"mode": "material-replay" if self.offline else "live",
            "answer": answer, "commands": attempted, "receipts": receipts,
            "checks": {"沿用新平台入口": old_direction if self.lab else None,
                       "新平台背景仍在告警请求": all(line in json.dumps(requests[0], ensure_ascii=False)
                           for line in self.platform_context.splitlines() if line.strip()) if requests else None,
                       "用新平台假设解释入口缺失": any(word in answer for word in ("Canary", "灰度", "服务网格", "Sidecar"))
                           and any(word in answer for word in ("上报", "采集", "缺失", "缺少", "不完整")) if answer else None}})
        rows = [("会话上下文归属", self.platform_context, "这段背景属于新支付平台"),
                ("当前工作目录", "legacy-payment/", "切换目录不清除既有会话历史")]
        if self.lab:
            rows.extend(("实际命令", e.get("command", ""), e.get("aggregatedOutput", "")) for e in attempted)
            rows.append(("实际回答", answer, "是否继续用灰度和服务网格解释告警，以本次轨迹为准"))
        else:
            rows.extend(("Python 查询旧入口", p["entry"], p["stderr"].strip()) for p in probes)
            rows.append(("模型调查", "未执行", "没有模型回答、请求或工具回执"))
        self._table(["观察对象", "本次内容", "对判断的影响"], rows,
            "会话历史决定背景归属；litellm 代理层转发请求，不替 Agent 划定任务边界。"
            + ("离线查询只说明两套目录入口不同，不能证明模型串场。" if self.offline else ""))
        self._phase(STAGES[0])

    def show_history(self, parent_before, turns):
        self._save("parent-before.json", parent_before)
        self._save("old-turns.json", [{"material_turn": row["turn"], "turn_id": turn.id}
                                       for row, turn in turns] if turns else [])
        self._table(["旧记录位置", "记录内容", "能否成为新任务起点"],
            [("Turn " + str(row["turn"]) + (" · 已结束" if turns else " · 构造材料"), row["text"],
              "包含这一轮；入口仍须重查" if row["turn"] == 1 else "留在旧事故时间线") for row in self.old_turns],
            "边界应落在只建立模块背景、还没有进入事故判断的位置。背景和结论若同在一轮，会一同继承。")

    def show_fork(self, parent, branch, parent_before, branch_before, boundary, selected):
        checks = {}
        if self.lab:
            old_ids = [row["turn_id"] for row in json.loads((self.runtime / "old-turns.json").read_text())]
            inherited_ids = [t["id"] for t in branch_before["thread"]["turns"]]
            text = json.dumps(branch_before, ensure_ascii=False)
            checks = {"新Thread有独立编号": branch.id != parent.id,
                      "来源指向父Thread": branch_before["thread"].get("forkedFromId") == parent.id,
                      "指定轮包含在继承前缀": old_ids[0] in inherited_ids,
                      "之后的旧事故轮不继承": not any(i in inherited_ids for i in old_ids[1:]),
                      "背景在继承内容中": self.background_marker in text,
                      "旧事故观察与结论不在继承内容中": not any(row["text"] in text for row in self.old_turns[1:]),
                      "新分支使用当前目录": Path(branch_before["thread"]["cwd"]).resolve() == self.legacy}
        self._save("branch-before.json", branch_before)
        self._save("fork-boundary.json", {"mode": "material-replay" if self.offline else "live",
            "material_boundary": boundary, "selected_prefix": selected,
            "parent_id": parent.id if parent else None, "branch_id": branch.id if branch else None, "checks": checks})
        rows = [("选择的位置", "Turn " + str(boundary), "包含边界；此轮及以前的历史可继承"),
                ("保留的前缀", "\n".join(r["text"] for r in selected), "只建立模块背景"),
                ("排除的后续历史", "\n".join(r["text"] for r in self.old_turns[boundary:]), "旧事故观察、结论与处置留在父记录")]
        rows.extend((name, str(value), "真实 thread/read 核对") for name, value in checks.items())
        if self.offline:
            rows.append(("真实分支", "未创建", "Python 仅对构造记录切片，没有模拟 Thread ID"))
        self._table(["历史边界", "本次核对", "机制含义"], rows,
            "引用式继承可共享选定的历史前缀，新 Thread 再写自己的后续记录；接口核对能确认范围与来源，不能证明存储层实际复制了多少数据。")
        self._phase(STAGES[1])

    def replay_current(self):
        reads = [self.probe("--list"), *[self.probe(entry) for entry in ENTRIES],
                 self.probe("legacy_payment_logs", "--request-id", "pay-current-001")]
        code = (self.legacy / "retry.py").read_text(encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", "replay_retry.py"], cwd=self.legacy,
                                capture_output=True, text=True, timeout=10)
        record = {"reads": reads, "code": code, "replay": {"exit_code": result.returncode, "output": result.stdout, "stderr": result.stderr}}
        self._save("offline-current-reads.json", record)
        return record

    @staticmethod
    def queried(requests, entry, marker):
        from notebook_support import command_receipts
        return any("query.py" in r["command"] and entry in r["command"] and r["exit_code"] == 0
                   and marker in r["output"] for r in command_receipts(requests))

    def show_current(self, result, requests, replay, parent, branch, parent_before, after):
        try:
            data = json.loads((self.legacy / "current.json").read_text(encoding="utf-8"))
            checks = {}
            if self.lab:
                from notebook_support import command_receipts
                receipts = command_receipts(requests)
                parent_after = parent.read(include_turns=True).model_dump(mode="json", by_alias=True)
                branch_after = branch.read(include_turns=True).model_dump(mode="json", by_alias=True)
                self._save("parent-after.json", parent_after)
                self._save("branch-after.json", branch_after)
                first = json.dumps(requests[0], ensure_ascii=False) if requests else ""
                checks = {"首次请求带模块背景": self.background_marker in first,
                          "首次请求不含旧事故现场": not any(row["text"] in first for row in self.old_turns[1:]),
                          "今天材料没有预先写入首请求": self.current_marker not in first,
                          "重新确认当前入口": any("query.py" in r["command"] and "--list" in r["command"] and r["exit_code"] == 0
                                                and all(e in r["output"] for e in ENTRIES) for r in receipts),
                          "父时间线未因新调查改变": parent_before["thread"]["turns"] == parent_after["thread"]["turns"],
                          "新调查追加在新时间线": len(branch_after["thread"]["turns"]) == 2,
                          "形成当前调查": bool(result.final_response.strip()) and "Idempotency-Key" in result.final_response
                                               and "800" in result.final_response and "2.8" in result.final_response,
                          "读取新代码": any("retry.py" in r["command"] and r["exit_code"] == 0 and "pool.acquire()" in r["output"] for r in receipts),
                          "隔离重放当前路径": any("replay_retry.py" in r["command"] and r["exit_code"] == 0 and '"waiting": 4' in r["output"] for r in receipts)}
                checks.update({"重新读取" + e: self.queried(requests, e, data[e]["observation_id"]) for e in ENTRIES})
                (self.runtime / "new-alert-report.md").write_text(self.lab.clean(result.final_response), encoding="utf-8")
            else:
                checks = {"Python重新列出四个入口": replay["reads"][0]["exit_code"] == 0 and all(e in replay["reads"][0]["output"] for e in ENTRIES),
                          "Python查询均成功": all(r["exit_code"] == 0 for r in replay["reads"]),
                          "内存重放显示连接未归还": replay["replay"]["exit_code"] == 0 and json.loads(replay["replay"]["output"])["active"] == 8
                                                     and json.loads(replay["replay"]["output"])["waiting"] == 4}
            checks["材料没有改变"] = after == self.input_snapshot
            self._save("current-investigation.json", {"mode": "material-replay" if self.offline else "live", "checks": checks,
                "model_report": result.final_response if result else None, "inputs_after": after})
            status = "Python 材料查询" if self.offline else "当前模型调查"
            rows = [("当前入口 · " + status, "\n".join(ENTRIES), "继承只用来认门，入口仍须重新确认"),
                    ("当前发布与日志", json.dumps({e: data[e] for e in ENTRIES[:2]}, ensure_ascii=False), "第一条错误晚于新版本启动；旧版实例正常，调查转向版本差异"),
                    ("当前指标与配置", json.dumps({e: data[e] for e in ENTRIES[2:]}, ensure_ascii=False), "上限仍是构造值 800，没有调低；等待异常不能直接解释为旧配置问题"),
                    ("代码与隔离重放", replay["replay"]["output"] if self.offline else result.final_response,
                     "缺少 Idempotency-Key 时校验提前退出，已取得的连接没有归还；内存重放支持此机制，真实事故仍需独立核实")]
            rows.extend((name, str(value), "本次实际核对") for name, value in checks.items())
            self._table(["新时间线", "本次读取与调查", "怎样改变判断"], rows,
                "三项任务各存自己的目标、观察、判断与待办。"
                + ("离线没有模型调查或真实 Fork；显示的是 Python 读取的构造材料及内存重放。" if self.offline else "模型报告与成功工具回执单独保存，可相互核对。"))
        finally:
            self._phase(STAGES[2])
            self.close()

    def close(self):
        if self.closed:
            return
        if self._ipython:
            self._ipython.events.unregister("post_run_cell", self._on_cell_finished)
            self._ipython = None
        processes, ports = {}, []
        if self.lab:
            import socket
            for name, client in (("model", getattr(self.lab.codex, "_client", None)), ("fork", self.fork_client)):
                processes[name] = getattr(client, "_proc", None)
            if self.lab.proxy:
                processes["proxy"] = self.lab.proxy
                ports.append(int(self.lab.proxy.args[self.lab.proxy.args.index("--port") + 1]))
            if self.lab.observer:
                ports.append(self.lab.observer.server_address[1])
            self.lab.close()
            opened = {}
            for port in ports:
                with socket.socket() as sock:
                    sock.settimeout(1)
                    opened[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
        else:
            opened = {}
        self.closed = True
        atexit.unregister(self.close)
        self._save("cleanup.json", {"closed": True, "mode": "material-replay" if self.offline else "live",
            "started_connections": bool(self.lab), "process_returncodes": {name: proc.poll() if proc else None for name, proc in processes.items()},
            "ports_open": opened, "errors": self.lab._cleanup_errors if self.lab else []})
