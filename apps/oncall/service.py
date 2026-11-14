"""Keep two Hermes On-call workers and their background learning service running."""

import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from apps.oncall.paths import CAPSTONE, prepare
prepare()
HERE = CAPSTONE

from environment import SERVICES, load_json, write_json
from fleet import GROUP, SEED, receipt, sha256
from hermes_runtime import ONCALL_SKILL, ensure_loopback_no_proxy
from implementation import load_implementation
from patrol import connect, incidents, model_connection, writer_lock
from skillclaw_bridge import _bound_port, incident_archive
from self_evolving_harness.coordinator import ImprovementCoordinator
from self_evolving_harness.learning import LearningLedger
from self_evolving_harness.control import ControlStore, LeaseLost

ALIASES = ("instance-a", "instance-b")


def patrol_configuration(implementation):
    module = load_implementation(implementation)
    config = {"services": list(SERVICES), "sample": 2, "interval": 1}
    configure = getattr(module, "patrol_config", None)
    if configure is not None:
        if not callable(configure):
            raise ValueError("patrol_config must be a function")
        supplied = configure()
        if not isinstance(supplied, dict) or set(supplied) - config.keys():
            raise ValueError("patrol_config must return services, sample and interval settings")
        config.update(supplied)
    services = config["services"]
    if (not isinstance(services, (list, tuple)) or not services
            or any(not isinstance(service, str) or service not in SERVICES for service in services)
            or len(set(services)) != len(services)):
        raise ValueError("patrol_config services must be a nonempty list of unique lab services")
    sample = config["sample"]
    if type(sample) is not int or not 1 <= sample <= len(services):
        raise ValueError("patrol_config sample must be an integer within the selected service count")
    interval = config["interval"]
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("patrol_config interval must be a finite positive number")
    return {"services": list(services), "sample": sample, "interval": interval}


def stop_process(child):
    if child.poll() is None:
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def start_process(stack, command, log):
    output = stack.enter_context(log.open("a"))
    child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    stack.callback(stop_process, child)
    return child


@contextmanager
def client_proxy(root, alias, skills, options, *, offline=False):
    from skillclaw.api_server import SkillClawAPIServer, _llm_request_timeout_seconds
    from skillclaw.config import SkillClawConfig

    sys.path.insert(0, str(HERE.parents[1] / "src"))
    from apps.oncall.integrations.skillclaw_compat import structured_tool_status_compat

    record_dir = root / "proxy" / alias
    record_dir.mkdir(parents=True, exist_ok=True)
    config = SkillClawConfig(
        proxy_host="127.0.0.1", proxy_port=0, proxy_api_key=options["api_key"],
        claw_type="hermes", configure_openclaw=False, configure_hermes=False,
        llm_api_base=options["base_url"], llm_api_key=options["api_key"],
        llm_model_id=options["model"], served_model_name=options["model"],
        use_prm=False, use_skills=False, skills_dir=str(skills), record_enabled=True,
        record_dir=str(record_dir), sharing_enabled=True, sharing_upload_sessions=True,
        sharing_backend="local", sharing_local_root=str(root / "shared"),
        sharing_group_id=GROUP, sharing_user_alias=alias, sharing_skill_reload_mode="off",
        sharing_session_upload_interval=1,
        validation_enabled=False, dashboard_enabled=False, max_context_tokens=64000,
    )
    request_timeout = _llm_request_timeout_seconds()
    if not math.isfinite(request_timeout):
        raise ValueError("Use a finite SkillClaw upstream request timeout")
    # Native idle tracking is touched when an upstream request starts, before
    # its final response arrives. A short idle limit can archive an unfinished
    # tool turn while a real model is still generating the report.
    config.session_idle_close_seconds = 5 if offline else max(300, math.ceil(request_timeout) + 30)
    config.session_sweep_interval_seconds = 1
    proxy = SkillClawAPIServer(config)
    with structured_tool_status_compat():
        proxy.start()
        try:
            yield f"http://127.0.0.1:{_bound_port(proxy)}/v1"
        finally:
            proxy.stop()
            proxy._thread.join(timeout=65)
            if proxy._thread.is_alive():
                raise RuntimeError(f"{alias}: Client Proxy failed to drain")


def replace_skill(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(root, source, digest, *, state=None, safe_sha256=None, candidate_id=None):
    """Pull approved bytes with native clients before exposing them to workers."""
    from skillclaw_evolve import release

    if state is not None:
        state["pending_release"] = {
            "previous_sha256": safe_sha256 or state["active_sha256"],
            "candidate_sha256": digest, "source": str(source.resolve()), "started_at": time.time(),
        }
        if candidate_id is not None:
            state["pending_release"]["candidate_id"] = candidate_id
        write_json(root / "learning" / "state.json", state)
    delivery = root / "learning" / "deliveries" / uuid4().hex
    receivers = {alias: delivery / alias for alias in ALIASES}
    result = release(root / "formal", source, digest, receivers)
    content = (root / "formal" / "versions" / digest / ONCALL_SKILL / "SKILL.md").read_bytes()
    for alias in ALIASES:
        target = root / "receivers" / alias / ONCALL_SKILL / "SKILL.md"
        replace_skill(target, content)
    return result


def learning_ledger(root):
    return LearningLedger(root / "system" / "learning.sqlite3")


def restoration_receipt(root, restored_sha256):
    """Verify the preserved version and both actual worker entrypoints."""
    snapshot = root / "formal" / "versions" / restored_sha256 / ONCALL_SKILL / "SKILL.md"
    paths = {alias: root / "receivers" / alias / ONCALL_SKILL / "SKILL.md" for alias in ALIASES}
    if sha256(snapshot) != restored_sha256 or any(sha256(path) != restored_sha256 for path in paths.values()):
        raise RuntimeError("Restoration does not match the parent at every worker")
    return {"verified": True, "receivers": {alias: {"sha256": restored_sha256} for alias in paths}}


def reconcile_learning_ledger(root, state):
    """Reconcile lifecycle intent without re-running models or publication."""
    ledger = learning_ledger(root)
    last = state.get("last_release", {})
    if last.get("candidate_id") and last.get("status") == "rolled_back":
        ledger.recovered(last["candidate_id"], last["previous_sha256"],
                         restoration_receipt(root, last["previous_sha256"]))
    for row in ledger.candidates(statuses=("publishing",)):
        if (state.get("pending_release") or {}).get("candidate_id") == row["id"]:
            raise RuntimeError("Reconcile business delivery before admitting more learning")
        if (last.get("candidate_id") == row["id"]
                and state["active_sha256"] == row["spec"]["artifact_sha256"]
                and last.get("publication_receipt")):
            ledger.published(row["id"], last["publication_receipt"])
        elif state["active_sha256"] == row["spec"]["parent_sha256"]:
            ledger.recovered(row["id"], state["active_sha256"],
                             restoration_receipt(root, state["active_sha256"]))
        else:
            raise RuntimeError("Unresolved publication intent requires delivery reconciliation")


def recover_publication(root, state):
    """Restore interrupted publication before admitting another learning batch."""
    pending = state.get("pending_release")
    if pending is None:
        return None
    safe_sha = pending["previous_sha256"]
    source = root / "formal" / "versions" / safe_sha / ONCALL_SKILL / "SKILL.md"
    try:
        result = publish(root, source, safe_sha)
    except Exception as exc:
        state.update(status="recovery_error", error=str(exc), updated_at=time.time())
        write_json(root / "learning" / "state.json", state)
        raise
    recovered = {**state, "active_sha256": safe_sha, "status": "waiting"}
    recovered.pop("pending_release")
    recovered["publication_recovery"] = {
        "status": "requires_reevaluation", "interrupted_release": pending,
        "restored_sha256": safe_sha, "recovered_at": time.time(),
    }
    if pending.get("candidate_id"):
        learning_ledger(root).recovered(pending["candidate_id"], safe_sha,
                                        restoration_receipt(root, safe_sha))
    last_release = recovered.get("last_release")
    if last_release is not None and last_release["sha256"] != safe_sha:
        recovered["last_release"] = {**last_release, "status": "interrupted"}
    write_json(root / "learning" / "state.json", recovered)
    state.clear()
    state.update(recovered)
    return result


def completed_incidents(root):
    found = []
    for alias in ALIASES:
        state = root / "patrols" / alias
        database = state / "patrol.sqlite"
        if not database.is_file():
            continue
        with closing(connect(state, readonly=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'incidents'").fetchone() is None:
                continue
            for service in SERVICES:
                for record in incidents(db, service):
                    if record["recovered_inspection_id"] is not None:
                        found.append({"instance": alias, "record": record})
    return found


def check_adoption(root, state, completed):
    release = state.get("last_release")
    if not release or release["status"] != "awaiting_adoption":
        return
    for entry in completed:
        alias, record = entry["instance"], entry["record"]
        if alias in release["adoptions"] or record["skill_sha256"] != release["sha256"]:
            continue
        observation = json.loads(record["observation"])
        if observation["window_end"] <= release["published_at"]:
            continue
        proof = receipt(root / "patrols" / alias, record)
        statuses = [probe["http_status"] for probe in proof["probes"]]
        assessment = None
        if len(statuses) == 1 and 200 <= statuses[0] < 300:
            assessment = "not_reproduced"
        elif len(statuses) == 2 and 500 <= statuses[0] < 600:
            if 200 <= statuses[1] < 300:
                assessment = "intermittent"
            elif 500 <= statuses[1] < 600:
                assessment = "persistent"
        skill = proof["skill"]
        valid = (
            assessment is not None and proof["report"].get("assessment") == assessment
            and proof["report"].get("probe_http_statuses") == statuses
            and proof["report"].get("probe_http_status") == statuses[0]
            and skill.get("success") is True and skill.get("name") == ONCALL_SKILL
            and skill.get("sha256") == release["sha256"]
            and hashlib.sha256(skill["content"].encode("utf-8")).hexdigest() == release["sha256"]
            and all(record["skill_item_id"] < probe["call_item_id"] < probe["item_id"] < record["report_item_id"]
                    and probe.get("service") == observation["service"]
                    and probe.get("environment_id") == observation["environment_id"]
                    and observation["window_end"] <= probe["checked_at"] for probe in proof["probes"])
            and all(previous["item_id"] < current["call_item_id"]
                    for previous, current in zip(proof["probes"], proof["probes"][1:]))
        )
        release["adoptions"][alias] = {"session_id": record["session_id"], "incident_id": record["incident_id"],
                                       "sha256": record["skill_sha256"], "probe_http_statuses": statuses,
                                       "report_item_id": record["report_item_id"],
                                       "passed": valid}
    if any(not entry["passed"] for entry in release["adoptions"].values()):
        previous = root / "formal" / "versions" / release["previous_sha256"] / ONCALL_SKILL / "SKILL.md"
        publish(root, previous, release["previous_sha256"], state=state, safe_sha256=release["previous_sha256"])
        state["active_sha256"] = release["previous_sha256"]
        release["status"] = "rolled_back"
        state.pop("pending_release", None)
        if release.get("candidate_id"):
            learning_ledger(root).recovered(release["candidate_id"], release["previous_sha256"],
                                            restoration_receipt(root, release["previous_sha256"]))
        write_json(root / "learning" / "state.json", state)
    elif set(release["adoptions"]) == set(ALIASES):
        if release.get("candidate_id"):
            learning_ledger(root).adopted(release["candidate_id"], {
                alias: {"skill_sha256": item["sha256"], "session_id": item["session_id"],
                        "report_item_id": item["report_item_id"], "verified": True}
                for alias, item in release["adoptions"].items()
            })
        release["status"] = "adopted"


def select_batch(root, state, completed):
    processed = set(state["processed_sessions"])
    selected = {}
    for entry in completed:
        alias, record = entry["instance"], entry["record"]
        if alias in selected or record["session_id"] in processed or record["skill_sha256"] != state["active_sha256"]:
            continue
        try:
            archive = incident_archive(root / "archives", alias, record["session_id"], group_id=GROUP,
                                       incident_id=record["incident_id"], skill_sha256=record["skill_sha256"])
        except (OSError, ValueError, KeyError):
            continue
        selected[alias] = {"archive": str(archive), "session_id": record["session_id"]}
    return selected if set(selected) == set(ALIASES) else None


class JobHeartbeat:
    """Renew one learning lease; failure fences evaluation/publication."""
    def __init__(self,store,lease):
        self.store,self.lease = store,lease
        self.stop = threading.Event()
        self.failure = None
        self.thread = threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.wait(5):
            try:
                self.store.heartbeat(self.lease,lease_seconds=30)
            except Exception as exc:
                self.failure = exc
                return

    def fence(self):
        if self.failure is not None:
            raise LeaseLost("Learning job heartbeat lost ownership") from self.failure
        self.store.heartbeat(self.lease,lease_seconds=30)

    def close(self):
        self.stop.set()
        self.thread.join()


def learn_once(root, live, module, options):
    """Serialize publication decisions and reload state committed by operator jobs."""
    from fleet_eval import evaluate
    from skillclaw_evolve import evolve
    from engineering_analysis import build_analysis

    status_path = root / "learning" / "state.json"
    with writer_lock(root / "learning", "governance.sqlite", timeout=1):
        state = load_json(status_path)
        state["governance_version"] = "sqlite-v1"
        try:
            recover_publication(root, state)
            reconcile_learning_ledger(root, state)
            completed = completed_incidents(root)
            check_adoption(root, state, completed)
            awaiting = (state.get("last_release", {}).get("status") == "awaiting_adoption"
                        or state.get("operator_rollback", {}).get("status") == "awaiting_verification")
            batch = None if awaiting else select_batch(root, state, completed)
            if batch:
                sessions = sorted(entry["session_id"] for entry in batch.values())
                key = hashlib.sha256("\n".join(sessions).encode()).hexdigest()[:16]
                jobs = ControlStore(root / "system" / "control.sqlite3")
                job = jobs.enqueue(scope=learning_ledger(root).namespace, queue="learning",
                    key=f"batch:{key}:{state['active_sha256']}", max_attempts=3,
                    payload={"baseline_sha256":state["active_sha256"], "sources": {
                        alias:{**entry,"archive_sha256":sha256(Path(entry["archive"]))}
                        for alias,entry in batch.items()}})
                if job["status"] in ("completed", "failed"):
                    state["processed_sessions"] = sorted(set(state["processed_sessions"]).union(sessions))
                    state.update(status="waiting",updated_at=time.time())
                    write_json(status_path,state)
                    return
                claimed = jobs.claim(f"learner-{os.getpid()}-{uuid4().hex}",queue="learning",
                                     task_id=job["id"],lease_seconds=30)
                if claimed is None:
                    return  # Another owner or a persisted retry delay is still active.
                lease,_ = claimed
                attempt = max(state["attempts"].get(key, 0) + 1, jobs.get(job["id"])["attempts"])
                state["attempts"][key] = attempt
                work = root / "learning" / "batches" / key / f"attempt-{attempt}"
                work.mkdir(parents=True)
                state.update(status="evaluating", batch=key, attempt=attempt)
                write_json(status_path, state)
                baseline = root / "formal" / "versions" / state["active_sha256"] / ONCALL_SKILL / "SKILL.md"
                result = {"sources": batch, "baseline_sha256": state["active_sha256"], "status": "keep"}
                heartbeat = JobHeartbeat(jobs,lease)
                try:
                    heartbeat.fence()
                    jobs.checkpoint(lease,{"stage":"generating","batch":key,"attempt":attempt})
                    records = {(entry["instance"], entry["record"]["session_id"]): entry["record"]
                               for entry in completed}
                    analysis_sources = []
                    for alias, entry in batch.items():
                        record = records[alias, entry["session_id"]]
                        worker = root / "patrols" / alias
                        analysis_sources.append({"instance": alias, "worker": worker, "record": record,
                                                 "branch_path": worker / "pages" / record["incident_id"] / "investigation.json"})
                    analysis = build_analysis(work, analysis_sources)
                    result["engineering_analysis"] = analysis
                    candidate, evolution = evolve(work / "staging", baseline,
                                                  [Path(entry["archive"]) for entry in batch.values()],
                                                  options if live else None, engineering_analysis=analysis)
                    result["evolution"] = evolution
                    if candidate is not None:
                        ledger = learning_ledger(root)
                        candidate_id = ledger.register(
                            scope=ledger.namespace, artifact_sha256=sha256(candidate),
                            parent_sha256=state["active_sha256"],
                            sources=[f"{alias}:{item['session_id']}:{sha256(Path(item['archive']))}"
                                     for alias, item in batch.items()], generator="skillclaw-workflow",
                            generation={"batch": key, "attempt": attempt, "model": options["model"],
                                        "mode": "live" if live else "local-script-model"},
                        )
                        coordinator = ImprovementCoordinator(ledger)
                        result["candidate_record"] = {"id": candidate_id, "ledger": "system/learning.sqlite3"}
                        heartbeat.fence()
                        jobs.checkpoint(lease,{"stage":"evaluating","candidate_id":candidate_id})
                        evaluation, report_digest = coordinator.evaluate(
                            candidate_id, candidate,
                            lambda: evaluate(work / "evaluation", baseline, candidate, options,
                                             release_policy=module.release_policy),
                        )
                        result["candidate_record"]["evaluation_sha256"] = report_digest
                        result["evaluation"] = evaluation
                        if evaluation["passed"]:
                            digest = evaluation["candidate_sha256"]
                            try:
                                heartbeat.fence()
                                jobs.checkpoint(lease,{"stage":"publishing","candidate_id":candidate_id,
                                                       "evaluation_sha256":report_digest})
                                result["release"] = coordinator.publish(
                                    candidate_id, candidate, list(ALIASES),
                                    lambda: publish(root, candidate, digest, state=state, candidate_id=candidate_id),
                                )
                            except Exception:
                                # A partial delivery must restore both the formal pointer and receivers.
                                result["rollback"] = recover_publication(root, state)
                                raise
                            committed = {
                                **state, "active_sha256": digest,
                                "processed_sessions": [*state["processed_sessions"], *sessions],
                                "last_release": {"sha256": digest, "previous_sha256": state["active_sha256"],
                                                 "published_at": time.time(), "status": "awaiting_adoption",
                                                 "candidate_id": candidate_id,
                                                 "publication_receipt": result["release"],
                                                 "adoptions": {}},
                            }
                            committed.pop("pending_release")
                            write_json(status_path, committed)
                            state = committed
                            result["status"] = "published"
                        else:
                            result["status"] = "rejected"
                    state["processed_sessions"] = sorted(set(state["processed_sessions"]).union(sessions))
                except Exception as exc:
                    result.update(status="failed", error=str(exc))
                    if attempt >= 3:
                        state["processed_sessions"].extend(sessions)
                        result["retry_exhausted"] = True
                finally:
                    heartbeat.close()
                write_json(work / "result.json", result)
                if result["status"] == "failed":
                    jobs.fail(lease,result["error"],retry_delay=2)
                else:
                    jobs.complete(lease,{"status":result["status"],"result":str(work / "result.json"),
                                         "candidate_record":result.get("candidate_record")})
                state["last_batch"] = {"path": str(work), "status": result["status"]}
            state.update(status="waiting", updated_at=time.time())
            write_json(status_path, state)
        except Exception as exc:
            state.update(status="error", error=str(exc), updated_at=time.time())
            write_json(status_path, state)
            if state.get("pending_release") is not None:
                raise


def learn(root, live, implementation=None):
    module = load_implementation(implementation or HERE / "workflow.py")
    with model_connection(not live) as url:
        options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if live else "local-fixture",
                   "model": os.environ.get("GLM_MODEL", "glm-5.2") if live else "patrol-fixture"}
        while True:
            try:
                learn_once(root, live, module, options)
            except RuntimeError as exc:
                if str(exc) != "This state is already in use by a patrol runtime command":
                    raise
                # Operator publication owns the same transaction. Keep workers
                # running and retry with freshly loaded state after it releases.
            time.sleep(2)


def _serve_owned(root, live, implementation, *, environment_states=None, oncall=True, learning=True, resume=False,
                 memory_config=None, enable_memory=False):
    ensure_loopback_no_proxy()
    root = Path(root).resolve()
    implementation = Path(implementation).resolve()
    patrol_config = patrol_configuration(implementation)
    attached = environment_states is not None
    if attached:
        if set(environment_states) != set(ALIASES):
            raise ValueError("Provide an environment directory for both instance-a and instance-b")
        environment_states = {alias: Path(path).resolve() for alias, path in environment_states.items()}
        if len(set(environment_states.values())) != len(ALIASES):
            raise ValueError("Each instance requires a separate HTTP environment")
    else:
        environment_states = {alias: root / "environments" / alias for alias in ALIASES}
    learning_enabled = learning and oncall
    (root / "learning").mkdir(exist_ok=True)
    binding = {"schema_version": 1, "mode": "live" if live else "local-script-model",
               "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if live else "patrol-fixture",
               "model_endpoint": (os.environ.get("GLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4") if live else "local",
               "implementation": str(implementation), "implementation_sha256": sha256(implementation),
               "patrol_config": patrol_config, "oncall_enabled": oncall,
               "learning_enabled": learning_enabled, "environment_ownership": "external" if attached else "service",
               "environments": {alias: str(path) for alias,path in environment_states.items()}}
    if memory_config is not None:
        memory_config = Path(memory_config).resolve()
        binding["memory_configuration"] = {"path": str(memory_config), "sha256": sha256(memory_config)}
    binding_path = root / "service-binding.json"
    existing = binding_path.is_file()
    if existing:
        if not resume:
            raise FileExistsError("Existing service state requires --resume")
        saved = load_json(binding_path)
        if saved["configuration"] != binding:
            previous = saved["configuration"]
            without_memory = {key: value for key, value in binding.items() if key != "memory_configuration"}
            if (enable_memory and memory_config is not None and "memory_configuration" not in previous
                    and previous == without_memory):
                saved.setdefault("migrations", []).append({"kind": "enable-openviking-memory",
                    "at": time.time(), "previous_configuration": previous, "configuration": binding})
                saved["configuration"] = binding
                write_json(binding_path, saved)
            else:
                raise ValueError("Cannot resume with different mode, implementation or environment bindings")
    elif any(root.iterdir()) and (root / "service.json").exists():
        raise ValueError("Existing service state has no verified binding; migrate explicitly")
    else:
        saved = {"configuration": binding, "environment_ids": {}}
        write_json(binding_path, saved)
    if attached and existing:
        from environment import get_snapshot
        for alias,path in environment_states.items():
            if get_snapshot(path,SERVICES[0])["environment_id"] != saved["environment_ids"].get(alias):
                raise ValueError("External environment scope changed; refusing to resume old incidents")
    status = {"status": "starting", "mode": "live" if live else "local-script-model", "root": str(root),
              "environments": {alias: str(path) for alias, path in environment_states.items()},
              "environment_ownership": "external" if attached else "service",
              "patrol_config": patrol_config,
              "oncall_enabled": oncall, "learning_enabled": learning_enabled, "learner_pid": None}
    status_path = root / "service.json"
    try:
        with model_connection(not live) as url, ExitStack() as stack:
            options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if live else "local-fixture",
                       "model": os.environ.get("GLM_MODEL", "glm-5.2") if live else "patrol-fixture"}
            if existing and (root / "learning" / "state.json").is_file():
                state = load_json(root / "learning" / "state.json")
                recover_publication(root, state)
                reconcile_learning_ledger(root, state)
                restoration_receipt(root,state["active_sha256"])
            else:
                publish(root, SEED, sha256(SEED))
                write_json(root / "learning" / "state.json", {"status": "waiting" if learning_enabled else "disabled",
                                                               "active_sha256": sha256(SEED),
                                                               "processed_sessions": [], "attempts": {}})
            workers, environments = {}, {}
            for alias in ALIASES:
                environment = environment_states[alias]
                if not attached:
                    environment.mkdir(parents=True, exist_ok=resume)
                    env_command = [sys.executable, "-B", str(HERE / "patrol/environment.py"),
                                   "--state", str(environment), "serve"]
                    if resume:
                        env_command.append("--resume")
                    environments[alias] = start_process(stack, env_command,
                                                       environment / "environment.log")
                elif not (environment / "environment.json").is_file():
                    raise RuntimeError(f"{alias}: start the external HTTP environment before attaching")
                deadline = time.monotonic() + 30
                while not attached:
                    process_state = environment / "environment-process.json"
                    if (process_state.is_file()
                            and load_json(process_state).get("pid") == environments[alias].pid
                            and (environment / "environment.json").is_file()):
                        break
                    if environments[alias].poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f"{alias}: HTTP environment failed to start")
                    time.sleep(.1)
                from environment import get_snapshot
                identity = get_snapshot(environment,SERVICES[0])["environment_id"]
                previous = saved["environment_ids"].get(alias)
                if previous is not None and previous != identity:
                    raise ValueError("HTTP environment scope changed across restart")
                saved["environment_ids"][alias] = identity
                write_json(binding_path,saved)
            memory_binding = None
            if memory_config is not None:
                from apps.oncall.memory import bind_configuration
                memory_binding = bind_configuration(memory_config, root, saved["environment_ids"])
                previous = os.environ.get("ONCALL_MEMORY_CONFIG")
                os.environ["ONCALL_MEMORY_CONFIG"] = str(memory_binding)
                def restore_memory_environment():
                    if previous is None:
                        os.environ.pop("ONCALL_MEMORY_CONFIG", None)
                    else:
                        os.environ["ONCALL_MEMORY_CONFIG"] = previous
                stack.callback(restore_memory_environment)
            for alias in ALIASES:
                environment = environment_states[alias]
                receiver = root / "receivers" / alias
                proxy = stack.enter_context(client_proxy(root / "archives", alias, receiver, options,
                                                         offline=not live))
                worker = root / "patrols" / alias
                worker.mkdir(parents=True, exist_ok=resume)
                command = [sys.executable, "-B", str(HERE / "patrol/patrol.py"), "--state", str(worker),
                           "run", "--environment-state", str(environment),
                           "--skill-file", str(receiver / ONCALL_SKILL / "SKILL.md"), "--capture-url", proxy,
                           "--interval", str(patrol_config["interval"]), "--sample", str(patrol_config["sample"]),
                           "--implementation", str(implementation)]
                for service in patrol_config["services"]:
                    command.extend(("--service", service))
                if oncall:
                    command.extend(("--oncall", "--page-oncall"))
                if not live:
                    command.append("--offline")
                workers[alias] = start_process(stack, command, worker / "patrol.log")
            memory_command = [sys.executable, "-B", str(Path(__file__).resolve()), "--out", str(root),
                              "--memory-worker", "--memory-config", str(memory_binding)]
            if live:
                memory_command.append("--live")
            memory_worker = (start_process(stack, memory_command, root / "system" / "memory-worker.log")
                             if memory_binding else None)
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--out", str(root), "--learn-worker",
                       "--implementation", str(implementation)]
            if live:
                command.append("--live")
            learner = start_process(stack, command, root / "learning" / "worker.log") if learning_enabled else None
            status.update(status="running", workers={alias: process.pid for alias, process in workers.items()},
                          learner_pid=learner.pid if learner is not None else None,
                          memory_backend="openviking" if memory_binding else "disabled",
                          memory_binding=str(memory_binding) if memory_binding else None,
                          memory_worker_pid=memory_worker.pid if memory_worker else None)
            write_json(status_path, status)
            print(f"On-call service running. State: {root}", flush=True)
            restarts = 0
            memory_restarts = 0
            while True:
                for alias, process in {**environments, **{f"{alias}-patrol": child for alias, child in workers.items()}}.items():
                    if process.poll() is not None:
                        raise RuntimeError(f"{alias} stopped with exit code {process.returncode}")
                if learner is not None and learner.poll() is not None and restarts < 3:
                    restarts += 1
                    learner = start_process(stack, command, root / "learning" / "worker.log")
                    status.update(learner_pid=learner.pid, learner_restarts=restarts)
                if memory_worker is not None and memory_worker.poll() is not None and memory_restarts < 3:
                    memory_restarts += 1
                    memory_worker = start_process(stack, memory_command, root / "system" / "memory-worker.log")
                    status.update(memory_worker_pid=memory_worker.pid, memory_worker_restarts=memory_restarts)
                status["memory_available"] = memory_worker is not None and memory_worker.poll() is None
                if memory_binding:
                    memory_status = Path(load_json(memory_binding)["state_dir"]) / "status.json"
                    status["memory"] = load_json(memory_status) if memory_status.is_file() else {"status": "starting"}
                status.update(learning_available=learner is not None and learner.poll() is None, updated_at=time.time())
                write_json(status_path, status)
                time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        status["status"] = "stopped"
    except BaseException as exc:
        status.update(status="failed", error=str(exc))
        raise
    finally:
        status["stopped_at"] = time.time()
        write_json(status_path, status)


@contextmanager
def service_owner(root):
    """A distinct process lifetime lock, held while children and learners run."""
    with (root / ".service-owner.lock").open("a+") as owner:
        try:
            fcntl.flock(owner,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This service state already has an owner") from None
        try:
            yield
        finally:
            fcntl.flock(owner,fcntl.LOCK_UN)


def serve(root,live,implementation,*,environment_states=None,oncall=True,learning=True,resume=False,
          memory_config=None,enable_memory=False):
    root = Path(root).resolve()
    root.mkdir(parents=True,exist_ok=resume)
    with service_owner(root):
        return _serve_owned(root,live,implementation,environment_states=environment_states,
                            oncall=oncall,learning=learning,resume=resume,memory_config=memory_config,
                            enable_memory=enable_memory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New directory for this continuous service")
    parser.add_argument("--resume", action="store_true", help="Resume the same bound state, preserving approved skills and checkpoints")
    parser.add_argument("--live", action="store_true", help="Use the configured model instead of local scripted responses")
    parser.add_argument("--implementation", type=Path, default=HERE / "workflow.py")
    parser.add_argument("--environment-a", type=Path, help="Attach to an already running instance-a environment")
    parser.add_argument("--environment-b", type=Path, help="Attach to an already running instance-b environment")
    parser.add_argument("--patrol-only", action="store_true", help="Run scheduled inspections before enabling On-call handling")
    parser.add_argument("--no-learning", action="store_true", help="Run On-call workers without the background learner")
    parser.add_argument("--learn-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--memory-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--memory-config", type=Path, help="OpenViking deployment memory configuration")
    parser.add_argument("--enable-memory", action="store_true",
                        help="Explicitly add memory to an otherwise unchanged existing service binding")
    args = parser.parse_args(argv)
    ensure_loopback_no_proxy()
    if (args.environment_a is None) != (args.environment_b is None):
        parser.error("--environment-a and --environment-b must be provided together")
    if args.memory_worker:
        if not args.memory_config:
            parser.error("Memory worker requires its actual bound configuration")
        from apps.oncall.memory import main as run_memory
        command = ["--root", str(args.out.resolve()), "--config", str(args.memory_config.resolve())]
        if args.live:
            command.append("--live")
        run_memory(command)
    elif args.learn_worker:
        learn(args.out.resolve(), args.live, args.implementation.resolve())
    else:
        environments = None if args.environment_a is None else {
            "instance-a": args.environment_a, "instance-b": args.environment_b,
        }
        serve(args.out.resolve(), args.live, args.implementation.resolve(), environment_states=environments,
              oncall=not args.patrol_only, learning=not args.no_learning, resume=args.resume,
              memory_config=args.memory_config, enable_memory=args.enable_memory)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    main()
