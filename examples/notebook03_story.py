"""03 讲的构造材料、请求展示与留痕；模型调用和回执分析由 notebook 执行。"""
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import time


STAGES = ["文件就在目录里，Agent 为什么没有用上？",
          "模型这一轮看见的，不是整个工作目录",
          "谁来准备模型每一轮看到的内容？"]


class ContextEngineStory:
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
            self.marker = "SAMPLE-" + secrets.token_hex(6)
            self.deployment = self.work / "deployment.md"
            text = (self.materials / "deployment.md").read_text(encoding="utf-8")
            self.deployment.write_text(text.replace("{{SAMPLE_MARKER}}", self.marker), encoding="utf-8")
            self.alert = (self.materials / "alert.md").read_text(encoding="utf-8").strip()
            self.rule = "区分已知事实和待验证的猜测；引用材料时说明文件名和时间，不要修改文件。"
            self.question = "目前已经确认哪些事实，还缺什么信息才能判断原因？"
            self.input_snapshot = self.snapshot(self.work)
            self._save("inputs-before.json", self.input_snapshot)
            self._save("run.json", {"mode": "material-replay" if self.offline else "live",
                                    "model": None if self.offline else "glm-5.2", "marker": self.marker,
                                    "alert": self.alert, "rule": self.rule, "question": self.question})
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

    def show_answers(self, before, result, before_start, read_start, read_end, thread):
        self._save("comparison.json", {"before_start": before_start, "read_start": read_start,
                   "read_end": read_end, "thread_id": thread.id if thread else None,
                   "before_answer": before.final_response if before else None,
                   "after_answer": result.final_response if result else None})
        rows = [("只根据告警，尚未读取部署记录\n" + self.question,
                 before.final_response if before else "离线没有模型回答；告警材料：\n" + self.alert),
                ("要求读取 deployment.md 后\n" + self.question,
                 result.final_response if result else "离线没有执行模型读取；部署材料由 Python 展示：\n"
                 + self.deployment.read_text(encoding="utf-8"))]
        self._table(["同一问题", "本次实际回答"], rows,
                    "两次都问已确认哪些事实、还缺什么；第二次才要求读取部署记录。变化是否进入判断，要继续对回真实请求和回执。"
                    if self.lab else "离线仅展示构造材料，不提供模拟回答或工具回执，也不证明模型读过文件。")
        self._phase(STAGES[0])

    def excerpt(self, text, needles, width=140):
        text = self.lab.clean(text) if self.lab else str(text)
        spans = []
        for needle in needles:
            position = text.find(needle)
            if position >= 0:
                spans.append((max(0, position - width), min(len(text), position + len(needle) + width)))
        if not spans:
            return text[:280] + ("…（完整内容见本次请求记录）" if len(text) > 280 else "")
        merged = []
        for a, b in sorted(spans):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        return "\n…\n".join(text[a:b] for a, b in merged)

    def request_parts(self, request):
        items = request.get("input", [])
        return {
            "基础指令 instructions": bool(request.get("instructions")),
            "工具定义 tools": bool(request.get("tools")),
            "任务消息 input": any(isinstance(i, dict) and i.get("role") == "user"
                                   and self.question in json.dumps(i, ensure_ascii=False) for i in items),
            "开发者规则在消息中": any(isinstance(i, dict) and i.get("role") == "developer"
                                     and self.rule in json.dumps(i, ensure_ascii=False) for i in items),
        }

    def show_requests(self, requests, start=0):
        from notebook_support import tool_output_contains
        rows, records = [], []
        needles = [self.rule, self.question, "deployment.md", self.marker, "02:07", "连接池上限"]
        for index, request in enumerate(requests, start + 1):
            parts = self.request_parts(request)
            entered = tool_output_contains(request, self.marker)
            records.append({"request": index, "parts": parts, "marker_in_tool_output": entered})
            rows.append((index, "基础指令 · instructions", self.excerpt(str(request.get("instructions", "")), [self.rule])))
            tools = request.get("tools", [])
            rows.append((index, "工具定义 · tools", ", ".join(t.get("name", t.get("type", "")) for t in tools)
                         + "\n" + self.excerpt(json.dumps(tools, ensure_ascii=False), ["exec_command"])))
            for item in request.get("input", []):
                if not isinstance(item, dict):
                    continue
                kind = "工具结果" if item.get("type") == "function_call_output" else item.get("role", item.get("type", "消息"))
                label = {"developer": "开发者消息", "user": "任务消息", "assistant": "模型消息"}.get(kind, kind)
                rows.append((index, label + " · input", self.excerpt(json.dumps(item, ensure_ascii=False), needles)))
        self._save("request-assembly.json", {"requests": records, "rows": rows})
        self._table(["请求", "组成", "规则与编号附近的真实内容"],
                    rows or [("离线回放", "没有模型请求", "不制造请求组成或编号进入的时机")],
                    "这里是客户端发给本地适配器、尚未经过协议转换的 Responses 请求体。基础指令与工具定义分别列出，开发者规则位于消息中；"
                    "工具结果里的编号才说明部署正文已进入这一轮输入。完整请求保存为 request-*.json，不代表模型内部推理。")

    def show_composition(self, parts, receipts, marker_requests):
        first = next((index for index, entered in marker_requests if entered), None)
        self._save("composition.json", {"parts": parts, "receipts": receipts,
                   "marker_requests": marker_requests, "marker_first_request": first})
        self._table(["请求组成", "本次是否出现"],
                    [(name, "是" if value else "否") for name, value in parts.items()]
                    if self.lab else [(name, "离线无请求，不适用") for name in parts],
                    "以只依据告警的第一次请求核对四项组成。随机编号首次进入工具结果的请求序号："
                    + (str(first) if first is not None else "离线无请求" if self.offline else "未捕获，请查看读取回执"))
        self._phase(STAGES[1])

    @staticmethod
    def new_tool_calls(requests, previous_ids, result=None):
        calls = {}
        for request in requests:
            for item in request.get("input", []):
                if isinstance(item, dict) and item.get("type") == "function_call" and item.get("call_id") not in previous_ids:
                    calls[item.get("call_id")] = item
        # 最后一次工具执行可能没有后续请求，仍从客户端事件单独核查。
        if result:
            for wrapped in result.items:
                item = wrapped.root
                if item.type in ("commandExecution", "fileChange"):
                    calls[item.id] = item.model_dump(mode="json", by_alias=True)
        return list(calls.values())

    def show_followup(self, followup, followup_calls, read_calls, new_calls, after, thread):
        from notebook_support import tool_output_contains
        try:
            first = followup_calls[0] if followup_calls else {}
            retained = tool_output_contains(first, self.marker)
            # 找旧回执的原调用编号，避免仅凭追问回答或编号复述判断。
            old_outputs = {i.get("call_id"): str(i.get("output", "")) for r in read_calls for i in r.get("input", [])
                           if isinstance(i, dict) and i.get("type") == "function_call_output" and self.marker in str(i.get("output", ""))}
            retained_outputs = [i for i in first.get("input", []) if isinstance(i, dict)
                                and i.get("type") == "function_call_output"
                                and i.get("call_id") in old_outputs
                                and str(i.get("output", "")) == old_outputs[i.get("call_id")]]
            self._save("inputs-after.json", after)
            self._save("followup.json", {"thread_id": thread.id if thread else None,
                       "answer": followup.final_response if followup else None,
                       "old_outputs": old_outputs, "retained_outputs": retained_outputs,
                       "marker_retained": retained, "new_tool_calls": new_calls,
                       "inputs_unchanged": after == self.input_snapshot})
            rows = [("读取轮首次请求", "尚未读入" if read_calls and not tool_output_contains(read_calls[0], self.marker)
                     else "离线无请求" if self.offline else "已出现，需核查", "读取动作见前面的配对回执"),
                    ("读取后的后续请求", "部署编号经工具结果进入" if old_outputs else "离线无请求" if self.offline else "未见部署工具结果", "见前面的真实命令"),
                    ("同一会话追问", "原工具结果仍在输入中" if retained_outputs else "离线无请求" if self.offline else "未确认保留原工具结果",
                     len(new_calls) if self.lab else "离线没有调用"),
                    ("追问的实际回答", followup.final_response if followup else "离线未调用模型", "只问采集编号与采集时间"),
                    ("工作目录输入文件", "未改变" if after == self.input_snapshot else "发生变化", "比较全部输入文件的 SHA-256")]
            self._table(["位置", "工具结果中的记录", "本轮新工具调用"], rows,
                        "工具返回后，运行环境依据更新的历史重新准备下一次输入；追问可以继续带上旧回执。文件在目录里≠进入上下文；"
                        "客户端事件记录≠这一轮的完整输入。事件能核对动作与返回，请求记录才呈现本轮实际装入的内容。"
                        + ("离线只比较材料文件，不证明输入重新准备或旧回执保留。" if self.offline else ""))
            self._phase(STAGES[2])
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
