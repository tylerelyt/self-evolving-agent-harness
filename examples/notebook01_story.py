"""01 讲的材料、过程表格与留痕；模型调用及回执分析在 notebook 中执行。"""
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import time


class AgentLoopStory:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.offline = bool(os.environ.get("LAB_VALIDATION_RUN_ID"))
        self.lab = None
        self.closed = False
        self.metrics = []
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
            self.inputs = self.work / "inputs"
            self.report_path = self.work / "outputs/incident-report.md"
            self.report_path.parent.mkdir()
            self.loop_marker = "LOOP-" + secrets.token_hex(8)
            for source, target in (("alert.md", "alert.md"), ("payment-api.log", "logs/payment-api.log"),
                                   ("checkout.py", "src/checkout.py")):
                path = self.inputs / target
                path.parent.mkdir(parents=True, exist_ok=True)
                text = (self.materials / source).read_text(encoding="utf-8")
                path.write_text(text.replace("{{LOOP_MARKER}}", self.loop_marker), encoding="utf-8")
            self.input_snapshot = self.snapshot(self.inputs)
            self.input_text = {str(p.relative_to(self.inputs)): p.read_text(encoding="utf-8")
                               for p in sorted(self.inputs.rglob("*")) if p.is_file()}
            self._save("inputs-before.json", self.input_snapshot)
            self._save("run.json", {"mode": "material-replay" if self.offline else "live",
                                    "model": None if self.offline else "glm-5.2", "loop_marker": self.loop_marker})
            self._phase_start = time.monotonic()
            self._phase_requests = 0
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

    def _phase(self, name):
        count = len(self.lab.requests) if self.lab else 0
        self.metrics.append({"act": name, "seconds": round(time.monotonic() - self._phase_start, 3),
                             "requests": count - self._phase_requests,
                             "turns_total": len(self.lab.results) if self.lab else 0})
        self._save("acts.json", self.metrics)
        self._phase_start, self._phase_requests = time.monotonic(), count

    def replay_report(self):
        """离线只依据案例材料写示例报告，不制造模型请求、命令事件或结束状态。"""
        if not self.offline:
            raise RuntimeError("材料回放只用于离线路径。")
        log = (self.inputs / "logs/payment-api.log").read_text(encoding="utf-8").splitlines()
        code = (self.inputs / "src/checkout.py").read_text(encoding="utf-8").splitlines()
        lines = ["# 构造材料回放报告", "", "本报告由离线脚本整理，不是本次模型调查结果。", "",
                 "已确认事实：", *[f"- inputs/logs/payment-api.log:{i}：`{line}`"
                                  for i, line in enumerate(log, 1) if "req-7f3a" in line],
                 *[f"- inputs/src/checkout.py:{i}：`{line.strip()}`"
                   for i, line in enumerate(code, 1) if 'payload["currency"]' in line], "",
                 "当前推断：请求数据缺少 currency，代码直接访问该字段，触发 KeyError 并返回 500。", "",
                 "待验证：字段为什么缺失、影响范围有多大、是否涉及上游或中间服务，现有材料不足以确定。", "",
                 "下一步：沿请求编号核对上游与中间服务记录，验证字段传递及缺失字段时的处理。",
                 "本练习中的实例名、时间、数值及编号均为构造值。"]
        self.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def receipt_request(requests, call_id):
        return next((i for i, request in enumerate(requests, 1)
                     if any(isinstance(item, dict) and item.get("type") == "function_call_output"
                            and item.get("call_id") == call_id for item in request.get("input", []))), None)

    def show_creation(self, result, requests):
        from notebook_support import command_receipts
        rows = []
        if self.offline:
            rows = [(f"inputs/{name}\n{text}", "离线回放，没有模型读取", "没有后续模型请求")
                    for name, text in self.input_text.items()]
        else:
            receipts = command_receipts(requests)
            seen = set()
            for receipt in receipts:
                first = self.receipt_request(requests, receipt["call_id"])
                later = []
                if first:
                    for item in requests[first - 1].get("input", []):
                        if isinstance(item, dict) and item.get("call_id"):
                            seen.add(item["call_id"])
                    for request in requests[first:]:
                        later = [item for item in request.get("input", []) if isinstance(item, dict)
                                 and item.get("type") == "function_call" and item.get("call_id") not in seen]
                        if later:
                            break
                following = "\n".join(item.get("arguments", "") for item in later)
                rows.append(("告警给请求编号；日志给请求数据与异常堆栈；代码给字段访问位置（均为构造值）",
                             f"{receipt['command']}\n退出码：{receipt['exit_code']}\n{receipt['output']}",
                             following or "没有捕获到后续命令；以结束状态和报告核对"))
            if not rows:
                rows = [("inputs/ 中的三份构造材料", "未捕获配对命令回执", "请查看本次请求和工具事件")]
        self._save("creation.json", {"mode": "material-replay" if self.offline else "live", "rows": rows,
                                     "status": result.status.value if result else None})
        self._table(["材料里有什么", "模型这一轮实际读了什么", "下一轮接着做什么"], rows,
                    "展示给读者的文件不会自动进入模型请求。表中读取内容来自本次配对回执；模型可能一次合并读取多个文件，以实际命令为准。"
                    if self.lab else "离线仅展示构造材料与示例报告，不证明模型读过文件或真实运行过调查循环。")
        self._phase("第一幕")

    def show_receipts(self, receipts, requests, marker_requests):
        rows = [(r["call_id"], r["command"], r["exit_code"], self.receipt_request(requests, r["call_id"]))
                for r in receipts]
        self._save("receipts.json", {"receipts": receipts, "rows": rows, "marker_requests": marker_requests,
                                    "marker_first_request": next((i for i, entered in marker_requests if entered), None)})
        self._table(["调用编号", "真实命令", "退出码", "回执首次进入的请求序号"],
                    rows or [("离线回放", "没有模型提出的命令", "不适用", "不适用")],
                    "同一个 call_id 配对动作与回执；退出码来自环境返回内容，历史回执重复携带时只计一次。")
        self._table(["请求序号", "日志随机编号是否已进入工具结果"],
                    [(i, "是" if entered else "否") for i, entered in marker_requests]
                    or [("离线回放", "没有模型请求，不能核验进入时机")],
                    "只检查 function_call_output 中的随机编号，不把用户提示或模型复述算作读取。"
                    "请求记录能说明材料进入调用的时机，不能展示模型内部推理。")
        self._phase("第二幕")

    def show_report(self, result, after):
        try:
            changed = sorted(name for name in self.input_snapshot.keys() | after.keys()
                             if self.input_snapshot.get(name) != after.get(name))
            present = self.report_path.is_file() and bool(self.report_path.read_text(encoding="utf-8").strip())
            status = result.status.value if result else None
            self._save("inputs-after.json", after)
            self._save("report-checks.json", {"report_nonempty": present, "inputs_unchanged": not changed,
                                              "changed_inputs": changed, "status": status})
            self._table(["本次产物", "是否发生", "说明"], [
                ("调查结束状态", status or "离线没有调查 Turn", "completed 只说明本轮执行结束，不证明归因正确。"),
                ("outputs/incident-report.md", "已生成且非空" if present else "未确认非空报告", "文件存在说明写入发生；日志与代码引用、事实与推断仍要逐条核对。"),
                ("inputs/ 前后比较", "未变" if not changed else "有变化：" + "、".join(changed), "本次未改输入只证明本次行为守了边界，不证明文字要求已经成为强制权限。"),
            ], "本阶段解读：随机编号支持读取链路，报告归因仍须对回日志和代码。请求缺少 currency 与直接访问该字段支持异常机制，"
               "却不能证明上游发布了错误版本。目录级隔离与审批将在后面的讲次落实。")
            self._phase("第三幕")
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        if self._ipython:
            self._ipython.events.unregister("post_run_cell", self._on_cell_finished)
            self._ipython = None
        try:
            if self.lab:
                self.lab.close()
            self.closed = True
        finally:
            self._save("cleanup.json", {"closed": self.closed,
                        "proxy_returncode": self.lab.proxy.poll() if self.lab and self.lab.proxy else None,
                        "errors": self.lab._cleanup_errors if self.lab else []})
