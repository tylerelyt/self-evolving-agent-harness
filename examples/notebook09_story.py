"""09 讲的三层材料、边界回放、配置核对与一次性审批；原生调用留在 Notebook。"""
from copy import deepcopy
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

STAGES = ["最省事的方案，为什么不能让它自己改？", "计划里已经说“不要改”，为什么还不够？", "怎样把计划真正变成权限？"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(folder):
    return {str(p.relative_to(folder)): digest(p) for p in sorted(folder.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


class IsolationApprovalStory:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.offline = bool(os.environ.get("LAB_VALIDATION_RUN_ID"))
        self.closed, self.lab, self._ipython = False, None, None
        self.stages, self.observations, self.replay_events, self.approvals = [], {}, [], []
        self.submitted_policies = []
        self.approved = False
        self._phase_start = time.monotonic()
        if self.offline:
            run_id = os.environ["LAB_VALIDATION_RUN_ID"]
            if not re.fullmatch(r"nb09-v1-[a-zA-Z0-9-]{4,70}", run_id):
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
            for name in ("services", "tests", "lib"):
                shutil.copytree(self.materials / name, self.work / name)
            (self.work / ".git").mkdir(exist_ok=True)
            self.outputs = self.work / "services/payment/legacy_adapter"
            self.tests = self.work / "tests/payment/legacy_adapter"
            self.inputs = self.work / "lib/database"
            self.adapter, self.regression, self.protected = self.outputs / "adapter.py", self.tests / "test_adapter.py", self.inputs / "pool.py"
            self.input_hash = digest(self.protected)
            self.plan = (self.materials / "PLAN.md").read_text(encoding="utf-8")
            self.planning_task = (f"只读查看 {self.adapter}、{self.protected}、{self.regression}，说明修改面和验证方法，不修改文件。\n" + self.plan)
            self.proposal_task = ("根据这份已确认计划，只输出完整 JSON，不调用工具、不执行修改。顶层 cwd、approvalPolicy、sandboxPolicy；"
                f"cwd={self.outputs}，approvalPolicy=never；sandboxPolicy 的 type=workspaceWrite，writableRoots 仅含 {self.tests}，"
                "networkAccess=false，excludeTmpdirEnvVar=true，excludeSlashTmp=true；不得添加其他字段或路径。")
            self.readonly_config = {"cwd": str(self.work), "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly"}}
            self.expected_config = {"cwd": str(self.outputs), "approvalPolicy": "never", "sandboxPolicy": {
                "type": "workspaceWrite", "writableRoots": [str(self.tests)], "networkAccess": False,
                "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}}
            self.permission = None
            self.allowed, self.extra_allowed = self.outputs / "allowed.txt", self.tests / "allowed.txt"
            self.scoped_command = (f"printf allowed > {shlex.quote(str(self.allowed))} && "
                f"printf tests-allowed > {shlex.quote(str(self.extra_allowed))} && "
                f"printf changed > {shlex.quote(str(self.protected))}")
            self.readonly_probe = self.outputs / "readonly-probe.txt"
            self.readonly_command = f"printf changed > {shlex.quote(str(self.readonly_probe))}"
            self.selfcheck_allowed, self.selfcheck_denied = self.outputs / ".boundary-check.txt", self.inputs / ".boundary-check.txt"
            self.selfcheck_command = (f"printf selfcheck > {shlex.quote(str(self.selfcheck_allowed))} && "
                f"printf forbidden > {shlex.quote(str(self.selfcheck_denied))}")
            # 审批另开独立任务，不扩大 Hotfix 的 never 权限。
            self.script = self.outputs / "write_report.py"
            self.script.write_text("from pathlib import Path\nPath('report.txt').write_text('已批准的构造报告', encoding='utf-8')\n", encoding="utf-8")
            self.script_hash = digest(self.script)
            self.report = self.outputs / "report.txt"
            self.before = snapshot(self.work)
            self.save("inputs-before.json", self.before)
            self.save("run.json", {"mode": "material-replay" if self.offline else "live",
                "model": None if self.offline else "glm-5.2", "instance": "legacy-payment-demo-09（构造值）",
                "values": "三处调用、30 秒和 5 秒均为构造值；不产生真实等待"})
            self.save("replay-events.json", [])
            self.save("observations.json", {})
            if self.lab:
                self._proxy = self.lab.proxy
                self._model_proc = getattr(self.lab.codex._client, "_proc", None)
                self._ports = [int(self._proxy.args[self._proxy.args.index("--port") + 1]), self.lab.observer.server_port]
            from IPython import get_ipython
            self._ipython = get_ipython()
            if self._ipython:
                self._ipython.events.register("post_run_cell", self._on_cell_finished)
        except BaseException:
            self.close()
            raise

    def save(self, name, value):
        if self.lab:
            self.lab._write_json(name, value)
        else:
            (self.runtime / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def clean(self, value):
        return self.lab.clean(value) if self.lab else str(value).replace(str(self.runtime), "<本次运行目录>")

    def table(self, headers, rows, note):
        from IPython.display import HTML, display
        def cell(v):
            return escape(self.clean(v))
        head = "".join("<th>" + cell(x) + "</th>" for x in headers)
        body = "".join("<tr>" + "".join("<td style='white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top;font-size:14px;line-height:1.6'>" + cell(x) + "</td>" for x in row) + "</tr>" for row in rows)
        display(HTML("<div class='stage-result'><table style='width:100%;table-layout:fixed;text-align:left'>"
            "<thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table><p>" + cell(note) + "</p></div>"))

    def phase(self, index):
        self.stages.append({"title": STAGES[index], "seconds": round(time.monotonic() - self._phase_start, 3)})
        self.save("stages.json", self.stages)
        self._phase_start = time.monotonic()

    def config_from_materials(self):
        text = (self.materials / "permission-proposal.json").read_text(encoding="utf-8")
        return json.loads(text.replace("{{ADAPTER}}", str(self.outputs)).replace("{{TESTS}}", str(self.tests)))

    def review_scope(self, proposal):
        # 教学批准来自题设，模型提出的配置必须和范围逐项相等。
        if proposal != self.expected_config:
            self.save("rejected-config.json", proposal)
            raise ValueError("配置与已确认计划不一致，未应用；须由人重新核对。")
        reviewed = deepcopy(proposal)
        self.save("reviewed-config.json", {"proposal": proposal, "approved": reviewed,
            "reviewer": "独立代码核对预先确认的教学范围；生产中必须由人或独立审批系统确认"})
        if self.permission is not None and self.permission != reviewed:
            raise ValueError("执行中的权限不能随方案改变。")
        self.permission = reviewed
        return deepcopy(reviewed)

    def replay_write(self, path, content, permission):
        """离线教学检查，只控制此回放函数；不能隔离任意 Shell 或子进程。"""
        path = Path(path).resolve()
        if not self.offline or not path.is_relative_to(self.work):
            raise ValueError("回放写入只能用于本次离线 work。")
        policy = permission["sandboxPolicy"]
        roots = [Path(permission["cwd"]).resolve(), *map(lambda p: Path(p).resolve(), policy.get("writableRoots", []))]
        allowed = policy["type"] == "workspaceWrite" and any(path.is_relative_to(root) for root in roots)
        event = {"source": "offline-boundary-replay", "path": str(path.relative_to(self.work)),
                 "allowed": allowed, "reason": "显式可写范围" if allowed else "默认拒绝：不在可写范围"}
        if allowed:
            path.write_text(content, encoding="utf-8")
        self.replay_events.append(event)
        self.save("replay-events.json", self.replay_events)
        return allowed

    def replay_hotfix(self, permission):
        self.replay_write(self.allowed, "allowed", permission)
        self.replay_write(self.extra_allowed, "tests-allowed", permission)
        rejected = not self.replay_write(self.protected, "def acquire(wait_seconds=5):\n    return {'wait_seconds': wait_seconds}\n", permission)
        patch = json.loads((self.materials / "hotfix-replay.json").read_text(encoding="utf-8"))
        for path in (self.adapter, self.regression):
            if not self.replay_write(path, patch[str(path.relative_to(self.work))], permission):
                raise RuntimeError("教学补丁未能在允许范围写入。")
        return {"source": "offline-boundary-replay", "denied": rejected, "receipts": []}

    def observe(self, name, start, command, target):
        from notebook_support import command_receipts, denied_write
        receipts = command_receipts(self.lab.requests[start:])
        value = {"source": "native-sandbox", "denied": denied_write(receipts, command, target), "receipts": receipts}
        self.observations[name] = value
        self.save("observations.json", self.observations)
        return value

    def show_execution(self, result, start, permission):
        self.observations["hotfix"] = self.observe("hotfix", start, self.scoped_command, self.protected) if self.lab else self.replay_hotfix(permission)
        self.save("observations.json", self.observations)
        self.table(["动作", "实际文件结果", "边界依据"], [
            ("cwd 适配层", self.allowed.read_text() if self.allowed.exists() else "未生成", "工作目录本身可写"),
            ("对应测试", self.extra_allowed.read_text() if self.extra_allowed.exists() else "未生成", "writableRoots 只额外放行这一处"),
            ("一行共享改法", "被拒绝" if self.observations["hotfix"]["denied"] else "未取得有效拒绝", "未点名允许的目录默认只读"),
            ("共享默认值", "哈希未变" if digest(self.protected) == self.input_hash else "已经改变", "30 秒为构造值"),
            ("适配层补丁", self.adapter.read_text(), "三个调用各自显式传入 5 秒（构造值）"),
        ], "离线目录边界回放：文件真实写入，但拒绝来自教学函数，不证明原生 Sandbox 生效。" if self.offline else
           "表格依据真实文件与匹配的命令拒绝回执；Sandbox 启动失败不算越界写入被拒。")
        self.phase(0)

    def record_readonly(self, result, start):
        if self.lab:
            value = self.observe("readonly", start, self.readonly_command, self.readonly_probe)
        else:
            value = {"source": "offline-boundary-replay", "denied": not self.replay_write(self.readonly_probe, "changed", self.readonly_config), "receipts": []}
        self.observations["readonly"] = value
        self.save("observations.json", self.observations)

    def show_plan_boundary(self, plan, regression):
        widened = deepcopy(self.permission)
        widened["sandboxPolicy"]["writableRoots"].append(str(self.inputs))
        try:
            self.review_scope(widened)
        except ValueError:
            refused = True
        else:
            refused = False
        self.save("plan-boundary.json", {"plan": self.plan, "model_plan": plan.final_response if plan else None,
            "expanded_config_rejected": refused, "permission_unchanged": self.permission == self.expected_config,
            "regression": {"exit_code": regression.returncode, "stdout": regression.stdout, "stderr": regression.stderr}})
        self.table(["负责什么", "本次内容与结果", "为什么不能互相代替"], [
            ("计划 / Skill / 项目规则", plan.final_response if plan else self.plan, "指导模型怎样判断；临场方案可以变化"),
            ("只读规划", "写入被拒" if self.observations["readonly"]["denied"] else "未取得有效拒绝", "规划阶段不能提前实施修改"),
            ("Sandbox 执行边界", json.dumps(self.permission, ensure_ascii=False, indent=2), "权限不随临场判断漂移；任意子进程也应由执行环境拦截"),
            ("自行扩大可写根", "配置被拒，原范围未变" if refused else "核对失败", "生成者不能自己批准共享目录"),
            ("独立参数回归", regression.stdout + regression.stderr, "退出码 " + str(regression.returncode) + "；只核对传参，不模拟生产等待"),
        ], "offline 只检查回放函数与配置；对 Shell 及子进程的操作系统隔离仍待 live 原生 Sandbox 核验。" if self.offline else
           "修改成功、越界被拒和代码正确分别检查；模型说完成不能代替独立回归。")
        self.phase(1)

    def record_selfcheck(self, result, start):
        if self.lab:
            value = self.observe("selfcheck", start, self.selfcheck_command, self.selfcheck_denied)
        else:
            self.replay_write(self.selfcheck_allowed, "selfcheck", self.permission)
            value = {"source": "offline-boundary-replay", "denied": not self.replay_write(self.selfcheck_denied, "forbidden", self.permission), "receipts": []}
        value["allowed_exists"] = self.selfcheck_allowed.is_file() and self.selfcheck_allowed.read_text() == "selfcheck"
        value["denied_absent"] = not self.selfcheck_denied.exists()
        self.observations["selfcheck"] = value
        # 只撤回本次适配层探针；共享探针本来就不应存在。
        self.selfcheck_allowed.unlink(missing_ok=True)
        self.save("observations.json", self.observations)

    def replay_approval(self, review_command):
        if not self.offline:
            raise ValueError("审批回放只用于离线。")
        params = {"command": "python3 write_report.py", "cwd": str(self.outputs)}
        method = "item/commandExecution/requestApproval"
        review_command(method, params)
        absent_after_denial = not self.report.exists()
        self.approved = True  # 构造材料给独立例外一次批准；不是主 Hotfix 扩权。
        if review_command(method, params)["decision"] == "accept":
            run = subprocess.run([sys.executable, "-B", str(self.script)], cwd=self.outputs, check=True, capture_output=True, text=True)
        repeated = review_command(method, params)["decision"]
        self.save("approval-result.json", {"source": "offline-callback-replay", "absent_after_denial": absent_after_denial,
            "exit_code": run.returncode, "repeat_decision": repeated, "consumed": not self.approved, "thread_ids": []})

    def finish(self):
        try:
            approval = json.loads((self.runtime / "approval-result.json").read_text())
            self.table(["谁负责", "实际结果", "边界"], [
                ("Agent 配置草案 · 构造材料" if self.offline else "Agent 生成配置", json.dumps(self.permission, ensure_ascii=False, indent=2), "根据已确认计划生成，不能自行启用"),
                ("人审配置的教学核对", "cwd + 一个额外根；不含 lib；networkAccess=false", "生产中由人或独立系统确认后应用；模型无自批权限"),
                ("临时文件自检", json.dumps(self.observations["selfcheck"], ensure_ascii=False, indent=2), "适配层成功，共享目录失败；适配层探针已撤回"),
                ("一次性例外 · 独立任务", " → ".join(a["decision"] for a in self.approvals), "先拒绝、一次批准、再次拒绝；没有改变 Hotfix 权限"),
                ("构造报告", self.report.read_text() if self.report.exists() else "未生成", "脚本哈希未变；批准已消费：" + str(approval["consumed"])),
                ("litellm 代理层", "offline 未启动，模型请求为零" if self.offline else "GLM-5.2 的本地协议适配与请求记录", "适配模型协议；不代替 Sandbox 或人的权限决定"),
            ], "审批回调为离线调用，报告脚本真实执行；不是 SDK 请求审批的实测。Agent 可以改变做法，权限仍由人掌握。" if self.offline else
               "审批由独立客户端回调作决定，匹配后只消费一次；不防范核对与执行之间的并发替换。")
            self.phase(2)
        finally:
            self.close()

    def _on_cell_finished(self, result):
        if result.error_before_exec or result.error_in_exec:
            self.close()

    def close(self):
        if self.closed:
            return
        errors, ports = [], {}
        if self._ipython:
            self._ipython.events.unregister("post_run_cell", self._on_cell_finished)
        try:
            if self.lab:
                self.lab.close()
        except Exception as exc:
            errors.append(self.clean(exc))
        finally:
            if self.lab and hasattr(self, "_ports"):
                for port in self._ports:
                    with socket.socket() as sock:
                        sock.settimeout(1)
                        ports[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
            self.approved = False
            self.closed = True
            self.save("inputs-after.json", snapshot(self.work))
            self.save("cleanup.json", {"closed": self.closed, "model_started": self.lab is not None,
                "proxy_returncode": self._proxy.poll() if hasattr(self, "_proxy") else None,
                "model_returncode": self._model_proc.poll() if getattr(self, "_model_proc", None) else None,
                "policy_returncode": self._policy_proc.poll() if getattr(self, "_policy_proc", None) else None,
                "approval_returncode": self._approval_proc.poll() if getattr(self, "_approval_proc", None) else None,
                "ports_open": ports, "errors": errors + (self.lab._cleanup_errors if self.lab else []),
                "selfcheck_removed": not self.selfcheck_allowed.exists() if hasattr(self, "selfcheck_allowed") else None})
        if errors:
            raise RuntimeError("本次连接关闭失败：" + "；".join(errors))
