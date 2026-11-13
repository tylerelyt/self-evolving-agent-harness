"""Approve a known Skill rollback, publish it natively and verify both workers' scopes."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "patrol"))

from environment import SERVICES, get_snapshot, load_json, probe_service, write_json
from export_workset import recorded_probes, scoped_snapshot, session_rows, verify_skill
from fleet import receipt
from hermes_runtime import (ONCALL_SKILL, ensure_loopback_no_proxy, install_oncall_skill,
                            open_sessions, read_oncall_skill, run_turn)
from local_model import serve as scripted_model
from oncall_service import ALIASES, publish, recover_publication, sha256
from patrol import connect, decode_report, incidents, model_connection, writer_lock
from replay_workset import REPLAY_SOURCE, run_replay


SCHEMA = "oncall-workbench-rollback-v1"
PREFIX = "ONCALL_ROLLBACK_VERIFY "
REPEAT = "首次探测返回 5xx 时，再调用一次 oncall_probe"


def inside(root, path):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Rollback resources must belong to this workbench")
    return path


def request_directory(root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("Select a recorded rollback request")
    return inside(root, root / "rollbacks" / identifier)


def current_run(root, expected_run=None):
    record = load_json(root / "workbench.json")
    identifier = record.get("active_run")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("Start an active On-call service before preparing rollback")
    run = inside(root, root / "runs" / identifier)
    if expected_run is not None and run != Path(expected_run).resolve():
        raise ValueError("The active On-call run changed after this request")
    service = load_json(run / "service.json")
    if (service["status"] != "running" or service["oncall_enabled"] is not True
            or service["mode"] not in ("live", "local-script-model")):
        raise ValueError("Rollback requires running On-call workers")
    inside(run, run / "learning")
    if (service.get("learning_enabled")
            and load_json(run / "learning" / "state.json").get("governance_version") != "sqlite-v1"):
        raise ValueError("Restart the learner to enable governed publication before rollback")
    for alias in ALIASES:
        pid = service["workers"][alias]
        if type(pid) is not int or pid <= 0:
            raise ValueError("The On-call worker has no valid process")
        os.kill(pid, 0)
        inside(root, service["environments"][alias])
    return run, service


def known_version(run, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Skill version must be a recorded SHA-256")
    source = inside(run, run / "formal" / "versions" / digest / ONCALL_SKILL / "SKILL.md")
    if sha256(source) != digest:
        raise ValueError("The known Skill snapshot has changed")
    return source


def approved_release(run, state):
    release = state.get("last_release", {})
    active = state["active_sha256"]
    previous = release.get("previous_sha256")
    if (state.get("pending_release") or state.get("operator_rollback", {}).get("status") == "awaiting_verification"
            or release.get("status") != "adopted" or release.get("sha256") != active or previous == active
            or set(release.get("adoptions", {})) != set(ALIASES)
            or any(entry.get("passed") is not True for entry in release["adoptions"].values())):
        raise ValueError("Rollback requires a release adopted by both instances and its prior version")
    known_version(run, active)
    known_version(run, previous)
    for alias in ALIASES:
        receiver = inside(run, run / "receivers" / alias / ONCALL_SKILL / "SKILL.md")
        if sha256(receiver) != active:
            raise ValueError("Both instances must hold the adopted version before rollback")
    return active, previous


def prepare(workbench_root, run_root=None):
    root = Path(workbench_root).resolve()
    run, service = current_run(root, run_root)
    with writer_lock(run / "learning", "governance.sqlite", timeout=1):
        state = load_json(run / "learning" / "state.json")
        active, previous = approved_release(run, state)
        identifier = uuid4().hex
        directory = request_directory(root, identifier)
        directory.mkdir(parents=True)
        request = {"schema": SCHEMA, "id": identifier, "status": "awaiting_approval", "run": str(run),
                   "from_sha256": active, "to_sha256": previous, "requested_at": time.time(),
                   "environments": service["environments"], "mode": service["mode"],
                   "adopted_release": state["last_release"], "approval_required": True}
        write_json(directory / "request.json", request)
        return request


def read_request(root, identifier):
    directory = request_directory(root, identifier)
    request = load_json(directory / "request.json")
    if request.get("schema") != SCHEMA or request.get("id") != identifier:
        raise ValueError("The rollback request is invalid")
    return directory, request


def approve(workbench_root, identifier, expected_active_sha256, expected_previous_sha256, *, request_context=None):
    root = Path(workbench_root).resolve()
    directory, request = read_request(root, identifier)
    run, _ = current_run(root, request["run"])
    if (expected_active_sha256 != request["from_sha256"]
            or expected_previous_sha256 != request["to_sha256"]):
        raise ValueError("Approval must name both displayed Skill hashes")
    with writer_lock(run / "learning", "governance.sqlite", timeout=1):
        state = load_json(run / "learning" / "state.json")
        if approved_release(run, state) != (expected_active_sha256, expected_previous_sha256):
            raise ValueError("The active Skill changed after preparing rollback")
        path = directory / "approval.json"
        if path.is_file():
            return load_json(path)
        approval = {"request_id": identifier, "run": str(run), "from_sha256": expected_active_sha256,
                    "to_sha256": expected_previous_sha256, "actor": "local-workbench-user",
                    "source": "local_workbench_http" if request_context is not None else "local_workbench_api",
                    "approved_at": time.time()}
        write_json(path, approval)
        return approval


def scripted_reply(messages):
    tools, calls, task = [], {}, None
    for message in messages:
        if message["role"] == "user":
            tools, calls = [], {}
            task = json.loads(message["content"].partition("\n")[0].removeprefix(PREFIX))
        elif message["role"] == "assistant":
            calls.update({call["id"]: call["function"]["name"] for call in message.get("tool_calls") or []})
        elif message["role"] == "tool":
            tools.append((calls[message["tool_call_id"]], json.loads(message["content"])))
    if not tools:
        return "skill_view", {"name": ONCALL_SKILL}
    if len(tools) == 1:
        return "service_snapshot", {}
    probes = [value for name, value in tools if name == "oncall_probe"]
    if not probes or (len(probes) == 1 and probes[0]["http_status"] >= 500 and REPEAT in tools[0][1]["content"]):
        return "oncall_probe", {}
    return None, {**task, "probe_http_statuses": [probe["http_status"] for probe in probes]}


def verify_task(directory, source, environment, service, expected_sha, options):
    directory.mkdir(parents=True)
    session = "rollback-" + uuid4().hex
    initial = get_snapshot(environment, service)
    task = {"service": service, "environment_id": initial["environment_id"], "skill_sha256": expected_sha}
    skill_read = False
    snapshot_read = False

    def skill():
        nonlocal skill_read
        value = read_oncall_skill(expected_sha)
        skill_read = True
        return value

    def snapshot():
        nonlocal snapshot_read
        if not skill_read:
            raise ValueError("Read the restored Skill before querying metrics")
        value = get_snapshot(environment, service)
        if value["environment_id"] != task["environment_id"]:
            raise ValueError("The verification environment changed")
        snapshot_read = True
        return value

    def probe():
        if not snapshot_read:
            raise ValueError("Read the current scoped metrics before probing")
        return probe_service(environment, service, environment_id=task["environment_id"])

    with open_sessions(directory, fresh=True) as db:
        if install_oncall_skill(directory, source) != expected_sha:
            raise ValueError("The restored Skill changed before verification")
        rows, _ = run_turn(db, session, PREFIX + json.dumps(task) + "\n" + (
            "先用 skill_view 读取本轮指定的 On-call Skill，再用 service_snapshot 查询当前服务指标，"
            "按 Skill 调用 oncall_probe 做一次有界复核。只报告实际回执。最后提交 JSON，包含 "
            "service、environment_id、skill_sha256 和按序列出的 probe_http_statuses。"),
            {"skill_view": skill, "service_snapshot": snapshot, "oncall_probe": probe}, options)
    proof = verify_rows(rows, task)
    result = {"session_id": session, **task, **proof, "verified_at": time.time(),
              "database": str(directory / "hermes" / "state.db")}
    write_json(directory / "result.json", result)
    return result


def verify_rows(rows, task):
    calls, receipts = {}, []
    for row in rows:
        for call in row.get("tool_calls") or []:
            name = call["function"]["name"]
            arguments = json.loads(call["function"]["arguments"])
            if (row["role"] != "assistant" or call["id"] in calls
                    or arguments != ({"name": ONCALL_SKILL} if name == "skill_view" else {})):
                raise ValueError("Unexpected parameters in rollback verification")
            calls[call["id"]] = (name, row["id"])
        if row["role"] == "tool":
            name, call_item = calls.pop(row["tool_call_id"])
            if row["tool_name"] != name or call_item >= row["id"]:
                raise ValueError("The verification tool receipt has no earlier call")
            receipts.append({"name": name, "call_item_id": call_item, "item_id": row["id"],
                             "receipt": json.loads(row["content"])})
    if calls or [item["name"] for item in receipts[:2]] != ["skill_view", "service_snapshot"]:
        raise ValueError("Verification did not read the Skill and current snapshot")
    skill = receipts[0]["receipt"]
    if (skill.get("success") is not True or skill.get("name") != ONCALL_SKILL
            or skill.get("sha256") != task["skill_sha256"]
            or hashlib.sha256(skill["content"].encode()).hexdigest() != task["skill_sha256"]):
        raise ValueError("The native Skill read does not match the restored version")
    observation = receipts[1]["receipt"]
    if (observation["service"] != task["service"] or observation["environment_id"] != task["environment_id"]
            or observation["source"] != "live_lab_http_requests"):
        raise ValueError("Rollback verification queried the wrong environment")
    probes = receipts[2:]
    if not 1 <= len(probes) <= 2:
        raise ValueError("Rollback verification requires one or two actual probes")
    for previous, event in zip(receipts, receipts[1:]):
        if previous["item_id"] >= event["call_item_id"]:
            raise ValueError("Rollback verification tools were not sequential")
    for event in probes:
        value = event["receipt"]
        if (event["name"] != "oncall_probe" or value.get("service") != task["service"]
                or value.get("environment_id") != task["environment_id"]
                or type(value.get("http_status")) is not int or value["http_status"] // 100 not in (2, 5)
                or value["checked_at"] < observation["window_end"]):
            raise ValueError("Rollback verification lacks a scoped HTTP probe")
    final = rows[-1]
    report = decode_report(final["content"])
    statuses = [event["receipt"]["http_status"] for event in probes]
    if (final["role"] != "assistant" or final["id"] <= probes[-1]["item_id"]
            or any(report.get(key) != value for key, value in task.items())
            or report.get("probe_http_statuses") != statuses):
        raise ValueError("Rollback verification report differs from the actual receipts")
    return {"passed": True, "skill_item_id": receipts[0]["item_id"], "report_item_id": final["id"],
            "probe_http_statuses": statuses, "tool_receipts": receipts, "report": report}


def patrol_checkpoints(run):
    evidence = {}
    for alias in ALIASES:
        worker = run / "patrols" / alias
        with closing(connect(worker, readonly=True)) as db:
            row = db.execute("SELECT * FROM inspections ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError("Both instances must have a committed patrol checkpoint")
        evidence[alias] = {key: row[key] for key in ("id", "service", "checked_at", "session_id", "report_item_id")}
    return evidence


def continued_patrol(root, run, before, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_run(root, run)
        after = patrol_checkpoints(run)
        if all(after[alias]["id"] > before[alias]["id"] for alias in ALIASES):
            return after
        time.sleep(.5)
    raise RuntimeError("Both workers must continue committing patrol records after rollback")


def frozen_incident(run, alias, request):
    """Verify the original adopted incident before reusing its captured observations."""
    adopted = request["adopted_release"]["adoptions"][alias]
    worker = inside(run, run / "patrols" / alias)
    record = None
    with closing(connect(worker, readonly=True)) as db:
        for service in SERVICES:
            record = next((entry for entry in incidents(db, service)
                           if entry["incident_id"] == adopted["incident_id"]), None)
            if record is not None:
                break
    if (record is None or adopted.get("passed") is not True
            or record["session_id"] != adopted["session_id"]
            or record["skill_sha256"] != adopted["sha256"] or adopted["sha256"] != request["from_sha256"]):
        raise ValueError("Frozen replay source must be this release's original adopted incident")
    observation = json.loads(record["observation"])
    snapshot = scoped_snapshot(observation, observation["service"], observation["environment_id"])
    proof = receipt(worker, record)
    database = inside(run, worker / "hermes" / "state.db")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = session_rows(db, record["session_id"], record["skill_item_id"], record["report_item_id"])
    verify_skill(rows[record["skill_item_id"]], request["from_sha256"])
    scope = {"service": snapshot["service"], "environment_id": snapshot["environment_id"]}
    statuses = recorded_probes(rows, proof["probes"], "oncall_probe", scope,
                               record["skill_item_id"], record["report_item_id"])
    report = decode_report(rows[record["report_item_id"]]["content"])
    if (statuses != adopted["probe_http_statuses"] or report.get("probe_http_statuses") != statuses
            or len(statuses) != 2 or statuses[0] // 100 != 5):
        raise ValueError("Behavior replay requires the adopted incident's recorded 5xx and second probe")
    return {"incident_id": record["incident_id"], "source_session_id": record["session_id"],
            "source_report_item_id": record["report_item_id"], "source_skill_item_id": record["skill_item_id"],
            "source_snapshot": snapshot, "probe_http_statuses": statuses}


def replay_behavior(directory, run, request, *, live=False):
    """Contrast the exact captured case using restored and formerly adopted Skill bytes."""
    result = {}
    with model_connection(not live) as url:
        options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if live else "local-fixture",
                   "model": os.environ.get("GLM_MODEL", "glm-5.2") if live else "patrol-fixture"}
        for alias in ALIASES:
            source = frozen_incident(run, alias, request)
            snapshot = {**source["source_snapshot"], "source": REPLAY_SOURCE}
            arms = {}
            restored_text = known_version(run, request["to_sha256"]).read_text(encoding="utf-8")
            for name, digest in (("restored", request["to_sha256"]), ("adopted", request["from_sha256"])):
                state = directory / alias / name
                state.mkdir(parents=True)
                arms[name] = run_replay(state, known_version(run, digest), options,
                                       snapshot, source["probe_http_statuses"])
                arms[name]["database"] = str(state / "hermes" / "state.db")
                if (not arms[name]["trace_verified"] or arms[name]["status"] != "observed"
                        or arms[name]["skill_sha256"] != digest):
                    raise ValueError("Frozen behavior replay lacks a verified native execution")
            original = source["probe_http_statuses"]
            restored_statuses = original[:1]
            restored_assessment = "failure_observed"
            if REPEAT in restored_text:
                restored_statuses = original
                restored_assessment = "persistent" if original[1] >= 500 else "intermittent"
            behavior_restored = (arms["restored"]["probe_statuses"] == restored_statuses
                                 and arms["adopted"]["probe_statuses"] == original
                                 and arms["restored"]["reported_assessment"] == restored_assessment
                                 and arms["restored"]["session_id"] != arms["adopted"]["session_id"])
            if not behavior_restored:
                raise ValueError("Frozen incident did not demonstrate the prior Skill's probe behavior")
            result[alias] = {"source": "frozen_incident_replay", **source, "snapshot": snapshot,
                             "snapshot_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
                             "behavior_restored": True, "behavior_changed": restored_statuses != original, **arms}
            write_json(directory / alias / "comparison.json", result[alias])
    return result


def commit_completion(state_path, state, directory, result):
    """Commit authority last, without changing the in-memory pending record first."""
    committed = {**state, "operator_rollback": {
        **state["operator_rollback"], "status": "verified",
        "result": str(directory / "result.json"), "verified_at": result["completed_at"],
    }}
    write_json(state_path, committed)


def resume_completion(root, run, directory, request, state, service):
    """Recover a pinned completed attempt without repeating publication or tasks."""
    operator = state["operator_rollback"]
    completion = operator.get("completion")
    if completion is None:
        # Older interrupted runs have no trusted completion journal. An explicit
        # retry still uses the normal approved publication/verification path.
        return None
    if (not isinstance(completion, dict)
            or completion.get("request_id") != request["id"]
            or not isinstance(completion.get("artifact"), str)
            or not re.fullmatch(r"completions/[0-9a-f]{32}\.json", completion["artifact"])
            or not isinstance(completion.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", completion["sha256"])):
        raise ValueError("The rollback completion journal is invalid")
    artifact = inside(directory, directory / completion["artifact"])
    if sha256(artifact) != completion["sha256"]:
        raise ValueError("The rollback completion evidence changed")
    result = load_json(artifact)
    expected = {key: request[key] for key in ("run", "from_sha256", "to_sha256")}
    if (result.get("schema") != SCHEMA or result.get("request_id") != request["id"]
            or result.get("status") != "verified"
            or any(result.get(key) != value for key, value in expected.items())
            or state.get("pending_release") or state["active_sha256"] != request["to_sha256"]
            or state.get("last_release", {}).get("status") != "rolled_back"
            or state["last_release"].get("rollback_request_id") != request["id"]):
        raise ValueError("The completed rollback no longer matches the authoritative version")
    known_version(run, request["to_sha256"])
    for alias in ALIASES:
        if service["environments"][alias] != request["environments"][alias]:
            raise ValueError("The active environment changed after approval")
        inside(root, request["environments"][alias])
        receiver = inside(run, run / "receivers" / alias / ONCALL_SKILL / "SKILL.md")
        if sha256(receiver) != request["to_sha256"]:
            raise ValueError("The restored receiver changed before completion could be committed")
    write_json(directory / "result.json", result)
    commit_completion(run / "learning/state.json", state, directory, result)
    return result


def execute(workbench_root, identifier, live=False):
    root = Path(workbench_root).resolve()
    directory, request = read_request(root, identifier)
    approval = load_json(directory / "approval.json")
    expected = {key: request[key] for key in ("run", "from_sha256", "to_sha256")}
    if (approval.get("request_id") != identifier or approval.get("actor") != "local-workbench-user"
            or any(approval.get(key) != value for key, value in expected.items())):
        raise ValueError("Rollback needs explicit local approval for this run and these Skill hashes")
    run, service = current_run(root, request["run"])
    if live != (request["mode"] == "live") or service["mode"] != request["mode"]:
        raise ValueError("Use the active On-call service's model mode for rollback verification")
    ensure_loopback_no_proxy()
    with writer_lock(run / "learning", "governance.sqlite", timeout=1):
        state_path = run / "learning" / "state.json"
        state = load_json(state_path)
        prior = state.get("operator_rollback")
        resuming = prior is not None and prior.get("request_id") == identifier
        if resuming and prior["status"] == "verified":
            result = load_json(directory / "result.json")
            if "behavior_replay" not in result:
                result["behavior_replay"] = replay_behavior(directory / "behavior-replay" / uuid4().hex,
                                                           run, request, live=live)
                result["behavior_replay_verified_at"] = time.time()
                write_json(directory / "result.json", result)
            return result
        if resuming:
            completed = resume_completion(root, run, directory, request, state, service)
            if completed is not None:
                return completed
        before = patrol_checkpoints(run)
        if not resuming:
            if approved_release(run, state) != (request["from_sha256"], request["to_sha256"]):
                raise ValueError("The adopted Skill changed after approval")
            state["operator_rollback"] = {"request_id": identifier, "status": "awaiting_verification", **expected}
            write_json(state_path, state)
        elif state["active_sha256"] not in (request["from_sha256"], request["to_sha256"]):
            raise ValueError("A different release superseded this rollback")
        restored = known_version(run, request["to_sha256"])
        try:
            if state.get("pending_release"):
                recover_publication(run, state)
            publication = publish(run, restored, request["to_sha256"], state=state,
                                  safe_sha256=request["to_sha256"])
            write_json(directory / "publication.json", publication)
            state.update(active_sha256=request["to_sha256"], status="waiting", updated_at=time.time())
            state["last_release"] = {**request["adopted_release"], "status": "rolled_back",
                                     "rollback_request_id": identifier}
            state.pop("pending_release", None)
            write_json(state_path, state)
            attempt = directory / "verification" / uuid4().hex
            proofs = {}
            connection = model_connection(False) if live else scripted_model(scripted_reply)
            with connection as url:
                options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if live else "local-fixture",
                           "model": os.environ.get("GLM_MODEL", "glm-5.2") if live else "rollback-verification-script"}
                for alias, assigned in zip(ALIASES, ("payments", "orders")):
                    environment = inside(root, request["environments"][alias])
                    if service["environments"][alias] != request["environments"][alias]:
                        raise ValueError("The active environment changed after approval")
                    source = inside(run, run / "receivers" / alias / ONCALL_SKILL / "SKILL.md")
                    proofs[alias] = verify_task(attempt / alias, source, environment, assigned,
                                               request["to_sha256"], options)
            if len({proof["session_id"] for proof in proofs.values()}) != len(ALIASES):
                raise ValueError("Rollback verification reused a Session")
            behavior = replay_behavior(directory / "behavior-replay" / uuid4().hex, run, request, live=live)
            continued = continued_patrol(root, run, before)
            result = {"schema": SCHEMA, "request_id": identifier, "status": "verified", **expected,
                      "approval": str(directory / "approval.json"), "publication": str(directory / "publication.json"),
                      "verifications": proofs, "behavior_replay": behavior,
                      "patrol_before": before, "patrol_continued": continued,
                      "completed_at": time.time()}
            # The immutable payload is recoverable even when interruption lands
            # after the authority pins its hash but before result.json is exposed.
            completion = inside(directory, directory / "completions" / (uuid4().hex + ".json"))
            completion.parent.mkdir(exist_ok=True)
            write_json(completion, result)
            state["operator_rollback"]["completion"] = {
                "request_id": identifier, "artifact": str(completion.relative_to(directory)),
                "sha256": sha256(completion),
            }
            write_json(state_path, state)
            write_json(directory / "result.json", result)
            commit_completion(state_path, state, directory, result)
            return result
        except Exception as exc:
            error_path = directory / ("completion-error.json" if state["operator_rollback"].get("completion")
                                      else "result.json")
            write_json(error_path, {"schema": SCHEMA, "request_id": identifier,
                       "status": "awaiting_verification", **expected, "error": str(exc), "updated_at": time.time()})
            raise


def rollback_state(workbench_root):
    root = Path(workbench_root).resolve()
    requests = []
    for path in sorted((root / "rollbacks").glob("*/request.json")):
        directory, request = read_request(root, path.parent.name)
        result_path, approval_path = directory / "result.json", directory / "approval.json"
        result = load_json(result_path) if result_path.is_file() else None
        status = result["status"] if result else "approved" if approval_path.is_file() else "awaiting_approval"
        authority_path = (inside(root, Path(request["run"]) / "learning/state.json")
                          if isinstance(request.get("run"), str) else None)
        authority = load_json(authority_path) if authority_path is not None and authority_path.is_file() else {}
        operator = authority.get("operator_rollback", {})
        if operator.get("request_id") == request["id"] and operator.get("status") == "awaiting_verification":
            status = "awaiting_verification"
        requests.append({**request, "status": status,
                         "approval": load_json(approval_path) if approval_path.is_file() else None,
                         "result": result})
    requests.sort(key=lambda request: (request["requested_at"], request["id"]))
    return {"requests": requests}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--action", choices=("prepare", "approve", "execute", "status"), required=True)
    parser.add_argument("--request-id")
    parser.add_argument("--active-sha256")
    parser.add_argument("--previous-sha256")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.action in ("approve", "execute") and not args.request_id:
        parser.error("This action requires --request-id")
    if args.action == "approve" and (not args.active_sha256 or not args.previous_sha256):
        parser.error("Approval requires both displayed Skill hashes")
    if args.action == "prepare":
        result = prepare(args.state)
    elif args.action == "approve":
        result = approve(args.state, args.request_id, args.active_sha256, args.previous_sha256)
    elif args.action == "execute":
        result = execute(args.state, args.request_id, args.live)
    else:
        result = rollback_state(args.state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
