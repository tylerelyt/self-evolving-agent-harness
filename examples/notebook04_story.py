"""04 讲的材料、三组过程表格与留痕；规则写入和会话调用由 notebook 执行。"""
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import time


STAGES = ["上一次已经改对，为什么新的任务又错了？", "哪些纠正值得留下？",
          "规则写在哪里，决定哪些任务会使用它"]


def request_input(request):
    """只整理请求的实际 input 字段，不把工具定义或基础指令当作项目规则。"""
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
    return "\n".join(strings(request.get("input", [])))


class AgentsMdStory:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.offline = bool(os.environ.get("LAB_VALIDATION_RUN_ID"))
        self.lab, self._ipython = None, None
        self.closed = False
        self.metrics, self.tasks, self.assembly = [], {}, []
        if self.offline:
            run_id = os.environ["LAB_VALIDATION_RUN_ID"]
            if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", run_id):
                raise ValueError("本次运行编号不合法。")
            self.runtime = self.directory / ".runtime" / run_id
            self.runtime.mkdir(parents=True, exist_ok=False)
            self.work = self.runtime / "work"
            self.work.mkdir()
            (self.work / ".git").mkdir()
        else:
            from notebook_support import Lab
            self.lab = Lab(self.directory)
            self.runtime, self.work = self.lab.runtime, self.lab.work
        try:
            self.materials = self.directory / "materials"
            self.payment = self.work / "payment-service"
            self.payment.mkdir()
            self.override_path = self.payment / "AGENTS.override.md"
            self.override_tag = "OVERRIDE-" + secrets.token_hex(8)
            self.root_rule = (self.materials / "AGENTS.md").read_text(encoding="utf-8").strip()
            self.local_rule = (self.materials / "payment-service/AGENTS.md").read_text(encoding="utf-8").strip()
            self.override_rule = (self.materials / "payment-service/AGENTS.override.md").read_text(
                encoding="utf-8").replace("{{OVERRIDE_TAG}}", self.override_tag).strip()
            self.incidents = {}
            for name in ("old-incident.md", "new-incident.md"):
                text = (self.materials / name).read_text(encoding="utf-8")
                (self.work / name).write_text(text, encoding="utf-8")
                self.incidents[name] = text
            self.correction = "时间上先后发生的两件事，不能直接写成因果关系。"
            self.old_prompt = (self.incidents["old-incident.md"] + "\n旧稿采用“先发生即原因”的写法。"
                               "请按这个旧稿写法给出一段简短原因分析，不调用工具。")
            self.new_prompt = self.incidents["new-incident.md"] + "\n请为这次事故写一段简短原因分析，不调用工具。"
            self.before = self.snapshot(self.work)
            self._save("inputs-before.json", self.before)
            self._save("run.json", {"mode": "material-replay" if self.offline else "live",
                "model": None if self.offline else "glm-5.2", "correction": self.correction,
                "root_rule": self.root_rule, "local_rule": self.local_rule, "override_rule": self.override_rule,
                "override_tag": self.override_tag, "old_prompt": self.old_prompt, "new_prompt": self.new_prompt})
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

    def _table(self, headers, rows, note, extra_tables=()):
        """每个阶段一组 act-result；范围、回答和同层选择在第三组中分别对照。"""
        from IPython.display import HTML, display
        def clean(value):
            return escape(self.lab.clean(value) if self.lab else str(value))
        tables = []
        for titles, values in [(headers, rows), *extra_tables]:
            head = "".join("<th style='text-align:left;font-size:14px'>" + clean(x) + "</th>" for x in titles)
            body = "".join("<tr>" + "".join("<td style='text-align:left;font-size:14px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top'>"
                           + clean(x) + "</td>" for x in row) + "</tr>" for row in values)
            tables.append("<table style='width:100%;table-layout:fixed;margin-bottom:16px'><thead><tr>" + head
                          + "</tr></thead><tbody>" + body + "</tbody></table>")
        display(HTML("<div class='act-result'>" + "".join(tables) + "<p>" + clean(note) + "</p></div>"))

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

    def capture(self, name, start, end, result, thread, cwd):
        self.tasks[name] = {"start": start, "end": end, "answer": result.final_response if result else None,
                            "thread_id": thread.id if thread else None, "cwd": str(cwd.relative_to(self.work))}
        for index in range(start, end):
            text = request_input(self.lab.requests[index])
            positions = {label: text.find(rule) for label, rule in (
                ("correction", self.correction), ("root", self.root_rule),
                ("local", self.local_rule), ("override", self.override_rule))}
            self.assembly.append({"task": name, "request": index + 1, "positions": positions,
                                  "input": text, "first_in_task": index == start})
        self._save("tasks.json", self.tasks)
        self._save("request-assembly.json", self.assembly)

    def show_history(self, old, corrected, fresh, old_thread, fresh_thread, starts):
        bounds = [*starts, len(self.lab.requests) if self.lab else 0]
        for i, (name, result, thread) in enumerate((
                ("old", old, old_thread), ("corrected", corrected, old_thread), ("unruled", fresh, fresh_thread))):
            self.capture(name, bounds[i], bounds[i + 1], result, thread, self.payment)
        self._table(["阶段", "本次回答"], [
            ("旧初答", old.final_response if old else "离线没有模型初答；旧材料：\n" + self.incidents["old-incident.md"]),
            ("旧纠正后", corrected.final_response if corrected else "离线只展示纠正文本：\n" + self.correction),
            ("新任务未写规则", fresh.final_response if fresh else "离线没有模型回答；新材料：\n" + self.incidents["new-incident.md"]),
        ], "旧初答按提示指定的旧稿写法生成，不作为模型自然犯错率的测量。新回答即使谨慎，也不能证明旧纠正自动进入了新会话；要对回实际输入。"
           + ("离线不提供模拟回答或请求。" if self.offline else ""))
        self._phase(STAGES[0])

    def show_selection(self, task_specific, stable_requirements, rule_text):
        self._save("selection.json", {"task_specific": task_specific, "stable_requirements": stable_requirements,
                                       "rule_text": rule_text})
        self._table(["本次信息", "留在旧任务", "进入项目要求"], [
            ("实例与参数（构造值）", "\n".join(task_specific), "不复制服务器名、连接池参数或旧事故结论"),
            ("团队确认的调查方法", "纠正发生的过程仍留在旧任务", "\n".join(stable_requirements)),
        ], "这里由人选取已确认的稳定要求；读取候选文件供读者核对，不会自动把它放入模型输入，也不把这一选择包装成自动提炼。")
        self._phase(STAGES[1])

    def record_scope(self, root_result, local_result, root_thread, local_thread, root_start, local_start):
        end = len(self.lab.requests) if self.lab else 0
        self.capture("root", root_start, local_start, root_result, root_thread, self.work)
        self.capture("local", local_start, end, local_result, local_thread, self.payment)

    def show_rules(self, override_result, override_thread, override_start):
        try:
            self.capture("override", override_start, len(self.lab.requests) if self.lab else 0,
                         override_result, override_thread, self.payment)
            texts = {name: request_input(self.lab.requests[self.tasks[name]["start"]]) if self.lab else ""
                     for name in ("root", "local", "override")}
            status = lambda yes: "已装入" if yes else "未装入"
            scope = [(label, status(self.root_rule in texts[name]) if self.lab else "离线无请求",
                      status(self.local_rule in texts[name]) if self.lab else "离线无请求")
                     for name, label in (("root", "项目根目录"), ("local", "payment-service/"))]
            answers = [(label, self.tasks[name]["answer"] or "离线没有模型回答") for name, label in
                       (("unruled", "未写规则"), ("root", "只有根规则"), ("local", "根规则与服务规则"))]
            order = [texts["local"].find(self.root_rule), texts["local"].find(self.local_rule)] if self.lab else None
            chosen = {"根规则仍在": self.root_rule in texts["override"] if self.lab else None,
                      "优先文件已加入": self.override_rule in texts["override"] if self.lab else None,
                      "同层原规则未合并": self.local_rule not in texts["override"] if self.lab else None,
                      "临时文件已撤下": not self.override_path.exists()}
            self._save("scope.json", {"rows": scope, "answers": answers, "root_local_positions": order,
                                     "override_checks": chosen})
            after = self.snapshot(self.work)
            self._save("inputs-after.json", after)
            self._table(["新会话工作目录", "根规则", "服务规则"], scope,
                "根到当前目录的实际位置：" + (str(order) if order else "离线不检验框架加载")
                + "。优先文件替换这一层整份规则，保留的本地要求须写完整；根规则属于另一层。"
                "规则进入输入只证明要求已收到；报告是否遵守另看，不代表参数更新或写入权限。",
                extra_tables=[(["同场事故", "原因分析实际写法"], answers),
                              (["同层选择", "本次结果"], [(k, "离线无请求，不适用" if v is None else "是" if v else "否")
                                                            for k, v in chosen.items()])])
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
            if hasattr(self, "override_path"):
                self.override_path.unlink(missing_ok=True)
            if self.lab:
                self.lab.close()
            self.closed = True
        finally:
            self._save("cleanup.json", {"closed": self.closed,
                "override_removed": not self.override_path.exists() if hasattr(self, "override_path") else True,
                "proxy_returncode": self.lab.proxy.poll() if self.lab and self.lab.proxy else None,
                "errors": self.lab._cleanup_errors if self.lab else []})
