"""02 讲的构造材料、过程表格与留痕；控制接口由 notebook 直接调用。"""
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).parent / "02-instruction-control"))
from control_validation import TurnTrace, successful_reads


class InstructionControlStory:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.offline = bool(os.environ.get("LAB_VALIDATION_RUN_ID"))
        self.lab = None
        self.closed = False
        self.metrics, self.traces = [], []
        self._ipython = None
        if self.offline:
            run_id = os.environ["LAB_VALIDATION_RUN_ID"]
            if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", run_id):
                raise ValueError("本次运行编号不合法。")
            self.runtime = self.directory / ".runtime" / run_id
            self.runtime.mkdir(parents=True, exist_ok=False)
            self.work = self.runtime / "work"
            self.work.mkdir()
        else:
            from notebook_support import Lab
            self.lab = Lab(self.directory)
            self.runtime, self.work = self.lab.runtime, self.lab.work
        try:
            self.materials = self.directory / "materials"
            self.files = {}
            for source in sorted(self.materials.rglob("*")):
                if not source.is_file():
                    continue
                name = str(source.relative_to(self.materials))
                text = source.read_text(encoding="utf-8")
                if name.startswith("servers/"):
                    text += "\n材料编号：OBS-" + secrets.token_hex(12) + "\n"
                target = self.work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                self.files[name] = text
            self.first_paths = {"servers/server-17/deployment.md", "servers/server-17/metrics.md"}
            self.baseline = self.snapshot(self.work / "servers")
            self.scripts_before = self.script_snapshot()
            self.alert_tag = "CONTROL-" + secrets.token_hex(8)
            self.deployment_marker = self.files["servers/server-17/deployment.md"].split("材料编号：", 1)[1].strip()
            self.patrol_marker = self.work / "tools/.patrol-probe-started"
            self.interrupt_marker = self.work / "tools/.interrupt-probe-started"
            self.patrol_task = (
                "巡检 servers/ 下两台构造服务器，先 server-17，再 server-31。每台有 deployment.md 和 metrics.md。"
                "用独立 cat 命令读取该台两份材料，不使用管道或脚本读取，不提前读取另一台。"
                "读完 server-17 后运行 tools/patrol_probe.py，等待采集结束。"
                "把巡检结论写入 patrol-report.md，一台一段。不要修改 servers/ 或 tools/，不要变更服务；"
                "不查询工作目录外的文件或网络。实例名、时间与数值均为构造值。")
            self.alert = (
                "【实时告警 02:07（构造值）】server-17 的 payment-api P95 延迟超过 3 秒，5xx 错误率升至 8.4%。"
                "请在当前任务中优先调查，结合已掌握的部署与变更情况，把结论和文件依据写入 incident-report.md。"
                "区分事实、推断与待验证问题；使用已取得的材料，不重复读取 server-17 文件，不用 cat 回显报告。"
                "调查后继续完成 server-31 的巡检，写入 patrol-report.md。\n本次告警投递编号：" + self.alert_tag)
            self.interrupt_task = (
                "运行 tools/interrupt_probe.py，等它完整结束后把结果写入 interrupt-report.md；不要修改其他文件。"
                "只使用本次工作目录，不访问外部服务。")
            self._save("inputs-before.json", self.baseline)
            self._save("scripts-before.json", self.scripts_before)
            self._save("run.json", {"mode": "material-replay" if self.offline else "live",
                                    "model": None if self.offline else "glm-5.2",
                                    "alert_tag": self.alert_tag, "deployment_marker": self.deployment_marker})
            self._phase_start, self._phase_requests = time.monotonic(), 0
            from IPython import get_ipython
            self._ipython = get_ipython()
            if self._ipython:
                self._ipython.events.register("post_run_cell", self._on_cell_finished)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def snapshot(folder):
        return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(folder.rglob("*")) if p.is_file()}

    def script_snapshot(self):
        return {name: hashlib.sha256((self.work / name).read_bytes()).hexdigest()
                for name in self.files if name.startswith("tools/")}

    def _save(self, name, value):
        if self.lab:
            self.lab._write_json(name, value)
        else:
            (self.runtime / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _table(self, headers, rows, note):
        from IPython.display import HTML, display
        def clean(value):
            return escape(self.lab.clean(value) if self.lab else str(value))
        head = "".join("<th style='text-align:left;font-size:14px'>" + clean(x) + "</th>" for x in headers)
        body = "".join("<tr>" + "".join("<td style='text-align:left;font-size:14px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top'>"
                       + clean(x) + "</td>" for x in row) + "</tr>" for row in rows)
        display(HTML("<div class='act-result'><table style='width:100%;table-layout:fixed'><thead><tr>" + head
                     + "</tr></thead><tbody>" + body + "</tbody></table><p>" + clean(note) + "</p></div>"))

    def _on_cell_finished(self, result):
        if result.error_before_exec or result.error_in_exec:
            self.close()

    def track(self, trace):
        self.traces.append(trace)
        return trace

    def _phase(self, name):
        count = len(self.lab.requests) if self.lab else 0
        self.metrics.append({"act": name, "seconds": round(time.monotonic() - self._phase_start, 3),
                             "requests": count - self._phase_requests,
                             "turns_total": len(self.lab.results) if self.lab else 0})
        self._save("acts.json", self.metrics)
        self._phase_start, self._phase_requests = time.monotonic(), count

    def probe_state(self, marker):
        """单独查进程；运行终态不代替工具进程状态。"""
        if not marker.is_file():
            return {"marker_present": False, "running": False, "pid": None}
        data = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        try:
            os.kill(pid, 0)
            running = True
        except ProcessLookupError:
            running = False
        except PermissionError:
            running = None
        return {"marker_present": True, "running": running, "pid": pid,
                "method": "对本次探针记录的进程号执行零信号查询；不改变进程状态"}

    def show_window(self, trace):
        events = trace.snapshot() if trace else []
        reads = successful_reads(events, self.work, self.files)
        probe = self.probe_state(self.patrol_marker) if self.lab else None
        self.window = {"events_before_steer": len(events), "reads": reads, "probe": probe,
                       "mode": "material-replay" if self.offline else "live"}
        self._save("patrol-window.json", self.window)
        rows = []
        for server, role in (("server-17", "当前检查 payment-api；告警随后交给这项任务"),
                             ("server-31", "等待后续检查 order-api")):
            names = [p for p in sorted(self.files) if p.startswith("servers/" + server + "/")]
            if self.offline:
                text = "\n\n".join(p + "\n" + self.files[p] for p in names)
                state = "离线仅展示材料；没有模型读取或采集"
            else:
                text = "\n\n".join(p + "\n" + self.files[p] for p in names
                                      if p in {r["path"] for r in reads}) or "尚未取得读取回执"
                state = ("启动标记已写入，进程正在运行" if probe["running"] is True else "未确认仍在运行") if server == "server-17" else "尚未启动采集"
            rows.append((server + "（构造实例）", role, text, state))
        self._table(["对象", "本次角色", "已读材料", "探针状态"], rows,
                    "两份成功读取回执与慢探针启动标记共同确定投递窗口。独立 cat 与约 25 秒探针是本练习的观察条件；不是通用巡检要求。"
                    if self.lab else "离线只回放构造材料，不证明模型读过文件；探针未运行，也没有活跃运行可供转向。")
        self._phase("第一幕")

    def record_turn(self, name, trace, session):
        events = trace.snapshot() if trace else []
        self._save(name + "-events.json", events)
        if trace:
            record = {"thread_id": session.id, "turn_id": trace.turn.id, "status": trace.status,
                      "items": events}
            self.lab.results.append({k: record[k] for k in ("thread_id", "turn_id", "status")})
            self._save(f"turn-{len(self.lab.results):03}.json", record)

    def show_patrol(self, trace, response, calls, requests_before_steer, first_alert_index, alert_request):
        from notebook_support import tool_output_contains
        events = trace.snapshot() if trace else []
        before = self.window["events_before_steer"]
        reads = successful_reads(events, self.work, self.files)
        facts = {
            "same_turn": response.turn_id == trace.turn.id if trace else None,
            "turn_id": trace.turn.id if trace else None,
            "steer_turn_id": response.turn_id if response else None,
            "reads_before_alert": sorted({r["path"] for r in self.window["reads"]}),
            "first_alert_request": first_alert_index + 1 if first_alert_index is not None else None,
            "requests_before_steer": requests_before_steer,
            "pre_alert_requests_clean": all(self.alert_tag not in json.dumps(r, ensure_ascii=False)
                                             for r in calls[:requests_before_steer]) if self.lab else None,
            "deployment_receipt_preserved": tool_output_contains(alert_request, self.deployment_marker) if self.lab else None,
            "status": trace.status if trace else None, "reads": reads,
            "patrol_probe_running_before_alert": bool(self.window["probe"]["running"]) if self.lab else None,
            "server17_rereads_after_alert": [r for r in reads if r["completed"] >= before and r["path"] in self.first_paths],
            "server31_reads_after_alert": sorted({r["path"] for r in reads if r["started"] >= before and "server-31" in r["path"]}),
        }
        for name in ("incident-report.md", "patrol-report.md"):
            path = self.work / name
            facts[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
        self._save("patrol-observations.json", facts)
        rows = []
        submitted = False
        for event in events:
            if event["seq"] >= before and not submitted:
                rows.append(("告警提交", self.alert, "", ""))
                submitted = True
            if event["method"] != "item/completed":
                continue
            item = event["item"]
            if item.get("type") == "commandExecution":
                rows.append(("告警前" if event["seq"] < before else "告警后", item.get("command", ""), "", ""))
            elif item.get("type") == "agentMessage":
                rows.append(("本次判断", item.get("text", ""), "", ""))
        if self.offline:
            rows.append(("材料回放", "没有提交告警或模型请求", "", ""))
        elif not submitted:
            rows.append(("告警提交", self.alert, "", ""))
        observations = [("告警是否进入原运行", facts["same_turn"]),
                        ("告警前已读材料", "\n".join(facts["reads_before_alert"]) or "无"),
                        ("投递前已捕获请求数", requests_before_steer),
                        ("告警首次进入的请求序号", facts["first_alert_request"]),
                        ("告警所在请求是否仍带部署回执", facts["deployment_receipt_preserved"]),
                        ("巡检运行终态", facts["status"])]
        rows.extend(("", "", name, "离线未执行，不适用" if self.offline else str(value)) for name, value in observations)
        for name in ("incident-report.md", "patrol-report.md"):
            rows.append(("报告文件", name, "文件实际内容", facts[name] or "本次未生成"))
        self._table(["时间线", "动作", "观察项", "本次结果"], rows,
                    "接口返回同一运行编号只证明输入投递；随机告警编号首次进入的请求及同一请求中的部署工具回执，才说明新旧信息一起进入后续判断。"
                    "报告按本次原文展示，根因仍须对回材料，不把固定请求序号当成普遍保证。")
        self._phase("第二幕")

    def capture_interrupt_window(self, trace):
        data = {"events": trace.snapshot() if trace else [],
                "probe": self.probe_state(self.interrupt_marker) if trace else None}
        self._save("interrupt-window.json", data)

    def show_interrupt(self, trace):
        try:
            after, scripts = self.snapshot(self.work / "servers"), self.script_snapshot()
            probe = self.probe_state(self.interrupt_marker) if self.lab else None
            facts = {"status": trace.status if trace else None, "probe": probe,
                     "marker_present": self.interrupt_marker.is_file(),
                     "report_present": (self.work / "interrupt-report.md").exists(),
                     "inputs_unchanged": after == self.baseline, "scripts_unchanged": scripts == self.scripts_before}
            self._save("interrupt-observations.json", facts)
            self._save("inputs-after.json", after)
            self._save("scripts-after.json", scripts)
            rows = [("运行终态", facts["status"] or "离线未运行，无中断终态"),
                    ("中断前启动标记", "仍在" if facts["marker_present"] else "未生成"),
                    ("完整结束后才应写的报告", "已生成" if facts["report_present"] else "未生成"),
                    ("业务材料", "未变" if facts["inputs_unchanged"] else "发生变化"),
                    ("探针脚本", "未变" if facts["scripts_unchanged"] else "发生变化"),
                    ("探针进程单独检查", "离线未启动" if probe is None else
                     "状态未知：没有查询权限" if probe["running"] is None else "仍在运行" if probe["running"] else "已退出")]
            self._table(["观察对象", "中断后实际情况"], rows,
                        "steer 改的是接下来怎样继续，interrupt 结束当前执行；副作用不自动回滚。"
                        "本例探针只写本地启动标记，不操作业务服务。运行终态、已有文件和工具进程分别核查；离线没有中断行为。")
            self._phase("第三幕")
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        errors = []
        if self._ipython:
            self._ipython.events.unregister("post_run_cell", self._on_cell_finished)
            self._ipython = None
        try:
            for index, trace in enumerate(self.traces):
                try:
                    trace.abort()
                except Exception as exc:
                    errors.append(str(exc))
                self._save(("patrol" if index == 0 else "interrupt") + "-events.json", trace.snapshot())
            if self.lab:
                self.lab.close()
            # 关闭连接后另查本次探针，不拿运行终态断言进程退出。
            probes = {}
            if self.lab and hasattr(self, "patrol_marker"):
                deadline = time.monotonic() + 45
                while True:
                    probes = {name: self.probe_state(marker) for name, marker in
                              (("patrol", self.patrol_marker), ("interrupt", self.interrupt_marker))}
                    if all(p["running"] is False for p in probes.values()) or time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)
                if not all(p["running"] is False for p in probes.values()):
                    errors.append("关闭后仍有本次探针进程运行或无法确认其状态")
            self.closed = not errors
        finally:
            self._save("cleanup.json", {"closed": self.closed, "probes": locals().get("probes", {}),
                       "proxy_returncode": self.lab.proxy.poll() if self.lab and self.lab.proxy else None,
                       "errors": errors + (self.lab._cleanup_errors if self.lab else [])})
        if errors:
            raise RuntimeError("本次清理未完成：" + "；".join(errors))
