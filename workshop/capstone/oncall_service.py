"""Keep two Hermes On-call workers and their background learning service running."""

import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "patrol"))

from environment import SERVICES, load_json, write_json
from fleet import GROUP, SEED, receipt, sha256
from hermes_runtime import ONCALL_SKILL, ensure_loopback_no_proxy
from implementation import load_implementation
from patrol import connect, incidents, model_connection, writer_lock
from skillclaw_bridge import _bound_port, incident_archive

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

    sys.path.insert(0, str(HERE.parents[1] / "examples"))
    from skillclaw_lab.compat import structured_tool_status_compat

    record_dir = root / "proxy" / alias
    record_dir.mkdir(parents=True)
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


def publish(root, source, digest, *, state=None, safe_sha256=None):
    """Pull approved bytes with native clients before exposing them to workers."""
    from skillclaw_evolve import release

    if state is not None:
        state["pending_release"] = {
            "previous_sha256": safe_sha256 or state["active_sha256"],
            "candidate_sha256": digest, "source": str(source.resolve()), "started_at": time.time(),
        }
        write_json(root / "learning" / "state.json", state)
    delivery = root / "learning" / "deliveries" / uuid4().hex
    receivers = {alias: delivery / alias for alias in ALIASES}
    result = release(root / "formal", source, digest, receivers)
    content = (root / "formal" / "versions" / digest / ONCALL_SKILL / "SKILL.md").read_bytes()
    for alias in ALIASES:
        target = root / "receivers" / alias / ONCALL_SKILL / "SKILL.md"
        replace_skill(target, content)
    return result


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
                                       "passed": valid}
    if any(not entry["passed"] for entry in release["adoptions"].values()):
        previous = root / "formal" / "versions" / release["previous_sha256"] / ONCALL_SKILL / "SKILL.md"
        publish(root, previous, release["previous_sha256"], state=state, safe_sha256=release["previous_sha256"])
        state["active_sha256"] = release["previous_sha256"]
        release["status"] = "rolled_back"
        state.pop("pending_release", None)
        write_json(root / "learning" / "state.json", state)
    elif set(release["adoptions"]) == set(ALIASES):
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
            completed = completed_incidents(root)
            check_adoption(root, state, completed)
            awaiting = (state.get("last_release", {}).get("status") == "awaiting_adoption"
                        or state.get("operator_rollback", {}).get("status") == "awaiting_verification")
            batch = None if awaiting else select_batch(root, state, completed)
            if batch:
                sessions = sorted(entry["session_id"] for entry in batch.values())
                key = hashlib.sha256("\n".join(sessions).encode()).hexdigest()[:16]
                attempt = state["attempts"].get(key, 0) + 1
                state["attempts"][key] = attempt
                work = root / "learning" / "batches" / key / f"attempt-{attempt}"
                work.mkdir(parents=True)
                state.update(status="evaluating", batch=key, attempt=attempt)
                write_json(status_path, state)
                baseline = root / "formal" / "versions" / state["active_sha256"] / ONCALL_SKILL / "SKILL.md"
                result = {"sources": batch, "baseline_sha256": state["active_sha256"], "status": "keep"}
                try:
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
                        evaluation = evaluate(work / "evaluation", baseline, candidate, options,
                                              release_policy=module.release_policy)
                        result["evaluation"] = evaluation
                        if evaluation["passed"]:
                            digest = evaluation["candidate_sha256"]
                            try:
                                result["release"] = publish(root, candidate, digest, state=state)
                            except Exception:
                                # A partial delivery must restore both the formal pointer and receivers.
                                result["rollback"] = recover_publication(root, state)
                                raise
                            committed = {
                                **state, "active_sha256": digest,
                                "processed_sessions": [*state["processed_sessions"], *sessions],
                                "last_release": {"sha256": digest, "previous_sha256": state["active_sha256"],
                                                 "published_at": time.time(), "status": "awaiting_adoption",
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
                write_json(work / "result.json", result)
                state["last_batch"] = {"path": str(work), "status": result["status"]}
            state.update(status="waiting", updated_at=time.time())
            write_json(status_path, state)
        except Exception as exc:
            state.update(status="error", error=str(exc), updated_at=time.time())
            write_json(status_path, state)
            if state.get("pending_release") is not None:
                raise


def learn(root, live, implementation=None):
    module = load_implementation(implementation or HERE / "self_improvement_agent.py")
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


def serve(root, live, implementation, *, environment_states=None, oncall=True, learning=True):
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
    root.mkdir(parents=True, exist_ok=False)
    (root / "learning").mkdir()
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
            publish(root, SEED, sha256(SEED))
            write_json(root / "learning" / "state.json", {"status": "waiting" if learning_enabled else "disabled",
                                                           "active_sha256": sha256(SEED),
                                                           "processed_sessions": [], "attempts": {}})
            workers, environments = {}, {}
            for alias in ALIASES:
                environment = environment_states[alias]
                if not attached:
                    environment.mkdir(parents=True)
                    environments[alias] = start_process(stack, [sys.executable, "-B", str(HERE / "patrol/environment.py"),
                                                                "--state", str(environment), "serve"],
                                                       environment / "environment.log")
                elif not (environment / "environment.json").is_file():
                    raise RuntimeError(f"{alias}: start the external HTTP environment before attaching")
                deadline = time.monotonic() + 30
                while not (environment / "environment.json").is_file():
                    if environments[alias].poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f"{alias}: HTTP environment failed to start")
                    time.sleep(.1)
                receiver = root / "receivers" / alias
                proxy = stack.enter_context(client_proxy(root / "archives", alias, receiver, options,
                                                         offline=not live))
                worker = root / "patrols" / alias
                worker.mkdir(parents=True)
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
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--out", str(root), "--learn-worker",
                       "--implementation", str(implementation)]
            if live:
                command.append("--live")
            learner = start_process(stack, command, root / "learning" / "worker.log") if learning_enabled else None
            status.update(status="running", workers={alias: process.pid for alias, process in workers.items()},
                          learner_pid=learner.pid if learner is not None else None)
            write_json(status_path, status)
            print(f"On-call service running. State: {root}", flush=True)
            restarts = 0
            while True:
                for alias, process in {**environments, **{f"{alias}-patrol": child for alias, child in workers.items()}}.items():
                    if process.poll() is not None:
                        raise RuntimeError(f"{alias} stopped with exit code {process.returncode}")
                if learner is not None and learner.poll() is not None and restarts < 3:
                    restarts += 1
                    learner = start_process(stack, command, root / "learning" / "worker.log")
                    status.update(learner_pid=learner.pid, learner_restarts=restarts)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New directory for this continuous service")
    parser.add_argument("--live", action="store_true", help="Use the configured model instead of local scripted responses")
    parser.add_argument("--implementation", type=Path, default=HERE / "self_improvement_agent.py")
    parser.add_argument("--environment-a", type=Path, help="Attach to an already running instance-a environment")
    parser.add_argument("--environment-b", type=Path, help="Attach to an already running instance-b environment")
    parser.add_argument("--patrol-only", action="store_true", help="Run scheduled inspections before enabling On-call handling")
    parser.add_argument("--no-learning", action="store_true", help="Run On-call workers without the background learner")
    parser.add_argument("--learn-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    ensure_loopback_no_proxy()
    if (args.environment_a is None) != (args.environment_b is None):
        parser.error("--environment-a and --environment-b must be provided together")
    if args.learn_worker:
        learn(args.out.resolve(), args.live, args.implementation.resolve())
    else:
        environments = None if args.environment_a is None else {
            "instance-a": args.environment_a, "instance-b": args.environment_b,
        }
        serve(args.out.resolve(), args.live, args.implementation.resolve(), environment_states=environments,
              oncall=not args.patrol_only, learning=not args.no_learning)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    main()
