"""07 讲的材料、表格与留痕；核心动作由 notebook 直接执行。"""
from html import escape
import json
import os
from pathlib import Path
import shutil
import time


REPORT = (
    "抽查的异常请求均在访问结算服务时发生域名解析失败；结算服务已经启用新地址，"
    "历史支付模块仍使用旧域名；同一时段没有应用发布，数据库连接指标也保持正常；"
    "现有日志、配置和变更记录共同支持“依赖地址迁移后配置未同步”是本次超时的主要原因。"
)


class PaymentSkillStory:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.offline = bool(os.environ.get("LAB_VALIDATION_RUN_ID"))
        self.closed = False
        self.lab = None
        self.metrics = []
        if self.offline:
            import re
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
            self.creator = self.work / "tooling/skill-creator"
            shutil.copytree(self.directory / "skill_creator", self.creator)
            # 旧记录留在模块仓库外，新调查只能从模块自己的目录出发。
            self.history = self.work / "history"
            self.history.mkdir()
            shutil.copyfile(self.materials / "old-thread.md", self.history / "old-thread.md")
            self.project = self.work / "legacy-payment"
            self.project.mkdir()
            (self.project / ".git").mkdir()
            self.current = self.project / "current"
            self.current.mkdir()
            shutil.copyfile(self.materials / "query.py", self.current / "query.py")
            shutil.copyfile(self.materials / "current.json", self.current / "current.json")
            shutil.copyfile(self.materials / "alert.md", self.project / "alert.md")
            self.skill_dir = self.project / ".agents/skills/legacy-payment-oncall"
            self.skill_path = self.skill_dir / "SKILL.md"
            self.session = None
            self._save("run.json", {"mode": "material-replay" if self.offline else "live",
                                    "model": None if self.offline else "glm-5.2"})
            self._phase_start = time.monotonic()
            self._phase_requests = 0
            # 直接执行的代码格报错时，也释放本次连接。
            from IPython import get_ipython
            self._ipython = get_ipython()
            if self._ipython:
                self._ipython.events.register("post_run_cell", self._on_cell_finished)
        except BaseException:
            self.close()
            raise

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
        display(HTML("<div class='act-result'><table style='width:100%;table-layout:fixed'><thead><tr>" + head + "</tr></thead><tbody>" + body
                     + "</tbody></table><p>" + clean(note) + "</p></div>"))

    def _on_cell_finished(self, result):
        if result.error_before_exec or result.error_in_exec:
            self.close()

    def _phase(self, name):
        self.metrics.append({"act": name, "seconds": round(time.monotonic() - self._phase_start, 3),
                             "requests": len(self.lab.requests) - self._phase_requests if self.lab else 0,
                             "turns_total": len(self.lab.results) if self.lab else 0})
        self._save("acts.json", self.metrics)
        self._phase_start = time.monotonic()
        self._phase_requests = len(self.lab.requests) if self.lab else 0

    @staticmethod
    def _receipt(result):
        return {"exit_code": result.returncode, "output": result.stdout.strip(), "stderr": result.stderr}

    def record_initialization(self, result):
        self._save("init.json", self._receipt(result))
        self._save("initialized-files.json", sorted(str(p.relative_to(self.project))
            for p in self.skill_dir.rglob("*") if p.is_file()))

    def show_creation(self, validation, session):
        self.session = session
        if self.lab:
            shutil.copyfile(self.skill_path, self.runtime / "model-draft-SKILL.md")
            self._save("model-draft-validation.json", self._receipt(validation))
        else:
            self._save("replay-draft-validation.json", self._receipt(validation))
        self._table(["这次整理的内容", "进入 Skill 的方法", "留在原 Thread 的现场"], [
                ("模块边界", "独立发布、独立依赖；不套用新支付平台设定", "本次实例与版本"),
                ("查询入口", "legacy_release_history / legacy_payment_logs / legacy_payment_db_pool / legacy_payment_runtime_config", "本次查询参数与返回值"),
                ("指标含义", "活跃连接、等待连接、连接池使用情况的口径", "连接池上限 60、使用率 100%"),
                ("调查顺序", "从最早异常样本取得 request_id，再相互核对日志、发布、指标与配置", "pay-old-001；支付重试未归还连接"),
                ("init_skill.py", "legacy-payment/.agents/skills/legacy-payment-oncall/：SKILL.md、agents/openai.yaml", "未传 --resources；没有增加资源目录"),
                ("quick_validate.py · " + ("本次模型提炼" if self.lab else "案例材料回放"),
                 validation.stdout.strip(), "结构合格，正文仍需逐条审阅"),
            ], "Skill Creator 指导经验提炼与文件创建；结构校验没有证明入口真实、步骤合理，也没有检查旧根因是否成了长期规则。")
        self._phase("第一幕")

    def record_case_draft(self, validation):
        self._save("first-draft-validation.json", self._receipt(validation))

    def show_review(self, validation):
        original = (self.materials / "first-draft-SKILL.md").read_text(encoding="utf-8")
        revised = (self.materials / "reviewed-SKILL.md").read_text(encoding="utf-8")
        actual = self.skill_path.read_text(encoding="utf-8")
        self._save("review-diff.json", {"before": original, "after": actual,
                                      "matches_article_version": actual == revised})
        self._save("reviewed-validation.json", self._receipt(validation))
        self._table(["逐条核对", "专栏案例初稿", "本次修改后的文件"], [
                ("根因", "支付接口错误率上升时，优先检查新版本重试路径是否耗尽数据库连接池。",
                 "历史事故只能提供检查线索，不能作为当前根因。" if "历史事故只能提供检查线索，不能作为当前根因。" in actual else actual),
                ("实例参数", "检查 legacy-payment-03 实例", "重新确认本次实例与版本范围" if "legacy-payment-03" not in actual else "旧实例名仍在文件中"),
                ("SKILL.md", "可复用的方法中夹带了上一场答案", actual),
                ("quick_validate.py", "Skill is valid!", validation.stdout.strip()),
            ], "修改前后结构都合格。Agent 用脚本守住文件结构；方法是否值得长期保留，要由熟悉模块的同学逐条把关。")
        self._phase("第二幕")

    @staticmethod
    def _queried(requests, entry):
        # 只从真实工具调用及其成功回执中确认查询，不把用户提示词当作读取记录。
        calls, outputs = {}, {}
        for request in requests:
            for item in request.get("input", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "function_call":
                    calls[item.get("call_id")] = item.get("arguments", "")
                elif item.get("type") == "function_call_output":
                    outputs[item.get("call_id")] = str(item.get("output", ""))
        return any("query.py" in args and entry in args and "Process exited with code 0" in outputs.get(cid, "")
                   for cid, args in calls.items())

    def show_investigation(self, result, fresh):
        requests = self.lab.requests[self._phase_requests:] if self.lab else []
        try:
            if self.lab:
                self._save("fresh-thread.json", {"thread_id": fresh.id, "previous_thread_id": self.session.id})
            report = result.final_response if result else REPORT
            (self.runtime / "new-alert-report.md").write_text(report, encoding="utf-8")
            data = json.loads((self.current / "current.json").read_text(encoding="utf-8"))
            rows = []
            observations = [
                ("legacy_payment_logs", "最早二十条异常请求访问结算服务时，均发生相同旧域名的解析失败", "调查范围收窄到模块与结算服务的调用链路"),
                ("legacy_release_history", "告警前二十四小时没有发布；当前版本已稳定运行一天", "暂时排除应用发布"),
                ("legacy_payment_db_pool", "活跃连接 18/200，等待连接 0，使用率 9%", "上一场连接池耗尽没有重现"),
                ("legacy_payment_runtime_config", "结算服务当天启用新地址；模块仍配置已停用的旧域名", "与日志和变更记录共同支持配置未同步"),
            ]
            access = {entry: self._queried(requests, entry) for entry, _, _ in observations}
            self._save("new-alert-reads.json", {"mode": "material-replay" if self.offline else "live", "queried": access,
                                               "sample_request_ids": [r["request_id"] for r in data["legacy_payment_logs"]]})
            for entry, observation, meaning in observations:
                status = "案例材料" if self.offline else "本次已查询" if access[entry] else "未确认查询"
                rows.append((entry + " · " + status, observation, meaning))
            # 模型报告原文留存；读者看到的是对当前材料的定稿解读。
            rows.append(("本幕解读", REPORT, "按当前现场判断；结论限于抽查请求及本次材料"))
            self._table(["当前入口", "本次材料与报告", "怎样改变判断"], rows,
                "新 Thread 从模块自己的入口取数，根因来自这次重新读取的日志、指标与配置。旧 Thread 留下方法形成的过程，日常调查不必再从那里找起点。")
        except BaseException:
            self.close()
            raise
        finally:
            self._phase("第三幕")
            self.close()

    def close(self):
        if self.closed:
            return
        if getattr(self, "_ipython", None):
            self._ipython.events.unregister("post_run_cell", self._on_cell_finished)
        if self.lab:
            self.lab.close()
        self.closed = True
        self._save("cleanup.json", {"closed": self.closed,
            "proxy_returncode": self.lab.proxy.poll() if self.lab and self.lab.proxy else None,
            "errors": self.lab._cleanup_errors if self.lab else []})
