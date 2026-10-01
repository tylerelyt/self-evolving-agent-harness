"""08 讲的材料、表格与运行记录；离线回放与模型请求分别保存。"""
import atexit
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import time


def snapshot(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


class ProgressiveDisclosureStory:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.offline = bool(os.environ.get("LAB_VALIDATION_RUN_ID"))
        self.lab, self.closed = None, False
        self.markers = {name: name + "-" + secrets.token_hex(6)
                        for name in ("META", "BODY", "REF", "ASSET", "SOURCE", "RESULT")}
        self.replay_context, self.replay_events, self.ranges = [], [], {}
        self.stages = []
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
            # 公共 Lab 使用这个变量命名目录；08 的模式判断已在前面完成。
            run_id = os.environ.get("NB08_LIVE_RUN_ID") or ("nb08-v1-live-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
            os.environ["LAB_VALIDATION_RUN_ID"] = run_id
            try:
                self.lab = Lab(self.directory)
            finally:
                os.environ.pop("LAB_VALIDATION_RUN_ID", None)
            self.runtime, self.work = self.lab.runtime, self.lab.work
        try:
            self.materials = self.directory / "materials"
            self.skill_dir = self.work / ".agents/skills/legacy-payment-oncall"
            shutil.copytree(self.materials / "legacy-payment-oncall", self.skill_dir)
            shutil.copytree(self.materials / "inputs", self.work / "inputs")
            for p in self.skill_dir.rglob("*"):
                if p.is_file():
                    text = p.read_text(encoding="utf-8")
                    for name, marker in {**self.markers, "PYTHON": sys.executable}.items():
                        text = text.replace("{{" + name + "}}", marker)
                    p.write_text(text, encoding="utf-8")
            self.skill_path = self.skill_dir / "SKILL.md"
            self.relative_skill = self.skill_dir.relative_to(self.work)
            self.replay_paths = ["inputs/alert.md", self.relative_skill / "references/log-fields.md",
                "inputs/legacy.log", "inputs/release.md", "inputs/pool.md", "inputs/config.md",
                self.relative_skill / "assets/incident-report.md"]
            self.report_path = self.work / "report.md"
            self.initial_files = {"inputs": snapshot(self.work / "inputs"), "skill": snapshot(self.skill_dir)}
            self.save("inputs-before.json", self.initial_files)
            self.save("run.json", {"mode": "material-replay" if self.offline else "live",
                                  "markers": self.markers, "python": sys.executable,
                                  "skill": str(self.relative_skill)})
            atexit.register(self.close)
            from IPython import get_ipython
            self._ipython = get_ipython()
            if self._ipython:
                self._ipython.events.register("post_run_cell", self._on_error)
        except BaseException:
            self.close()
            raise

    def save(self, name, value):
        if self.lab:
            self.lab._write_json(name, value)
        else:
            (self.runtime / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def table(self, headers, rows, note):
        from IPython.display import HTML, display
        def clean(value):
            return escape(self.lab.clean(value) if self.lab else str(value))
        head = "".join("<th>" + clean(x) + "</th>" for x in headers)
        body = "".join("<tr>" + "".join("<td style='vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere'>"
                       + clean(x) + "</td>" for x in row) + "</tr>" for row in rows)
        display(HTML("<table style='width:100%;table-layout:fixed;text-align:left'><thead><tr>" + head
                     + "</tr></thead><tbody>" + body + "</tbody></table><p>" + clean(note) + "</p>"))

    def phase(self, name):
        self.stages.append(name)
        self.save("stages.json", self.stages)

    def show_budget(self, contents, packed, core):
        self.save("budget.json", {"core_chars": len(core), "packed_chars": len(packed),
                                  "extra_chars": len(packed) - len(core),
                                  "files": {name: len(text) for name, text in contents.items()}})
        self.table(["组织方式", "入口字符数", "选中后的代价"], [
            ("全部堆进入口", len(packed), "还没读日志，字段表已经出现；还没写报告，模板与源码已经出现"),
            ("主干与附件分开", len(core), "入口保留使用时机与路径；正文需读、脚本需执行"),
            ("提前装入的附件", len(packed) - len(core), "这里量的是材料字符，不是实际模型 token 数"),
        ], "比较同一套真实文件；文件分层是准备，什么时候打开仍取决于后面的 Loop。")
        self.phase("阶段一")

    def _replay_snapshot(self, stage):
        self.replay_context.append({"stage": stage, "content": "\n".join(e["output"] for e in self.replay_events)})
        self.save("replay-context.json", self.replay_context)
        self.save("replay-events.json", self.replay_events)

    def record_entry(self, start, catalog_end, method_end):
        self.ranges.update(catalog=[start, catalog_end], method=[catalog_end, method_end])
        if self.offline:
            text = self.skill_path.read_text(encoding="utf-8")
            metadata = text.split("---", 2)[1].strip()
            self.replay_events.append({"action": "能力索引", "output": metadata})
            self._replay_snapshot("能力索引")
            self.replay_events.append({"action": "选中装入", "path": str(self.relative_skill / "SKILL.md"), "output": text})
            self._replay_snapshot("选中装入")
        self.save("request-ranges.json", self.ranges)

    def replay_read(self, relative):
        path = self.work / relative
        text = path.read_text(encoding="utf-8")
        self.replay_events.append({"action": "本地读取", "path": str(relative), "output": text})
        self._replay_snapshot(str(relative))
        return text

    def replay_report(self):
        # 构造报告刻意漏掉一条推断的引用，观察检查程序的反馈；不是模型回答。
        text = ("# 事故调查报告（离线构造材料）\n\n## 已确认事实\n"
                "- 事实：旧日志 rid=pay-constructed-001 出现域名解析失败；连接池使用率为构造值 9%，等待为 0。[日志](inputs/legacy.log:2) [指标](inputs/pool.md:2)\n"
                "## 当前推断\n- 推断：旧域名停用后，模块配置未同步可能是本次超时的主要原因；还未做恢复验证。\n"
                "## 待验证问题\n核对全部实例，并验证配置更新后是否恢复。[配置](inputs/config.md:3)\n")
        self.report_path.write_text(text, encoding="utf-8")
        self.replay_events.append({"action": "写构造报告", "path": "report.md", "output": "报告写入完成"})
        self._replay_snapshot("报告写完")

    def record_script(self, result):
        self.save("script-replay.json", {"exit_code": result.returncode, "output": result.stdout.strip(), "stderr": result.stderr})
        self.replay_events.append({"action": "本地执行检查脚本", "output": result.stdout.strip(), "exit_code": result.returncode})
        self._replay_snapshot("脚本结果返回")

    def observe_loop(self, start):
        from notebook_support import command_receipts, tool_output_contains
        requests = self.lab.requests if self.lab else []
        self.ranges["investigation"] = [start, len(requests)]
        self.save("request-ranges.json", self.ranges)
        receipts = command_receipts(requests[start:])
        self.save("receipts.json", receipts)
        rows = []
        if self.offline:
            for state in self.replay_context:
                rows.append((state["stage"], *["已进入" if m in state["content"] else "未见" for m in self.markers.values()]))
            note = "离线材料回放：文件真实读取、程序真实执行。此表没有模型请求，不证明模型主动选择或按需取用。"
        else:
            for i, request in enumerate(requests, 1):
                text = json.dumps(request, ensure_ascii=False)
                rows.append((f"实际请求 {i}", *["已进入" if m in text else "未见" for m in self.markers.values()]))
            note = "看正文标记而非路径；字段表与模板经工具结果进入后续请求，脚本返回结果与源码分开观察。"
        self.table(["输入准备阶段", "名称描述", "方法正文", "字段表", "模板", "脚本源码", "脚本结果"], rows, note)
        if self.offline:
            script = json.loads((self.runtime / "script-replay.json").read_text())
            script_rows = [("scripts/check_report.py report.md（本地回放）", script["exit_code"], script["output"])]
        else:
            script_rows = [(r["command"], r["exit_code"], r["output"]) for r in receipts if "check_report.py" in r["command"]]
        self.table(["实际执行", "退出码", "返回结果"], script_rows,
                   "检查程序只检查引用格式；返回缺引用条目不等于推断已被证实。")
        firsts = {name: next((i for i, req in enumerate(requests, 1) if tool_output_contains(req, marker)), None)
                  for name, marker in self.markers.items()}
        self.save("first-tool-results.json", firsts)
        if self.report_path.is_file():
            from IPython.display import Markdown, display
            display(Markdown(self.lab.clean(self.report_path.read_text()) if self.lab else self.report_path.read_text()))
        self.phase("阶段二")

    @staticmethod
    def match_description(description, task):
        # 明示的词语匹配演示，不能代替原生 Harness 的模型选择实验。
        return "各类问题" in description or any(word in description and word in task
                                              for word in ("超时", "错误率", "连接池"))

    def show_descriptions(self, descriptions, tasks, decisions):
        self.save("descriptions.json", {"descriptions": descriptions, "tasks": tasks, "decisions": decisions,
                                        "kind": "本地词语匹配演示，不是原生自动选择结果"})
        self.table(["入口描述", "无关磁盘任务", "连接池任务", "为什么要检查边界"], [
            (descriptions[name], *["选中" if x else "未选" for x in decisions[name]], why)
            for name, why in (("太宽", "无关问题也被匹配"), ("合适", "保留模块与告警范围"), ("太窄", "缺少连接池入口，遗漏相关任务"))
        ], "这是本地词语匹配对照，用来暴露描述的边界；实际模型的语义选择质量尚未核验。")

    def finish(self):
        after = {"inputs": snapshot(self.work / "inputs"), "skill": snapshot(self.skill_dir)}
        self.save("inputs-after.json", after)
        self.table(["Harness 部件", "负责什么", "本练习怎样观察"], [
            ("能力索引", "提供名称、描述和正文路径", "初始只有索引标记，附件正文留在磁盘"),
            ("选中装入", "SkillInput 指定方法，由 Codex 装入正文", "显式选中后方法标记进入；不把显式选择当成自动匹配"),
            ("Agent Loop", "模型提出读文件或运行，Harness 执行，再回流结果", "参考与模板看读取结果；脚本看执行结果"),
            ("litellm 代理层", "把真实请求适配给 GLM-5.2，并记录请求体", "离线未启动；live 需再按 call_id 核对实际请求"),
        ], "输入文件与 Skill：" + ("未改变。" if after == self.initial_files else "有变化，请核对记录。"))
        self.phase("阶段三")
        self.close()

    def _on_error(self, result):
        if result.error_before_exec or result.error_in_exec:
            self.close()

    def close(self):
        if self.closed:
            return
        if getattr(self, "_ipython", None):
            self._ipython.events.unregister("post_run_cell", self._on_error)
        errors, ports, proxy_code, model_code = [], {}, None, None
        if self.lab:
            proxy = self.lab.proxy
            proc = getattr(self.lab.codex._client, "_proc", None) if self.lab.codex else None
            observed_ports = ([int(proxy.args[proxy.args.index("--port") + 1])] if proxy else [])
            if self.lab.observer:
                observed_ports.append(self.lab.observer.server_port)
            try:
                self.lab.close()
            except Exception as exc:
                errors.append(self.lab.clean(exc))
            proxy_code, model_code = proxy.poll() if proxy else None, proc.poll() if proc else None
            for port in observed_ports:
                with socket.socket() as sock:
                    sock.settimeout(1)
                    ports[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
            errors.extend(self.lab._cleanup_errors)
        self.closed = not errors and not any(ports.values())
        self.save("cleanup.json", {"closed": self.closed, "model_started": self.lab is not None,
            "proxy_returncode": proxy_code, "model_returncode": model_code, "ports_open": ports, "errors": errors})
        atexit.unregister(self.close)
        if not self.closed:
            raise RuntimeError("本次连接未完全关闭，请核对 cleanup.json。")
