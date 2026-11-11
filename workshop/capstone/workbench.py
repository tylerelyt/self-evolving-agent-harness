"""Local On-call workbench over real HTTP, Hermes and learning records."""

from collections import deque
from contextlib import closing
import difflib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
from environment import SERVICES, classify, get_snapshot, inject_fault, load_json, write_json
from hermes_runtime import ONCALL_SKILL
from patrol import checkpoint, connect, incidents

ALIASES = ("instance-a", "instance-b")
JOB_LABELS = {"question": "追问", "memory_review": "Memory 复盘", "memory_recall": "新会话召回"}
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

    def start_agent(self):
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
                job.update(status="completed" if process.returncode == 0 else "failed",
                           returncode=process.returncode, finished_at=time.time())
                self.event("job_finished", JOB_LABELS[job["kind"]] + ("完成" if process.returncode == 0 else "失败"),
                           job_id=job["id"])

    def add_job(self, kind, identifier, command, log, **details):
        self.refresh_jobs()
        if any(job["kind"] == kind and job["status"] == "running" for job in self.record["jobs"]):
            raise ValueError("同类任务正在执行，请等结果返回。")
        self.background[identifier] = self.spawn(command, log)
        job = {"id": identifier, "kind": kind, "status": "running", "started_at": time.time(),
               "log": str(log.relative_to(self.root)), **details}
        self.record["jobs"].append(job)
        self.event("job_started", JOB_LABELS[kind] + "已开始", job_id=identifier)
        return job

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
        memory = optional_json(self.root / "memory/state.json", {"status": "empty", "facts": [], "recalls": []})
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

    def action(self, body):
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
            if action in ("fault", "recover"):
                alias, service = body.get("instance"), body.get("service")
                mode = "off" if action == "recover" else body.get("mode")
                if alias not in ALIASES or service not in SERVICES or mode not in ("on", "off", "intermittent"):
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
        return sources

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
                                    report = json.loads(row[0])
                                except json.JSONDecodeError:
                                    report = {"summary": row[0]}
                    status = "recovered" if record["recovered_inspection_id"] is not None else "handed_off" if page else "investigating"
                    result.append({"source": source, "instance": source.rsplit("/", 1)[-1],
                                   "incident_id": incident, "service": service, "status": status,
                                   "observed_at": observed["window_end"], "observation": observed,
                                   "session_id": record["session_id"], "report_item_id": record["report_item_id"],
                                   "skill_sha256": record["skill_sha256"], "report": report,
                                   "investigation": branch, "page": page,
                                   "recovered_inspection_id": record["recovered_inspection_id"]})
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
            if self.agent is None:
                status = "offline"
            elif self.agent.poll() is None:
                status = service.get("status", "starting")
            else:
                status = "stopped" if self.agent.returncode == 0 else "failed"
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
            return {"schema": "oncall-workbench-v1", "root": str(self.root), "time": time.time(),
                    "agent": {"status": status, "mode": "live" if self.live else "local-script-model",
                              "implementation": str(self.implementation_file), "code_sha256": code_sha,
                              "running_sha256": self.running_sha, "code_changed": self.running_sha != code_sha,
                              "implementation_error": implementation_error,
                              "log": agent_log.read_text(encoding="utf-8", errors="replace")[-12000:]
                              if agent_log is not None and agent_log.exists() else "",
                              "pid": self.agent.pid if self.agent is not None else None, "stages": stages,
                              "active_stage": self.record.get("active_stage", 0), "service": service},
                    "instances": [self.instance_snapshot(alias) for alias in ALIASES],
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
                self.send(200, workbench.action(body))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, SyntaxError, sqlite3.Error) as exc:
                self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
