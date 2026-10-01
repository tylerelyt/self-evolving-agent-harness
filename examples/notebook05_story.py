"""05 讲的构造材料、等待、表格与留痕；核心会话动作在 notebook 执行。"""
import hashlib
from html import escape
import importlib.util
import json
import os
from pathlib import Path
import re
import time


STAGES = ["历史被压缩以后，调查还能接着做吗？",
          "上下文更新后，什么不该随之改变？", "Codex 怎样做 Compaction？"]
CORRECTION = ("补充确认：相同报错在应用发布以前已经出现，因此暂时不能把发布确认为直接根因，"
              "也不能说发布已被完全排除。连接等待与错误率同时升高，所以先核对连接池配置变更。"
              "原文在 capacity.log 中，不能重启服务。简短确认，不调用工具。")
STATE_FIELDS = ["task", "release_status", "reason", "next_step", "next_reason", "constraint"]
EXACT_FIELDS = ["sample_0073", "sample_0127", "difference"]


def input_text(request):
    return json.dumps(request.get("input", []), ensure_ascii=False)


def sample_lines(text):
    return [next(line for line in text.splitlines() if line.startswith(f"sample={i:04d} "))
            for i in (73, 127)]


def sample_values(text):
    return {f"sample_{i:04d}": int(re.search(r"elapsed_us=(\d+)", line).group(1))
            for i, line in zip((73, 127), sample_lines(text))}


def available_samples(text, lines):
    # 数值必须与采样编号关联；不能只因同一数字出现在别处就认定有依据。
    values = sample_values("\n".join(lines))
    return [line in text or bool(re.search(
        rf"sample[=_ ]*0*{i}(?!\d)[^\n]{{0,160}}?elapsed_us[^\d\n]{{0,16}}{values[f'sample_{i:04d}']}(?!\d)", text))
        or bool(re.search(rf'''["']?sample_{i:04d}["']?\s*[:=：]\s*{values[f"sample_{i:04d}"]}(?!\d)''', text))
        for i, line in zip((73, 127), lines)]


class ContextCompactionStory:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.offline = bool(os.environ.get("LAB_VALIDATION_RUN_ID"))
        self.lab, self.closed, self._ipython = None, False, None
        self.metrics = []
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
            spec = importlib.util.spec_from_file_location("nb05_capacity", self.directory / "materials/generate_capacity.py")
            generator = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(generator)
            self.logfile = generator.write_capacity(self.work / "capacity.log")
            self.original_text = self.logfile.read_text(encoding="utf-8")
            self.original_samples = sample_lines(self.original_text)
            self.input_snapshot = self.snapshot(self.work)
            self._save("inputs-before.json", self.input_snapshot)
            self._save("run.json", {"mode": "material-replay" if self.offline else "live",
                                    "model": None if self.offline else "glm-5.2", "constructed": True})
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

    def show_before(self, first, corrected, correction, before_request, history):
        from notebook_support import command_receipts
        receipts = command_receipts(self.lab.requests) if self.lab else []
        self._save("before.json", {"first_answer": first.final_response if first else None,
                   "corrected_answer": corrected.final_response if corrected else None,
                   "correction": correction, "request_index": len(self.lab.requests) if self.lab else None,
                   "input_chars": len(input_text(before_request)) if self.lab else None,
                   "turn_ids": [t.id for t in history.turns] if history else [], "receipts": receipts})
        rows = [("任务与原始材料", "CASE-527；capacity.log：180 行采样、末尾异常记录；全部为构造值"),
                ("Agent 读取后的判断", first.final_response if first else "离线无模型回答；磁盘材料：\n" + "\n".join(self.original_text.splitlines()[-3:])),
                ("同一会话收到的纠正", correction),
                ("纠正后的实际回答", corrected.final_response if corrected else "离线没有模型确认"),
                ("成功读取的工具回执", "\n".join(f"{r['command']} · 退出码 {r['exit_code']}" for r in receipts)
                 if self.lab else "离线未执行模型读取，不制造工具回执")]
        self._table(["压缩前的依据", "实际内容"], rows,
                    "会话累积任务目标、已取得的事实、纠正与限制；磁盘上有材料，不等于已经进入模型输入。")
        self._phase(STAGES[0])

    def wait_compaction(self, thread, before_turn_ids, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = thread.read(include_turns=True).thread
            candidates = [t for t in history.turns if t.id not in before_turn_ids
                          and any(i.root.type == "contextCompaction" for i in t.items)]
            if candidates:
                turn = candidates[-1]
                if turn.status.value in ("failed", "interrupted"):
                    raise RuntimeError("原生压缩未完成：" + turn.status.value)
                if turn.status.value == "completed":
                    self._save("compaction.json", {"thread_id": thread.id,
                        "turn": turn.model_dump(mode="json", by_alias=True)})
                    return turn
            time.sleep(1)
        raise TimeoutError("240 秒内没有取得新增 contextCompaction 完成项，停止后续模型调用。")

    def show_after(self, compaction, recap, state, before_chars, after_chars, history, recap_start):
        self._save("after.json", {"answer": recap.final_response if recap else None, "state": state,
                   "before_chars": before_chars, "after_chars": after_chars, "request_index": recap_start if self.lab else None,
                   "history": history.model_dump(mode="json", by_alias=True) if history else None})
        absent = "离线没有模型复述，未验证保留情况"
        rows = [("输入字符数", before_chars if self.lab else "离线无请求", after_chars if self.lab else "离线无请求"),
                ("原生压缩完成项", "尚未发起", compaction.status.value + " · contextCompaction" if compaction else "离线不发起压缩"),
                ("任务编号", "CASE-527", state.get("task", absent)),
                ("发布判断与依据", "仍待验证；发布前已有同类报错", state.get("release_status", absent) + "；" + state.get("reason", "")),
                ("下一步及理由", "核对连接池配置变更；连接等待与错误率同时升高", state.get("next_step", absent) + "；" + state.get("next_reason", "")),
                ("操作限制", "不许重启", state.get("constraint", absent))]
        self._table(["比较项", "压缩前", "压缩后"], rows,
                    "工作集在压缩完成后替换。判断的确定程度与下一步理由要保留；字符数只说明输入长度，不代表 token 或费用。")
        self._phase(STAGES[1])

    def show_storage(self, before_request, after_request, history, recovered, exact, expected, new_reads, recall_requests, after):
        try:
            before = input_text(before_request) if self.lab else ""
            current = input_text(after_request) if self.lab else ""
            full = history.model_dump_json(by_alias=True) if history else ""
            initial = input_text(recall_requests[0]) if recall_requests else ""
            availability = available_samples(initial, self.original_samples) if self.lab else []
            self._save("inputs-after.json", after)
            self._save("recall.json", {"answer": recovered.final_response if recovered else None,
                "exact": exact, "expected": expected, "new_reads": new_reads,
                "available_before": availability, "request_start": len(self.lab.requests) - len(recall_requests) if self.lab else None,
                "inputs_unchanged": after == self.input_snapshot,
                "samples": self.original_samples, "history_ids": [t.id for t in history.turns] if history else []})
            def found(text):
                return "；".join(f"采样 {i}：" + ("可找到原句" if line in text else "未找到原句")
                                for i, line in zip((73, 127), self.original_samples))
            old_ids = set(json.loads((self.runtime / "before.json").read_text())["turn_ids"])
            retained_ids = old_ids & {t.id for t in history.turns} if history else set()
            def excerpt(receipt):
                matching = [line for line in receipt["output"].splitlines()
                            if any(sample in line for sample in ("sample=0073", "sample=0127", "sample_0073", "sample_0127"))]
                output = "\n".join(matching) if matching else receipt["output"][:600]
                return f"{receipt['command']} · 退出码 {receipt['exit_code']}\n{output}"
            rows = [("压缩前模型输入", found(before) if self.lab else "离线无请求"),
                    ("压缩后复述的首次输入", found(current) if self.lab else "离线无请求"),
                    ("压缩后读取完整 Thread 历史", found(full) if self.lab else "离线没有任务历史"),
                    ("磁盘 capacity.log（构造值）", found(self.logfile.read_text(encoding="utf-8")) + "\n" + "\n".join(self.original_samples)),
                    ("精确追问的本次回答", recovered.final_response if recovered else "离线没有模型回答；Python 对照值：" + json.dumps(expected, ensure_ascii=False)),
                    ("追问开始时的依据", str(availability) + "（分别对应 73、127）" if self.lab else "离线无请求"),
                    ("追问新增的工具回执（采样节选）", "\n".join(excerpt(r) for r in new_reads)
                     if new_reads else "本次没有新增工具回执" if self.lab else "离线没有工具调用"),
                    ("原调查轮次", f"原 {len(old_ids)} 轮中仍可读取 {len(retained_ids)} 轮，见历史记录" if history else "离线没有调查轮次"),
                    ("输入文件 SHA-256", "未改变" if after == self.input_snapshot else "有变化")]
            self._table(["保存位置", "采样原文是否可找到"], rows,
                        "完整历史、后续请求的工作集与磁盘原文分别核对。摘要若已有所需数值，直接回答也正常；缺少依据时应成功回读，再对照原文。"
                        + ("离线只回放材料，不证明压缩或模型行为。" if self.offline else ""))
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
