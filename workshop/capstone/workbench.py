"""Local On-call workbench over real HTTP, Hermes and learning records."""

from collections import deque
from contextlib import closing
import difflib
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "patrol"))
from environment import LoopbackHTTPServer, SERVICES, classify, get_snapshot, inject_fault, load_json, write_json
from hermes_runtime import ONCALL_SKILL
from patrol import checkpoint, connect, decode_report, incidents

ALIASES = ("instance-a", "instance-b")
JOB_LABELS = {"question": "追问", "chat": "SRE 对话", "memory_review": "Memory 复盘", "memory_recall": "新会话召回"}
from lab.stages import STAGES, stage_status


def optional_json(path, default=None):
    try:
        return load_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

class Workbench:
    def __init__(self, root, implementation_file, *, live=False):
        self.root = Path(root).resolve()
        self.implementation_file = Path(implementation_file).resolve()
        self.live = live
        self.lock = threading.RLock()
        self.environments = {}
        self.agent = None
        self.active_run = None
        self.check_process = None
        self.current_check = None
        self.running_sha = None
        self.history = {alias: {service: deque(maxlen=90) for service in SERVICES} for alias in ALIASES}
        self.root.mkdir(parents=True, exist_ok=True)
        self.record_path = self.root / "workbench.json"
        self.record = optional_json(self.record_path, {"events": [], "checks": [], "jobs": []})
        self.record.setdefault("jobs", [])
        previous = self.record.get("active_run")
        if isinstance(previous, str) and Path(previous).name == previous:
            run = self.root / "runs" / previous
            if (run / "service.json").is_file():
                self.active_run = run
        self.background = {}
        for check in self.record["checks"]:
            if check.get("status") == "running":
                check.update(status="interrupted", finished_at=time.time())
        for job in self.record["jobs"]:
            if job.get("status") == "running":
                job.update(status="interrupted", finished_at=time.time())

    def __enter__(self):
        try:
            for alias in ALIASES:
                directory = self.root / "environments" / alias
                directory.mkdir(parents=True, exist_ok=True)
                if (directory / "environment.json").exists():
                    raise RuntimeError(f"An environment already owns {directory}. Choose a fresh workbench directory.")
                self.environments[alias] = self.spawn(
                    [sys.executable, "-B", str(HERE / "patrol/environment.py"), "--state", str(directory), "serve"],
                    directory / "environment.log",
                )
                deadline = time.monotonic() + 25
                while True:
                    try:
                        if get_snapshot(directory, "payments")["source"] == "live_lab_http_requests":
                            break
                    except (OSError, ValueError, KeyError, RuntimeError):
                        pass
                    if self.environments[alias].poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f"{alias} did not start its HTTP environment")
                    time.sleep(.1)
            self.event("environment_ready", "两套服务环境已就绪，Agent 等待接入")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_args):
        self.close()

    @staticmethod
    def spawn(command, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as output:
            return subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)

    @staticmethod
    def stop(process):
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def close(self):
        with self.lock:
            for process in self.background.values():
                self.stop(process)
            self.stop(self.check_process)
            self.stop(self.agent)
            for process in self.environments.values():
                self.stop(process)
            self.environments.clear()

    def event(self, kind, message, **details):
        self.record["events"].append({"id": uuid4().hex, "time": time.time(), "kind": kind,
                                      "message": message, **details})
        self.record["events"] = self.record["events"][-200:]
        write_json(self.record_path, self.record)

    def refresh_check(self):
        if self.check_process is None or self.check_process.poll() is None:
            return
        if self.current_check["status"] == "running":
            code = self.check_process.returncode
            evidence = optional_json(self.root / "checks" / self.current_check["id"] / "check.json", {})
            passed = code == 0 and evidence.get("status") == "passed"
            self.current_check.update(status="passed" if passed else "failed", returncode=code,
                                      finished_at=time.time())
            self.event("check_finished", f"第 {self.current_check['stage']} 阶段验收"
                       + ("通过" if passed else "失败"), check_id=self.current_check["id"])

    def acknowledge_recovered_alerts(self):
        """Keep verified recoveries acknowledged when code starts a new patrol DB."""
        from environment import acknowledge_resolution, resolution_for_alert
        from workbench_memory import recovered_observation, verified_branch

        acknowledged, errors, scopes = [], [], {}
        for status in self.run_paths():
            for alias in ALIASES:
                worker = status.parent / "patrols" / alias
                if not (worker / "patrol.sqlite").is_file():
                    continue
                environment = self.root / "environments" / alias
                with closing(connect(worker, readonly=True)) as db:
                    db.execute("BEGIN")
                    for service in SERVICES:
                        for record in incidents(db, service):
                            if record["recovered_inspection_id"] is None:
                                continue
                            incident = record["incident_id"]
                            try:
                                if not re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident):
                                    raise ValueError("Recovered incident has an invalid monitoring ID")
                                key = alias, service
                                if key not in scopes:
                                    scopes[key] = get_snapshot(environment, service)
                                current = scopes[key]
                                original = json.loads(record["observation"])
                                if (current["source"] != "live_lab_http_requests"
                                        or current["service"] != service):
                                    raise ValueError("Current HTTP scope does not match the service")
                                if original["environment_id"] != current["environment_id"]:
                                    continue
                                alert_path = environment / "alerts" / service / f"{incident}.json"
                                if not alert_path.is_file():
                                    alert_path = environment / f"{service}.alert.json"
                                alert = load_json(alert_path)
                                if (alert["incident_id"] != incident or alert["service"] != service
                                        or alert["environment_id"] != original["environment_id"]):
                                    raise ValueError("Recovered incident differs from the monitoring event")
                                if resolution_for_alert(environment, alert) is not None:
                                    continue
                                inbox = worker / "pages" / incident
                                branch = load_json(inbox / "investigation.json")
                                page = load_json(inbox / "page.json")
                                verified_branch(worker, record, branch)
                                if (page["incident_id"] != incident or page["session_id"] != branch["session_id"]
                                        or page["report_item_id"] != branch["report_item_id"]
                                        or page["report"] != branch["report"]
                                        or page["scope"] != f"{original['environment_id']}/{service}"):
                                    raise ValueError("Recovery page does not cite the verified investigation")
                                recovered = recovered_observation(db, record, page)
                                last = dict(db.execute("SELECT * FROM inspections WHERE id = ?",
                                                       (record["recovered_inspection_id"],)).fetchone())
                                candidates = db.execute(
                                    """SELECT * FROM inspections WHERE service = ? AND environment = ? AND region = ?
                                       AND status = 'healthy' AND checked_at < ? ORDER BY checked_at DESC""",
                                    (last["service"], last["environment"], last["region"], recovered["window_start"]))
                                previous = None
                                for row in candidates:
                                    observation = json.loads(row["observation"])
                                    if (observation["environment_id"] == original["environment_id"]
                                            and page["paged_at"] < observation["window_start"]
                                            and observation["window_end"] < recovered["window_start"]
                                            and classify(observation) == "healthy" and observation["error_rate"] == 0):
                                        previous = dict(row)
                                        break
                                if previous is None:
                                    raise ValueError("Recovery is missing its earlier independent healthy window")
                                database = worker / "hermes" / "state.db"
                                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as sessions:
                                    sessions.row_factory = sqlite3.Row
                                    initial = sessions.execute("SELECT role, content FROM messages WHERE id = ? AND session_id = ?",
                                                               (record["report_item_id"], record["session_id"])).fetchone()
                                    completed = sessions.execute("SELECT timestamp FROM messages WHERE id = ? AND session_id = ?",
                                                                 (branch["report_item_id"], branch["session_id"])).fetchone()
                                    if (initial is None or initial["role"] != "assistant"
                                            or decode_report(initial["content"]).get("incident_id") != incident
                                            or completed is None or page["paged_at"] < completed["timestamp"]
                                            or page["paged_at"] < branch["probes"][-1]["checked_at"]):
                                        raise ValueError("Recovery page predates the actual incident investigation")
                                    windows = []
                                    for inspection in (previous, last):
                                        observation = json.loads(inspection["observation"])
                                        tool = sessions.execute("SELECT role, tool_name, content FROM messages WHERE id = ? AND session_id = ?",
                                                                (inspection["evidence_item_id"], inspection["session_id"])).fetchone()
                                        report = sessions.execute("SELECT role, content FROM messages WHERE id = ? AND session_id = ?",
                                                                  (inspection["report_item_id"], inspection["session_id"])).fetchone()
                                        returned = json.loads(tool["content"]) if tool is not None else {}
                                        returned.pop("history_available", None)
                                        if (tool is None or tool["role"] != "tool" or tool["tool_name"] != "service_snapshot"
                                                or returned != observation or report is None or report["role"] != "assistant"
                                                or decode_report(report["content"]).get("status") != "healthy"
                                                or not inspection["evidence_item_id"] < inspection["report_item_id"]):
                                            raise ValueError("Recovery window differs from its persisted Hermes tool receipt")
                                        windows.append({"inspection_id": inspection["id"], "session_id": inspection["session_id"],
                                                        "evidence_item_id": inspection["evidence_item_id"],
                                                        "report_item_id": inspection["report_item_id"], "observation": observation})
                                evidence = {"patrol_database": str(worker / "patrol.sqlite"), "session_database": str(database),
                                            "source_session_id": record["session_id"], "source_report_item_id": record["report_item_id"],
                                            "investigation_session_id": branch["session_id"],
                                            "investigation_report_item_id": branch["report_item_id"],
                                            "page_receipt": str(inbox / "page.json"), "page_sha256": file_hash(inbox / "page.json"),
                                            "paged_at": page["paged_at"], "recovered_inspection_id": last["id"],
                                            "recovery_windows": windows}
                                path = acknowledge_resolution(environment, alert, evidence)
                                acknowledged.append({"instance": alias, "incident_id": incident, "receipt": str(path)})
                            except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError, sqlite3.Error) as exc:
                                errors.append({"instance": alias, "incident_id": incident, "error": str(exc)})
        return {"acknowledged": acknowledged, "errors": errors}

    def start_agent(self):
        if self.live and not os.environ.get("GLM_API_KEY", "").strip():
            raise ValueError("请先配置 GLM_API_KEY，再启动 SRE Agent。")
        stages = stage_status(self.implementation_file)
        stage = 0
        for item in stages:
            if not item["implemented"]:
                break
            stage = item["number"]
        if stage == 0:
            raise ValueError("先完成 patrol_config，再启动巡检 Agent。")
        current_sha = file_hash(self.implementation_file)
        if self.agent is not None and self.agent.poll() is None:
            if current_sha == self.running_sha:
                return {"status": "running", "run": str(self.active_run)}
            self.stop(self.agent)
        recovery = self.acknowledge_recovered_alerts()
        if recovery["acknowledged"] or recovery["errors"]:
            self.event("alerts_acknowledged", f"已确认 {len(recovery['acknowledged'])} 起历史事故恢复",
                       **recovery)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        run = self.root / "runs" / run_id
        command = [sys.executable, "-B", str(HERE / "oncall_service.py"), "--out", str(run),
                   "--implementation", str(self.implementation_file),
                   "--environment-a", str(self.root / "environments/instance-a"),
                   "--environment-b", str(self.root / "environments/instance-b")]
        if stage == 1:
            command.append("--patrol-only")
        elif stage < 4:
            command.append("--no-learning")
        if self.live:
            command.append("--live")
        self.agent = self.spawn(command, self.root / "runs" / f"{run_id}.log")
        self.active_run = run
        self.running_sha = current_sha
        self.record.update(active_run=run_id, running_sha=current_sha, active_stage=stage)
        self.event("agent_started", f"已应用当前代码，启动第 {stage} 阶段 Agent", run_id=run_id,
                   implementation_sha256=current_sha)
        return {"status": "starting", "run_id": run_id, "stage": stage}

    def start_check(self, stage):
        if type(stage) is not int or not 1 <= stage <= 4:
            raise ValueError("Stage must be between 1 and 4")
        self.refresh_check()
        if self.check_process is not None and self.check_process.poll() is None:
            raise ValueError("已有验收正在执行，完成后再开始下一次。")
        identifier = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        directory = self.root / "checks" / identifier
        command = [sys.executable, "-B", str(HERE / "lab/check.py"), "--stage", str(stage),
                   "--out", str(directory), "--implementation", str(self.implementation_file)]
        if self.live:
            command.append("--live")
        self.check_process = self.spawn(command, self.root / "checks" / f"{identifier}.log")
        self.current_check = {"id": identifier, "stage": stage, "status": "running", "started_at": time.time(),
                              "implementation_sha256": file_hash(self.implementation_file)}
        self.record["checks"].append(self.current_check)
        self.event("check_started", f"开始第 {stage} 阶段验收", check_id=identifier)
        return self.current_check

    def check_records(self):
        by_id = {item["id"]: dict(item) for item in self.record["checks"]}
        for path in (self.root / "checks").glob("*/check.json"):
            evidence = optional_json(path)
            if not evidence:
                continue
            item = by_id.setdefault(path.parent.name, {"id": path.parent.name})
            stage = evidence.get("stage")
            stage = stage if stage is not None else evidence.get("part", 0)
            sha = evidence.get("implementation", {}).get("sha256")
            item.update(stage=stage, status=evidence.get("status", "unknown"), evidence=evidence,
                        implementation_sha256=sha, finished_at=evidence.get("completed_at"))
            required = set(range(1, stage + 1)) if type(stage) is int and 1 <= stage <= 4 else set()
            passed = {check.get("stage") for check in evidence.get("checks", [])
                      if check.get("status") == "passed" and check.get("evidence")
                      and all((path.parent / filename).is_file() for filename in check["evidence"])}
            item["evidence_valid"] = (evidence.get("schema") == "oncall-workshop-check-v1"
                                      and evidence.get("status") == "passed" and bool(required)
                                      and required <= passed)
            item.setdefault("started_at", path.stat().st_mtime)
        return sorted(by_id.values(), key=lambda item: item["started_at"])

    def refresh_jobs(self):
        for job in self.record["jobs"]:
            process = self.background.get(job["id"])
            if process is not None and job["status"] == "running" and process.poll() is not None:
                successful = process.returncode == 0
                if job["kind"] == "chat":
                    result = optional_json(self.root / "chat" / job["conversation"] / "last-result.json", {})
                    successful = successful and result.get("status") == "completed"
                job.update(status="completed" if successful else "failed",
                           returncode=process.returncode, finished_at=time.time())
                self.event("job_finished", JOB_LABELS[job["kind"]] + ("完成" if successful else "失败"),
                           job_id=job["id"])

    def add_job(self, kind, identifier, command, log, **details):
        self.refresh_jobs()
        if any(job["kind"] == kind and job["status"] == "running"
               and (kind != "chat" or job.get("conversation") == details.get("conversation"))
               for job in self.record["jobs"]):
            raise ValueError("同类任务正在执行，请等结果返回。")
        self.background[identifier] = self.spawn(command, log)
        job = {"id": identifier, "kind": kind, "status": "running", "started_at": time.time(),
               "log": str(log.relative_to(self.root)), **details}
        self.record["jobs"].append(job)
        self.event("job_started", JOB_LABELS[kind] + "已开始", job_id=identifier)
        return job

    def agent_status(self):
        if self.agent is None:
            return "offline"
        if self.agent.poll() is not None:
            return "stopped" if self.agent.returncode == 0 else "failed"
        service = optional_json(self.active_run / "service.json", {}) if self.active_run is not None else {}
        return service.get("status", "starting")

    def chat_capability(self):
        available, reason = True, None
        if self.live and not os.environ.get("GLM_API_KEY", "").strip():
            available, reason = False, "未配置模型连接。设置 GLM_API_KEY 后重新启动工作台。"
        elif self.agent_status() != "running" or self.record.get("active_stage", 0) < 1:
            available, reason = False, "SRE Agent 尚未上线。先完成巡检接入，再启动 Agent。"
        result = {"available": available, "mode": "live" if self.live else "offline",
                  "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if self.live else "local-script-model"}
        if reason:
            result["reason"] = reason
        return result

    def conversation_directory(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("请选择工作台中的对话。")
        directory = self.root / "chat" / identifier
        metadata = optional_json(directory / "conversation.json")
        if not isinstance(metadata, dict) or metadata.get("id") != identifier:
            raise ValueError("找不到这段对话。")
        return directory

    def require_chat(self):
        capability = self.chat_capability()
        if not capability["available"]:
            raise ValueError(capability["reason"])

    def new_conversation(self, title=None):
        self.require_chat()
        if title is not None and (not isinstance(title, str) or len(title) > 120):
            raise ValueError("对话标题最多 120 字。")
        identifier = uuid4().hex
        directory = self.root / "chat" / identifier
        directory.mkdir(parents=True)
        now = time.time()
        metadata = {"id": identifier, "title": (title or "新的值班会话").strip() or "新的值班会话",
                    "status": "empty", "created_at": now, "updated_at": now,
                    "mode": "live" if self.live else "local-script-model",
                    "model": self.chat_capability()["model"], "active_stage": self.record.get("active_stage", 0),
                    "implementation_sha256": self.running_sha or file_hash(self.implementation_file),
                    "active_run": self.active_run.name if self.active_run is not None else None}
        if self.active_run is not None and metadata["active_stage"] >= 2:
            metadata["skill_sha256"] = optional_json(self.active_run / "learning/state.json", {}).get("active_sha256")
        write_json(directory / "conversation.json", metadata)
        self.event("chat_created", "已打开新的值班会话", conversation=identifier)
        return {"conversation": metadata}

    def send_chat(self, body):
        self.require_chat()
        identifier = body.get("conversation")
        directory = self.conversation_directory(identifier)
        question = body.get("question", "")
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError("请填写不超过 4000 字的问题。")
        context = body.get("context", {})
        if not isinstance(context, dict) or set(context) - {"page", "instance", "service", "incident_id"}:
            raise ValueError("对话上下文只包含当前页面、实例、服务和事故。")
        if context.get("page") not in (None, "overview", "metrics", "logs", "changes", "deploys",
                                       "incidents", "memory", "learning"):
            raise ValueError("未知的业务页面。")
        if context.get("instance") not in (None, *ALIASES) or context.get("service") not in (None, *SERVICES):
            raise ValueError("请选择工作台中的实例和服务。")
        incident = context.get("incident_id")
        if incident is not None and (not isinstance(incident, str) or not re.fullmatch(
                r"lab-(orders|payments|inventory|notifications)-[0-9a-f]{12}", incident)):
            raise ValueError("未知的事故编号。")
        self.refresh_jobs()
        if any(job["kind"] == "chat" and job.get("conversation") == identifier
               and job["status"] == "running" for job in self.record["jobs"]):
            raise ValueError("Agent 正在回复这段对话，请等当前回复结束。")
        metadata = optional_json(directory / "conversation.json", {})
        if not metadata.get("session_id") and metadata.get("title") == "新的值班会话":
            metadata.update(title=question.strip()[:60], updated_at=time.time())
            write_json(directory / "conversation.json", metadata)
        job_id = uuid4().hex[:12]
        command = [sys.executable, "-B", str(HERE / "workbench_chat.py"), "--state", str(directory),
                   "--workbench", str(self.root), "--question", question.strip(),
                   "--context-json", json.dumps(context, ensure_ascii=False)]
        if not self.live:
            command.append("--offline")
        return self.add_job("chat", job_id, command, directory / "jobs" / f"{job_id}.log",
                            conversation=identifier, question=question.strip(), context=context)

    def chat(self, identifier):
        with self.lock:
            directory = self.conversation_directory(identifier)
            self.refresh_jobs()
            from workbench_chat import snapshot

            result = snapshot(directory)
            jobs = [{**job} for job in self.record["jobs"]
                    if job["kind"] == "chat" and job.get("conversation") == identifier]
            for job in jobs:
                log = self.root / job["log"]
                job["output"] = log.read_text(encoding="utf-8", errors="replace")[-12000:] if log.exists() else ""
            if jobs:
                result["conversation"]["status"] = jobs[-1]["status"]
                if jobs[-1]["status"] in ("failed", "interrupted"):
                    outcome = optional_json(directory / "last-result.json", {})
                    result["conversation"]["error"] = outcome.get("error") or jobs[-1]["output"] or "对话已中断，请重试。"
            return {**result, "jobs": jobs}

    def conversation_snapshots(self):
        conversations = []
        fields = {"id", "title", "status", "created_at", "updated_at", "mode", "model", "active_stage",
                  "implementation_sha256", "active_run", "skill_sha256", "session_id", "error"}
        for path in (self.root / "chat").glob("*/conversation.json"):
            metadata = optional_json(path)
            if not isinstance(metadata, dict) or metadata.get("id") != path.parent.name:
                continue
            result = {key: value for key, value in metadata.items() if key in fields}
            jobs = [job for job in self.record["jobs"]
                    if job["kind"] == "chat" and job.get("conversation") == result["id"]]
            if jobs:
                result["status"] = jobs[-1]["status"]
            conversations.append(result)
        return sorted(conversations, key=lambda item: item.get("updated_at", item.get("created_at", 0)), reverse=True)[:40]

    def start_memory(self, action, incident_id):
        if self.active_run is None:
            raise ValueError("先运行 Agent 并完成一次事故，再复盘或召回。")
        identifier = uuid4().hex[:12]
        command = [sys.executable, "-B", str(HERE / "workbench_memory.py"),
                   "--state", str(self.root), "--action", action,
                   "--environment-a", str(self.root / "environments/instance-a"),
                   "--environment-b", str(self.root / "environments/instance-b"),
                   "--implementation", str(self.implementation_file)]
        if incident_id:
            if not re.fullmatch(r"lab-(orders|payments|inventory|notifications)-[0-9a-f]{12}", incident_id):
                raise ValueError("Invalid incident ID")
            command.extend(("--incident-id", incident_id))
        if self.live:
            command.append("--live")
        return self.add_job("memory_" + action, identifier, command,
                            self.root / "memory/jobs" / f"{identifier}.log")

    def start_question(self, body):
        source, incident_id = body.get("source"), body.get("incident_id")
        question = body.get("question", "")
        if not isinstance(question, str):
            raise ValueError("Use a text question")
        question = question.strip()
        state = self.sources().get(source)
        if state is None or source.rsplit("/", 1)[-1] not in ALIASES:
            raise ValueError("Choose an incident from a running workbench instance")
        if not isinstance(incident_id, str) or not re.fullmatch(r"lab-(orders|payments|inventory|notifications)-[0-9a-f]{12}", incident_id):
            raise ValueError("Invalid incident ID")
        if not question or len(question) > 4000:
            raise ValueError("请填写不超过 4000 字的问题。")
        identifier = uuid4().hex[:12]
        directory = self.root / "questions" / identifier
        directory.mkdir(parents=True)
        result = directory / "result.json"
        alias = source.rsplit("/", 1)[-1]
        command = [sys.executable, "-B", str(HERE / "workbench_questions.py"),
                   "--state", str(state), "--environment-state", str(self.root / "environments" / alias),
                   "--incident-id", incident_id, "--question", question, "--out", str(result)]
        if self.live:
            command.append("--live")
        return self.add_job("question", identifier, command, directory / "question.log", source=source,
                            incident_id=incident_id, question=question, result=str(result.relative_to(self.root)))

    def memory_snapshot(self):
        from workbench_memory import memory_state

        memory = memory_state(self.root)
        memory["recalls"] = [{**entry, "source": f"memory:{entry['id']}",
                              "result": optional_json(Path(entry["evidence"]), {})}
                             for entry in memory.get("recalls", [])]
        jobs = [job for job in self.record["jobs"] if job["kind"].startswith("memory_")]
        if jobs:
            job = dict(jobs[-1])
            path = self.root / job["log"]
            job["output"] = path.read_text(encoding="utf-8", errors="replace")[-12000:] if path.exists() else ""
            memory["job"] = job
        return memory

    def question_snapshots(self):
        result = []
        for job in self.record["jobs"][-30:]:
            if job["kind"] != "question":
                continue
            answer = optional_json(self.root / job["result"], {})
            path = self.root / job["log"]
            output = path.read_text(encoding="utf-8", errors="replace")[-12000:] if path.exists() else ""
            result.append({**job, "answer": answer.get("answer"), "session_id": answer.get("session_id"),
                           "report_item_id": answer.get("report_item_id"),
                           "error": answer.get("error"), "evidence": answer, "output": output})
        return result

    def recovery_evidence(self, source, incident_id, *, require_fault=True):
        from workbench_memory import verified_branch

        if not isinstance(source, str):
            raise ValueError("恢复必须指定现场报告来源。")
        worker = self.sources().get(source)
        alias = source.rsplit("/", 1)[-1] if isinstance(source, str) else None
        if worker is None or source.startswith(("check:", "chat:", "chat-thread:")) or alias not in ALIASES:
            raise ValueError("恢复必须引用工作台现场的事故报告。")
        with closing(connect(worker, readonly=True)) as db:
            records = [row for service in SERVICES for row in incidents(db, service)
                       if row["incident_id"] == incident_id]
        if len(records) != 1:
            raise ValueError("找不到该事故的巡检与调查记录。")
        record = records[0]
        observed = json.loads(record["observation"])
        service, environment_id = observed["service"], observed["environment_id"]
        if service not in SERVICES or not re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident_id):
            raise ValueError("事故与服务范围不匹配。")
        environment = self.root / "environments" / alias
        current = get_snapshot(environment, service)
        if (current["source"] != "live_lab_http_requests" or current["service"] != service
                or current["environment_id"] != environment_id):
            raise ValueError("事故属于旧环境，不能批准当前服务的恢复。")
        inbox = worker / "pages" / incident_id
        branch, page = load_json(inbox / "investigation.json"), load_json(inbox / "page.json")
        verified_branch(worker, record, branch)
        with closing(sqlite3.connect((worker / "hermes/state.db").as_uri() + "?mode=ro", uri=True)) as sessions:
            completed = sessions.execute("SELECT timestamp FROM messages WHERE session_id=? AND id=?",
                                         (branch["session_id"], branch["report_item_id"])).fetchone()
        if (completed is None or type(page.get("paged_at")) not in (int, float)
                or not max(completed[0], branch["probes"][-1]["checked_at"]) <= page["paged_at"] <= time.time()):
            raise ValueError("Paging 记录必须晚于本次已完成的真实调查。")
        plan = branch["report"].get("recovery_plan")
        scope = f"{alias}/{environment_id}/{service}"
        if (plan != {"action": "clear_lab_fault", "approval_required": True, "verification": "two_clean_windows"}
                or page.get("incident_id") != incident_id or page.get("session_id") != branch["session_id"]
                or page.get("report_item_id") != branch["report_item_id"] or page.get("report") != branch["report"]
                or page.get("scope") not in (scope, f"{environment_id}/{service}")
                or page.get("channel") != "local_oncall_inbox" or page.get("evidence") != "investigation.json"):
            raise ValueError("必须先收到同一事故、环境和调查报告的恢复建议与 paging 记录。")
        if require_fault and (record["recovered_inspection_id"] is not None
                              or not (environment / f"{service}.fault").is_file()
                              or classify(current) != "abnormal"):
            raise ValueError("当前事故没有可执行的实验故障，请等待巡检确认恢复。")
        return {"source": source, "instance": alias, "service": service, "environment_id": environment_id,
                "incident_id": incident_id, "scope": scope, "report": branch["report"],
                "session_id": branch["session_id"], "report_item_id": branch["report_item_id"],
                "skill_sha256": record["skill_sha256"], "observation": current,
                "evidence": str((inbox / "investigation.json").relative_to(self.root)),
                "report_sha256": file_hash(inbox / "investigation.json"),
                "page_sha256": file_hash(inbox / "page.json")}

    def recovery_snapshot(self, incident_id, *, recovered=False):
        if not isinstance(incident_id, str) or not re.fullmatch(r"lab-(orders|payments|inventory|notifications)-[0-9a-f]{12}", incident_id):
            return None
        directory = self.root / "recovery" / incident_id
        request = optional_json(directory / "request.json")
        if request is None:
            return None
        execution = optional_json(directory / "execution.json")
        runtime_state = None
        database = self.root / "recovery" / "incidents.sqlite3"
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT state FROM incidents WHERE id=?", (incident_id,)).fetchone()
                runtime_state = row[0] if row else None
        return {**request, "approval": optional_json(directory / "approval.json"), "execution": execution,
                "runtime_state": runtime_state,
                "status": "recovered" if runtime_state == "CLOSED" else "verifying" if execution else "approved" if optional_json(directory / "approval.json") else "awaiting_approval"}

    def sync_recovery_verification(self, source, worker, record):
        """Mirror the patrol's persisted post-execution windows into its action ledger."""
        from incident_runtime import IncidentRuntime

        request = self.recovery_snapshot(record["incident_id"])
        if not request or not request["execution"] or request["runtime_state"] == "CLOSED" or record["recovered_inspection_id"] is None:
            return
        original = json.loads(record["observation"])
        if (source != request["source"] or source.rsplit("/", 1)[-1] != request["instance"] or original["service"] != request["service"]
                or original["environment_id"] != request["environment_id"]):
            return
        lower = request["execution"]["effect"]["cleared_at"]
        windows = []
        with closing(connect(worker, readonly=True)) as db:
            for row in db.execute("SELECT * FROM inspections WHERE service=? AND status='healthy' AND checked_at>? ORDER BY checked_at",
                                  (request["service"], lower)):
                observed = json.loads(row["observation"])
                if (observed["source"] != "live_lab_http_requests" or observed["environment_id"] != request["environment_id"]
                        or observed["service"] != request["service"] or abs(observed["window_end"] - observed["window_start"] - 30) > 0.001
                        or observed["window_start"] <= lower or observed["error_rate"] != 0 or classify(observed) != "healthy"):
                    continue
                with closing(sqlite3.connect((worker / "hermes/state.db").as_uri() + "?mode=ro", uri=True)) as sessions:
                    tool = sessions.execute("SELECT role,tool_name,content FROM messages WHERE session_id=? AND id=?",
                                            (row["session_id"], row["evidence_item_id"])).fetchone()
                    report = sessions.execute("SELECT role,content FROM messages WHERE session_id=? AND id=?",
                                              (row["session_id"], row["report_item_id"])).fetchone()
                returned = json.loads(tool[2]) if tool else {}
                returned.pop("history_available", None)
                if (not tool or tool[:2] != ("tool", "service_snapshot") or returned != observed or not report
                        or report[0] != "assistant" or decode_report(report[1]).get("status") != "healthy"
                        or row["evidence_item_id"] >= row["report_item_id"]):
                    continue
                windows.append({"inspection_id": row["id"], "source": source, "session_id": row["session_id"],
                                "evidence_item_id": row["evidence_item_id"], "report_item_id": row["report_item_id"],
                                "observation": observed})
                lower = observed["window_end"]
                if len(windows) == 2:
                    break
        if len(windows) != 2:
            return
        runtime = IncidentRuntime(self.root / "recovery" / "incidents.sqlite3")
        for index, window in enumerate(windows, 1):
            runtime.observe(f"{record['incident_id']}-recovery-{index}", record["incident_id"], request["scope"],
                            window["observation"]["requests"], 0, source="verification", quality="healthy", evidence=window)
        runtime.close(record["incident_id"], request["approval"]["actor"])
        write_json(self.root / "recovery" / record["incident_id"] / "verification.json", {"windows": windows, "verified_at": time.time()})
        self.event("recovery_verified", "两个执行后独立健康窗口已确认，事故处置已关闭", incident_id=record["incident_id"])

    def request_recovery(self, body):
        from incident_runtime import IncidentRuntime

        source, incident_id = body.get("source"), body.get("incident_id")
        if source is None:
            alias, service = body.get("instance"), body.get("service")
            if alias not in ALIASES or service not in SERVICES:
                raise ValueError("请选择需要恢复的现场事故。")
            source = f"{self.active_run.name}/{alias}" if self.active_run is not None else ""
            rows = self.incident_snapshot(source, self.sources().get(source, self.root / "missing"))
            row = next((item for item in rows if item["service"] == service and item["page"]
                        and item["status"] != "recovered" and (incident_id is None or item["incident_id"] == incident_id)), None)
            if row is None:
                raise ValueError("恢复前必须先有调查报告与 paging 记录。")
            incident_id = row["incident_id"]
        evidence = self.recovery_evidence(source, incident_id)
        directory = self.root / "recovery" / incident_id
        prior = self.recovery_snapshot(incident_id)
        if prior is not None:
            if any(prior.get(key) != evidence[key] for key in ("source", "scope", "session_id", "report_item_id", "report_sha256", "page_sha256")):
                raise ValueError("该事故已有绑定其他报告的恢复请求，请查看原请求。")
            return prior
        directory.mkdir(parents=True, exist_ok=True)
        flag = self.root / "environments" / evidence["instance"] / f"{evidence['service']}.fault"
        marker = flag.stat()
        request = {**evidence, "operation": incident_id + "-clear-fault", "action": "clear_lab_fault",
                   "requested_at": time.time(), "approval_required": True,
                   "fault_generation": {"inode": marker.st_ino, "mtime_ns": marker.st_mtime_ns}}
        runtime = IncidentRuntime(self.root / "recovery" / "incidents.sqlite3")
        observed = evidence["observation"]
        runtime.observe(incident_id + "-trigger", incident_id, request["scope"], observed["requests"],
                        round(observed["requests"] * observed["error_rate"]), quality="unhealthy",
                        source="workbench_recovery_request", evidence=observed)
        runtime.start(incident_id, request["skill_sha256"])
        runtime.request_action(incident_id, request["operation"], request["evidence"], action=request["action"])
        write_json(directory / "request.json", request)
        self.event("recovery_requested", "恢复请求已建立，等待本机明确批准", incident_id=incident_id,
                   operation=request["operation"], scope=request["scope"], evidence=request["evidence"])
        return self.recovery_snapshot(incident_id)

    def approve_recovery(self, body, request_context=None):
        from environment import _append_change, change_lock
        from fleet import clear_lab_fault
        from incident_runtime import IncidentRuntime

        if body.get("confirmed") is not True:
            raise ValueError("需要明确批准本次具体恢复请求，才能执行本地恢复。")
        request = self.recovery_snapshot(body.get("incident_id"))
        if request is None or any(body.get(key) != request[key] for key in (
                "source", "incident_id", "operation", "scope", "environment_id")):
            raise ValueError("批准与请求的事故、环境、范围或操作不匹配。")
        evidence = self.recovery_evidence(request["source"], request["incident_id"], require_fault=not bool(request["execution"]))
        if any(evidence[key] != request[key] for key in ("scope", "session_id", "report_item_id", "report_sha256", "page_sha256")):
            raise ValueError("调查报告已改变，请重新核对恢复请求。")
        if request["execution"]:
            return request
        directory = self.root / "recovery" / request["incident_id"]
        runtime = IncidentRuntime(self.root / "recovery" / "incidents.sqlite3")
        approval = request["approval"]
        if approval is None:
            approval = {"actor": "local-workbench-user", "source": "local_workbench_http" if request_context else "local_workbench_api",
                        "identity_verified": False, "confirmed": True, "confirmed_at": time.time(),
                        "operation": request["operation"], "incident_id": request["incident_id"], "scope": request["scope"],
                        "request_context": request_context or {"transport": "local_api"}}
            runtime.approve(request["incident_id"], request["operation"], request["scope"], approval["actor"])
            write_json(directory / "approval.json", approval)

        def apply():
            environment = self.root / "environments" / request["instance"]
            flag = environment / f"{request['service']}.fault"
            with change_lock(environment):
                before = "off"
                if flag.is_file():
                    marker = flag.stat()
                    if {"inode": marker.st_ino, "mtime_ns": marker.st_mtime_ns} != request["fault_generation"]:
                        raise ValueError("故障已被重新注入，本次批准不能清除另一代故障。")
                    before = "intermittent" if flag.read_text(encoding="utf-8") == "intermittent" else "on"
                effect = clear_lab_fault(environment, request["service"], request["environment_id"])
                if not effect["already_clear"]:
                    _append_change(environment, {"type": "fault_changed", "environment_id": request["environment_id"],
                        "service": request["service"], "actor": approval["actor"], "before": before, "after": "off",
                        "summary": "本机明确批准后执行实验恢复", "operation": request["operation"], "incident_id": request["incident_id"]})
                return effect

        receipt = runtime.execute(request["incident_id"], request["operation"], request["scope"], apply=apply)
        write_json(directory / "execution.json", receipt)
        self.event("recovery_executed", "已批准并执行本地恢复，等待后续巡检验证", incident_id=request["incident_id"],
                   operation=request["operation"], scope=request["scope"], approval=approval, execution=receipt)
        return self.recovery_snapshot(request["incident_id"])

    def action(self, body, *, request_context=None):
        with self.lock:
            action = body.get("action")
            if action in ("agent_start", "restart"):
                if action == "restart":
                    self.stop(self.agent)
                return self.start_agent()
            if action == "agent_stop":
                self.stop(self.agent)
                self.event("agent_stopped", "巡检 Agent 已停止，服务环境继续运行")
                return {"status": "stopped"}
            if action in ("recover", "recovery_request") or action == "fault" and body.get("mode") == "off":
                return self.request_recovery(body)
            if action == "recovery_approve":
                return self.approve_recovery(body, request_context)
            if action == "fault":
                alias, service = body.get("instance"), body.get("service")
                mode = body.get("mode")
                if alias not in ALIASES or service not in SERVICES or mode not in ("on", "intermittent"):
                    raise ValueError("Choose a known instance, service and fault mode")
                inject_fault(self.root / "environments" / alias, service, mode)
                label = {"on": "注入持续失败", "off": "解除故障", "intermittent": "注入间歇失败"}[mode]
                self.event("fault_changed", f"{alias} / {service} · {label}",
                           instance=alias, service=service, mode=mode)
                return {"status": "accepted", "instance": alias, "service": service, "mode": mode}
            if action == "check":
                return self.start_check(body.get("stage"))
            if action in ("memory_review", "memory_recall"):
                return self.start_memory(action.removeprefix("memory_"), body.get("incident_id"))
            if action == "question":
                return self.start_question(body)
            if action == "chat_new":
                return self.new_conversation(body.get("title"))
            if action == "chat_send":
                return self.send_chat(body)
            raise ValueError("Unknown workbench action")

    def run_paths(self):
        return sorted((self.root / "runs").glob("*/service.json"), reverse=True)

    def sources(self):
        sources = {}
        for status in self.run_paths():
            run_id = status.parent.name
            for alias in ALIASES:
                sources[f"{run_id}/{alias}"] = status.parent / "patrols" / alias
        for check in self.check_records():
            directory = self.root / "checks" / check["id"]
            for database in directory.glob("**/hermes/state.db"):
                relative = database.parent.parent.relative_to(directory).as_posix()
                sources[f"check:{check['id']}/{relative}"] = database.parent.parent
        for database in (self.root / "memory/recalls").glob("*/hermes/state.db"):
            sources[f"memory:{database.parent.parent.name}"] = database.parent.parent
        for database in (self.root / "chat").glob("*/hermes/state.db"):
            sources[f"chat:{database.parent.parent.name}"] = database.parent.parent
        for database in (self.root / "chat").glob("*/threads/*/hermes/state.db"):
            sources[f"chat-thread:{database.parents[3].name}/{database.parent.parent.name}"] = database.parent.parent
        return sources

    def business_snapshot(self, instances, incident_rows):
        from environment import get_request_logs, read_changes

        logs, changes, alerts, errors = [], [], [], []
        # The same monitoring event can be resumed in a later Agent run. Keep
        # the active run's progress, then the first (newest) remaining source.
        active = self.active_run.name if self.active_run is not None else None
        ordered = sorted((item for item in incident_rows if not item["source"].startswith("check:")),
                         key=lambda item: item["source"].split("/", 1)[0] != active)
        recorded = {}
        for item in ordered:
            recorded.setdefault((item["instance"], item["incident_id"]), item)
        for instance in instances:
            alias = instance["id"]
            directory = self.root / "environments" / alias
            scopes = {service["metrics"]["environment_id"] for service in instance["services"]
                      if service.get("metrics", {}).get("environment_id")}
            if len(scopes) != 1:
                errors.append({"instance": alias, "source": "business", "error": "当前环境指标尚未就绪。"})
                continue
            environment_id = next(iter(scopes))
            for service in SERVICES:
                try:
                    observation = get_request_logs(directory, service)
                    if observation["environment_id"] != environment_id or observation["service"] != service:
                        raise ValueError("日志不属于当前服务环境。")
                    for entry in observation["entries"]:
                        if entry.get("environment_id") != environment_id or entry.get("service") != service:
                            raise ValueError("日志行不属于当前服务环境。")
                        status = entry["status"]
                        logs.append({**entry, "timestamp": entry["time"], "instance": alias,
                                     "source": observation["source"], "http_status": status,
                                     "latency_ms": entry["duration_ms"], "level": "error" if status >= 500 else "info",
                                     "message": f"{entry['method']} {entry['path']} → HTTP {status}"})
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    errors.append({"instance": alias, "service": service, "source": "logs", "error": str(exc)})
            try:
                for entry in read_changes(directory, environment_id=environment_id, limit=100):
                    if entry["environment_id"] != environment_id:
                        raise ValueError("变更记录不属于当前服务环境。")
                    changes.append({**entry, "timestamp": entry["time"], "instance": alias,
                                    "kind": entry["type"], "title": entry["summary"]})
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                errors.append({"instance": alias, "source": "changes", "error": str(exc)})
            for path in (directory / "alerts").glob("*/*.json"):
                event = optional_json(path, {})
                if (event.get("environment_id") != environment_id or event.get("service") not in SERVICES
                        or event.get("incident_id") != path.stem):
                    continue
                record = recorded.get((alias, event["incident_id"]))
                if record and (record["instance"] != alias or record["service"] != event["service"]
                               or record["observation"]["environment_id"] != environment_id):
                    record = None
                alerts.append({**event, "timestamp": event["emitted_at"], "instance": alias,
                               "status": record["status"] if record else "open",
                               "report_source": record["source"] if record else None,
                               "session_id": record["session_id"] if record else None,
                               "report_item_id": record["report_item_id"] if record else None,
                               "recovery": record.get("recovery") if record else None})
        return {"logs": sorted(logs, key=lambda item: item["timestamp"], reverse=True)[:600],
                "changes": sorted(changes, key=lambda item: item["timestamp"], reverse=True)[:200],
                "alerts": sorted(alerts, key=lambda item: item["timestamp"], reverse=True)[:100], "errors": errors}

    def instance_snapshot(self, alias):
        environment = self.root / "environments" / alias
        services = []
        state = self.active_run / "patrols" / alias if self.active_run is not None else None
        database = state / "patrol.sqlite" if state is not None else None
        db = connect(state, readonly=True) if database is not None and database.is_file() else None
        try:
            for service in SERVICES:
                try:
                    metrics = get_snapshot(environment, service)
                    health = classify(metrics)
                    samples = self.history[alias][service]
                    if not samples or metrics["window_end"] - samples[-1]["time"] >= .5:
                        samples.append({"time": metrics["window_end"], "requests": metrics["requests"],
                                        "error_rate": metrics["error_rate"], "p95_ms": metrics["p95_ms"]})
                    saved = checkpoint(db, service) if db is not None else None
                    services.append({"name": service, "health": health, "metrics": metrics,
                                     "checkpoint": saved, "history": list(samples),
                                     "fault": ((environment / f"{service}.fault").read_text() or "on")
                                     if (environment / f"{service}.fault").exists() else None})
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    services.append({"name": service, "health": "unavailable", "error": str(exc)})
        finally:
            if db is not None:
                db.close()
        return {"id": alias, "services": services}

    def incident_snapshot(self, source, state):
        database = state / "patrol.sqlite"
        if not database.is_file():
            return []
        result = []
        with closing(connect(state, readonly=True)) as db:
            for service in SERVICES:
                for record in incidents(db, service)[:15]:
                    incident = record["incident_id"]
                    if not re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident):
                        continue
                    directory = state / "pages" / incident
                    branch = optional_json(directory / "investigation.json")
                    page = optional_json(directory / "page.json")
                    observed = json.loads(record["observation"])
                    report = None
                    if (state / "hermes/state.db").is_file():
                        with closing(sqlite3.connect((state / "hermes/state.db").as_uri() + "?mode=ro", uri=True)) as sessions:
                            row = sessions.execute("SELECT content FROM messages WHERE id=? AND session_id=?",
                                                   (record["report_item_id"], record["session_id"])).fetchone()
                            if row is not None:
                                try:
                                    report = decode_report(row[0])
                                except ValueError:
                                    report = {"summary": row[0]}
                    status = "recovered" if record["recovered_inspection_id"] is not None else "handed_off" if page else "investigating"
                    self.sync_recovery_verification(source, state, record)
                    result.append({"source": source, "instance": source.rsplit("/", 1)[-1],
                                   "incident_id": incident, "service": service, "status": status,
                                   "observed_at": observed["window_end"], "observation": observed,
                                   "session_id": record["session_id"], "report_item_id": record["report_item_id"],
                                   "skill_sha256": record["skill_sha256"], "report": report,
                                   "investigation": branch, "page": page,
                                   "recovered_inspection_id": record["recovered_inspection_id"],
                                   "recovery": self.recovery_snapshot(incident, recovered=record["recovered_inspection_id"] is not None)})
        return result

    @staticmethod
    def session_summaries(source, state):
        database = state / "hermes/state.db"
        if not database.is_file():
            return []
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [{"source": source, **dict(row)} for row in db.execute(
                "SELECT id, parent_session_id, started_at, model, message_count FROM sessions "
                "ORDER BY started_at DESC LIMIT 12")]

    def session(self, source, session_id, item=None):
        with self.lock:
            state = self.sources().get(source)
            if state is None:
                raise ValueError("Unknown session source")
            database = state / "hermes/state.db"
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                metadata = db.execute("SELECT id, parent_session_id, started_at, model FROM sessions WHERE id=?",
                                      (session_id,)).fetchone()
                if metadata is None:
                    raise ValueError("Unknown session")
                if item is None:
                    predicate, parameters = "session_id=?", (session_id,)
                else:
                    item = int(item)
                    if db.execute("SELECT 1 FROM messages WHERE session_id=? AND id=?", (session_id, item)).fetchone() is None:
                        raise ValueError("The requested Item does not belong to this session")
                    predicate, parameters = "session_id=? AND id<=?", (session_id, item)
                rows = db.execute("SELECT id, role, content, tool_call_id, tool_name, tool_calls FROM messages "
                                  f"WHERE {predicate} ORDER BY id DESC LIMIT 501", parameters).fetchall()
                messages = [dict(row) for row in reversed(rows[:500])]
            for message in messages:
                message["tool_calls"] = json.loads(message["tool_calls"] or "[]")
            return {"source": source, "session": dict(metadata), "messages": messages,
                    "has_earlier_messages": len(rows) > 500, "anchor_item_id": item}

    def learning_snapshot(self):
        if self.active_run is None:
            return {"status": "offline", "batches": [], "versions": [], "diff": ""}
        root = self.active_run
        state = optional_json(root / "learning/state.json", {"status": "offline"})
        batches = []
        for path in sorted((root / "learning/batches").glob("*/attempt-*/result.json"),
                           key=lambda item: item.stat().st_mtime, reverse=True)[:12]:
            batches.append({"path": path.relative_to(root).as_posix(), **optional_json(path, {})})
        versions = []
        for path in sorted((root / "formal/versions").glob(f"*/{ONCALL_SKILL}/SKILL.md")):
            versions.append({"sha256": path.parent.parent.name, "content": path.read_text(encoding="utf-8")})
        diff = ""
        release = state.get("last_release")
        if release is not None:
            by_hash = {version["sha256"]: version["content"] for version in versions}
            old, new = (by_hash.get(release[key], "") for key in ("previous_sha256", "sha256"))
            diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                fromfile="previous/SKILL.md", tofile="approved/SKILL.md"))
        return {**state, "batches": batches, "versions": versions, "diff": diff}

    def snapshot(self):
        with self.lock:
            self.refresh_check()
            self.refresh_jobs()
            implementation_error = None
            try:
                stages = stage_status(self.implementation_file)
            except (OSError, SyntaxError, ImportError, ValueError, TypeError, AttributeError) as exc:
                implementation_error = str(exc)
                stages = [{"number": number, "name": item["name"], "title": item["title"], "implemented": False}
                          for number, item in STAGES.items()]
            code_sha = file_hash(self.implementation_file) if self.implementation_file.is_file() else None
            service = optional_json(self.active_run / "service.json", {}) if self.active_run is not None else {}
            status = self.agent_status()
            sources = self.sources()
            incident_rows, sessions = [], []
            errors = []
            for source, state in sources.items():
                try:
                    incident_rows.extend(self.incident_snapshot(source, state))
                    sessions.extend(self.session_summaries(source, state))
                except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                    errors.append({"source": source, "error": str(exc)})
            all_checks = self.check_records()
            for check in all_checks:
                check["verified"] = bool(check.get("evidence_valid") and code_sha
                                         and check.get("implementation_sha256") == code_sha)
            for stage in stages:
                stage["verified"] = any(check["verified"] and check["stage"] >= stage["number"] for check in all_checks)
            checks = []
            for check in all_checks[-8:]:
                log = self.root / "checks" / f"{check['id']}.log"
                evidence = check.get("evidence")
                output = (log.read_text(encoding="utf-8", errors="replace")[-20000:] if log.exists()
                          else json.dumps(evidence, ensure_ascii=False, indent=2) if evidence else "")
                checks.append({**check, "log": output, "evidence": evidence})
            agent_log = self.root / "runs" / f"{self.active_run.name}.log" if self.active_run is not None else None
            instances = [self.instance_snapshot(alias) for alias in ALIASES]
            return {"schema": "oncall-workbench-v1", "root": str(self.root), "time": time.time(),
                    "agent": {"status": status, "mode": "live" if self.live else "local-script-model",
                              "implementation": str(self.implementation_file), "code_sha256": code_sha,
                              "model_env_file": os.environ.get("ONCALL_MODEL_ENV_FILE"),
                              "running_sha256": self.running_sha, "code_changed": self.running_sha != code_sha,
                              "implementation_error": implementation_error,
                              "log": agent_log.read_text(encoding="utf-8", errors="replace")[-12000:]
                              if agent_log is not None and agent_log.exists() else "",
                              "pid": self.agent.pid if self.agent is not None else None, "stages": stages,
                              "active_stage": self.record.get("active_stage", 0), "service": service},
                    "instances": instances, "business": self.business_snapshot(instances, incident_rows),
                    "chat": self.chat_capability(), "conversations": self.conversation_snapshots(),
                    "incidents": sorted(incident_rows, key=lambda row: row["observed_at"], reverse=True),
                    "sessions": sorted(sessions, key=lambda row: row["started_at"], reverse=True),
                    "learning": self.learning_snapshot(), "memory": self.memory_snapshot(),
                    "questions": self.question_snapshots(), "checks": checks,
                    "events": list(reversed(self.record["events"])), "errors": errors}


def make_server(workbench, host, port, html_path):
    html_path = Path(html_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            payload = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            try:
                if parsed.path == "/":
                    return self.send(200, html_path.read_bytes(), "text/html; charset=utf-8")
                if parsed.path == "/api/state":
                    return self.send(200, workbench.snapshot())
                if parsed.path == "/api/session":
                    query = parse_qs(parsed.query)
                    return self.send(200, workbench.session(query.get("source", [""])[0],
                                                           query.get("session", [""])[0], query.get("item", [None])[0]))
                if parsed.path == "/api/chat":
                    query = parse_qs(parsed.query)
                    return self.send(200, workbench.chat(query.get("conversation", [""])[0]))
                self.send(404, {"error": "Not found"})
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, SyntaxError, sqlite3.Error) as exc:
                self.send(400, {"error": str(exc)})

        def do_POST(self):
            if urlsplit(self.path).path != "/api/actions":
                return self.send(404, {"error": "Not found"})
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                return self.send(403, {"error": "Actions must come from this workbench"})
            if self.headers.get_content_type() != "application/json":
                return self.send(415, {"error": "Use application/json"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Use a JSON object")
                if body.get("action") == "recovery_approve":
                    self.send(200, workbench.action(body, request_context={"transport": "http", "origin": origin,
                                                                         "host": self.headers.get("Host")}))
                else:
                    self.send(200, workbench.action(body))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, SyntaxError, sqlite3.Error) as exc:
                self.send(400, {"error": str(exc)})

    server = LoopbackHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
