#!/usr/bin/env python3
"""Replay same-cluster isolation and coverage gaps from a completed fleet run."""

import argparse
from contextlib import nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

import enterprise_case_gate as gate
from enterprise_pack import check_scope, digest, file_digest, iso, read_sealed, row
from enterprise_replay import instruction, lesson_module, read_json, read_session, replay, task_scope
from environment import classify, write_json
from export_workset import require, run_file, scoped_snapshot
from hermes_runtime import ensure_loopback_no_proxy


HERE = Path(__file__).resolve().parent
SCHEMA = "capstone-enterprise-boundaries-v1"
CAPTURE_SCHEMA = "capstone-same-cluster-distractors-v1"
SCENARIOS = ("concurrent_incident", "insufficient_coverage")
SKILL = HERE / "skills/oncall-investigation/SKILL.md"
PACK_PATH = "enterprise/case-boundaries/pack.json"
EVALUATION_PATH = "enterprise/case-boundaries/evaluation/report.json"
PROMPT = (
    "仅调查指定工单。核对目标告警、影响服务、依赖和根因证据。查看同集群告警，发现其他工单时，"
    "查证它的失败是否独立。只把目标工单的证据用于判断；材料缺失时说明需要补查什么。"
)


def check_decoy_observation(observation, service, environment_id):
    probe, log, window = (observation[key] for key in ("probe", "log", "telemetry_window"))
    metrics = scoped_snapshot(observation["snapshot"], service, environment_id)
    require(classify(metrics) == "abnormal" and metrics["error_rate"] > 0
            and (probe["service"], probe["environment_id"], probe["path"], probe["http_status"])
            == (service, environment_id, "/work", 503)
            and "trace_id" not in probe
            and (log["service"], log["status"], log["caller_service"], log["fault_mode"])
            == (service, 503, None, "on")
            and re.fullmatch(r"[0-9a-f]{32}", log["trace_id"])
            and log["id"] == f"log-{log['trace_id']}"
            and window["window_start"] <= log["time"] <= window["window_end"]
            and max(metrics["window_start"], observation["probe_started_at"]) <= log["time"]
            <= probe["checked_at"] <= metrics["window_end"] <= window["window_end"],
            "Independent fault has no matching scoped HTTP 503 and direct log")
    return metrics, log


def same_cluster_sources(root, index):
    directory = f"enterprise/round-{index}"
    plan_path = run_file(root, f"{directory}/fault-plan.json")
    capture_path = run_file(root, f"{directory}/capture.json")
    recovery_path = run_file(root, f"round-{index}/recovery.json")
    sidecar_path = run_file(root, f"{directory}/same-cluster-distractors.json")
    sidecar, sidecar_sha = read_sealed(sidecar_path)
    plan, recoveries = read_json(plan_path), read_json(recovery_path)
    capture, _ = read_sealed(capture_path)
    lifecycle = read_json(run_file(root, f"round-{index}/incident-lifecycle.json"))
    events = {event["key"]: event for event in lifecycle["events"]}
    require(sidecar["schema"] == CAPTURE_SCHEMA and sidecar["round"] == plan["round"] == index
            and sidecar["fault_plan_sha256"] == file_digest(plan_path)
            and sidecar["capture_sha256"] == file_digest(capture_path)
            and sidecar["recovery_sha256"] == file_digest(recovery_path)
            and sidecar["incidents"].keys() == plan["targets"].keys() == capture["cases"].keys() == recoveries.keys(),
            "Independent-fault capture does not match the fleet round")
    sources = {}
    for alias, target in plan["targets"].items():
        evidence = sidecar["incidents"][alias]
        incident = capture["cases"][alias]["incident_id"]
        service, environment_id = target["decoy_service"], target["environment_id"]
        before, after = evidence["before"], evidence["after"]
        metrics, log = check_decoy_observation(before, service, environment_id)
        later_metrics, later_log = check_decoy_observation(after, service, environment_id)
        recovery = recoveries[alias]
        cleared_at = events[incident + "-execute"]["body"]["effect"]["cleared_at"]
        check_scope(recovery, target["service"], environment_id, "live_lab_http_requests")
        require((evidence["target_incident_id"], evidence["service"], evidence["environment_id"], evidence["injected_at"])
                == (incident, service, environment_id, target["decoy_injected_at"])
                and service not in (target["service"], target["root_service"])
                and max(target["injected_at"], target["decoy_injected_at"]) <= before["probe_started_at"]
                and max(before["probe"]["checked_at"], metrics["window_end"],
                        before["telemetry_window"]["window_end"]) <= cleared_at
                < recovery["window_start"] < recovery["window_end"] <= after["probe_started_at"]
                and cleared_at < later_metrics["window_start"] and recovery["window_end"] <= later_metrics["window_end"]
                and recovery["error_rate"] == 0 and classify(recovery) == "healthy"
                and log["trace_id"] != later_log["trace_id"],
                "Independent fault did not persist across the target's scoped recovery")
        foreign_incident = f"lab-independent-{index}-{alias}-{service}"
        sources[f"{incident}@{alias}"] = [
            {**row("concurrent-alert", "alerts", alias, service, metrics["window_end"], metrics),
             "incident_id": foreign_incident},
            {**row("concurrent-log", "logs", alias, service, log["time"], log),
             "incident_id": foreign_incident},
        ]
    return sources, sidecar_sha


def query_requirement(kind, cluster, service, status="covered"):
    return {"kind": kind, "cluster": cluster, "service": service, "status": status}


def derive(original, foreign_rows, scenario):
    base = original["snapshot"]
    incident = base["incident_id"]
    scope = task_scope(original["input"]["prompt"])
    cluster, target = scope["cluster"], scope["service"]
    original_answer = original["annotation"]["expected_output"]
    root_service = original_answer["root_cause"].split("/", 1)[1]
    complete = scenario == "concurrent_incident"
    kinds = base["manifest"]["clusters"][cluster]
    if not complete:
        kinds = [kind for kind in kinds if kind not in ("logs", "traces")]
    target_rows = [{**item, "incident_id": incident} for item in base["rows"]
                   if item["cluster"] == cluster and item["kind"] in kinds]
    if not complete:
        # Preserve the topology while removing the correlation identifier.
        for item in target_rows:
            if item["kind"] == "dependencies":
                item["value"] = {"upstream": item["value"]["upstream"]}
    foreign_rows = foreign_rows if complete else []
    rows = target_rows + foreign_rows
    start = min(base["coverage"]["start"], *(item["time"] for item in rows))
    end = max(base["coverage"]["end"], *(item["time"] for item in rows))
    snapshot = {**base, "snapshot_id": f"{incident}--{scenario}", "version": SCHEMA,
                "coverage": {"start": start, "end": end, "complete": True}, "rows": rows,
                "manifest": {"clusters": {cluster: kinds}}}
    scope = {**scope, "start": start, "end": end}
    queries = [query_requirement(item["kind"], item["cluster"], item["service"]) for item in rows]
    needed = []
    if not complete:
        unavailable = [query_requirement(kind, cluster, service, "not_covered")
                       for kind, service in (("traces", target), ("logs", root_service))]
        queries.extend(unavailable)
        needed = [{key: value for key, value in item.items() if key != "status"} for item in unavailable]
    case = {
        "id": f"{original['id']}--{scenario}", "source_case_id": original["id"],
        "split": original["split"], "scenario": scenario, "snapshot": snapshot,
        "input": {"prompt": "CAPSTONE_SCOPE " + json.dumps(scope, sort_keys=True) + "\n"
                            + f"CAPSTONE_INCIDENT {incident}\n" + PROMPT},
        "annotation": {
            "expected_output": {"root_cause": original_answer["root_cause"] if complete else None,
                                "affected_services": [original_answer["affected_service"]],
                                "excluded_services": [f"{cluster}/{foreign_rows[0]['service']}"] if complete else [],
                                "needed_evidence": needed},
            "required_evidence": [item["id"] for item in target_rows],
            "forbidden_evidence": [item["id"] for item in foreign_rows],
            "required_queries": queries,
        },
    }
    gate.validate_case(case)
    return case


def build(root, original):
    """Derive two views of each original incident without moving its split."""
    lesson_module("dataset").verify(original)
    sources, captures = {}, {}
    for index, split in enumerate(("dev", "holdout"), start=1):
        captured, captures[split] = same_cluster_sources(root, index)
        require(not (sources.keys() & captured.keys()), "Incident appears in both splits")
        sources.update({key: (split, rows) for key, rows in captured.items()})
    all_original = [case["id"] for case in original["ready"]] + [case["case_id"] for case in original["pending"]]
    require(len(all_original) == len(sources) and set(all_original) == sources.keys(),
            "Boundary cases must use the original fleet incident groups")
    ready = []
    for case in original["ready"]:
        split, rows = sources[case["id"]]
        require(case["split"] == split, "Boundary case crosses the source split")
        ready.extend(derive(case, rows, scenario) for scenario in SCENARIOS)
    pending = [{"source_case_id": item["case_id"], "split": sources[item["case_id"]][0],
                "scenarios": list(SCENARIOS), "reasons": item["reasons"]} for item in original["pending"]]
    pack = {"schema": SCHEMA, "source_pack_sha256": original["pack_sha256"], "capture_sha256": captures,
            "scorer_version": gate.SCORER_VERSION, "scorer_sha256": file_digest(gate.__file__),
            "skill_sha256": file_digest(SKILL), "ready": ready, "pending": pending}
    return {**pack, "pack_sha256": digest(pack)}


def verify(pack):
    require(pack["schema"] == SCHEMA and pack["scorer_version"] == gate.SCORER_VERSION
            and pack["scorer_sha256"] == file_digest(gate.__file__) and pack["skill_sha256"] == file_digest(SKILL)
            and pack["pack_sha256"] == digest({key: value for key, value in pack.items() if key != "pack_sha256"}),
            "Boundary pack, pinned scorer or Skill changed")
    groups, seen = {}, set()
    for case in pack["ready"]:
        identity, scenario = case["id"], case["scenario"]
        source, split = case["source_case_id"], case["split"]
        require(re.fullmatch(r"[a-zA-Z0-9_-]+@[a-zA-Z0-9_-]+", source)
                and split in {"dev", "holdout"} and split == groups.setdefault(source, split)
                and scenario in SCENARIOS and identity == f"{source}--{scenario}" and identity not in seen
                and case["snapshot"]["version"] == SCHEMA,
                "Boundary case ID, snapshot version or split is invalid")
        seen.add(identity)
        gate.validate_case(case)
    require(seen == {f"{source}--{scenario}" for source in groups for scenario in SCENARIOS},
            "A source incident lost one of its boundary cases")


def assessment(pack, rows):
    if any(item["grade"]["status"] != "scored" for item in rows):
        return "unscored"
    if pack["pending"] or {case["split"] for case in pack["ready"]} != {"dev", "holdout"}:
        return "insufficient_cases"
    return "passed" if all(item["grade"]["passed"] for item in rows) else "business_gate_failed"


def evaluate(output, pack, options, live):
    verify(pack)
    output = output.resolve()
    output.mkdir(parents=True)
    skill = SKILL.read_bytes()
    monitor = lesson_module("monitor")
    report = {"status": "running", "mode": "live" if live else "local-script-model",
              "pack_sha256": pack["pack_sha256"], "skill_sha256": pack["skill_sha256"], "cases": []}
    path = output / "report.json"
    write_json(path, report)
    if live:
        connection = nullcontext(options["base_url"])
    else:
        sys.path.insert(0, str(HERE.parent))
        from local_model import serve
        from enterprise_case_fixture import respond

        connection = serve(respond)
    with connection as url:
        runtime = {**options, "base_url": url, "model": options["model"] if live else "enterprise-boundaries-fixture"}
        for case in sorted(pack["ready"], key=lambda item: item["split"]):
            directory = output / case["split"] / case["id"]
            try:
                result = replay(directory, case, skill, runtime, monitor.query, case_protocol=True)
            except (RuntimeError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                result = {"status": "infrastructure_error", "error": str(exc)}
            result["grade"] = gate.grade(case, result)
            record = directory / "evaluation.json"
            write_json(record, result)
            report["cases"].append({"id": case["id"], "file": str(record.relative_to(output)),
                                    "grade": result["grade"], "session_id": result.get("session_id")})
            write_json(path, report)
    report["status"] = assessment(pack, report["cases"])
    write_json(path, report)
    return report


def summary(pack, status):
    return {"pack": PACK_PATH, "evaluation": EVALUATION_PATH, "ready": len(pack["ready"]),
            "pending_source_incidents": len(pack["pending"]), "status": status}


def run(root, options, live):
    pack = build(root, read_json(run_file(root, "enterprise/prepared/pack.json")))
    pack_path = root / PACK_PATH
    pack_path.parent.mkdir()
    write_json(pack_path, pack)
    evaluation = evaluate((root / EVALUATION_PATH).parent, pack, options, live)
    return summary(pack, evaluation["status"])


def audit(root, entry, original, mode):
    pack = read_json(run_file(root, PACK_PATH))
    require(build(root, original) == pack, "Boundary pack differs from the original sealed captures")
    output = run_file(root, EVALUATION_PATH).parent
    report = read_json(run_file(output, "report.json"))
    require(report["mode"] == mode and entry == summary(pack, report["status"])
            and report["pack_sha256"] == pack["pack_sha256"]
            and report["skill_sha256"] == pack["skill_sha256"], "Boundary replay disagrees with the frozen pack")
    cases = {case["id"]: case for case in pack["ready"]}
    require([item["id"] for item in report["cases"]]
            == [case["id"] for case in sorted(pack["ready"], key=lambda item: item["split"])],
            "Boundary replay changed its incident splits or order")
    sessions = set()
    for item in report["cases"]:
        case = cases[item["id"]]
        relative = f"{case['split']}/{case['id']}/evaluation.json"
        require(item["file"] == relative, "Boundary evaluation points outside its assigned case")
        result_path = run_file(output, relative)
        result = read_json(result_path)
        if result["status"] == "completed":
            state = result_path.parent
            skill = run_file(state, "SKILL.md").read_bytes()
            wal = run_file(state, "hermes/state.db").with_name("state.db-wal")
            require((not wal.exists() or wal.stat().st_size == 0)
                    and read_json(run_file(state, "task.json")) == case["input"]
                    and read_json(run_file(state, "snapshot.json")) == case["snapshot"]
                    and result["skill_sha256"] == pack["skill_sha256"] == sha256(skill).hexdigest(),
                    "Boundary replay changed its task, snapshot or Skill")
            persisted = read_session(state, result, case["input"]["prompt"] + "\n\n" + instruction(True),
                                     case["snapshot"], skill, lesson_module("monitor").query)
            require(all(result[key] == value for key, value in persisted.items())
                    and result["session_id"] not in sessions, "Boundary replay did not retain fresh tool Items")
            sessions.add(result["session_id"])
        require(item["session_id"] == result.get("session_id")
                and item["grade"] == result["grade"] == gate.grade(case, result), "Boundary replay grade changed")
    status = assessment(pack, report["cases"])
    require(report["status"] == status, "Boundary replay has an inconsistent assessment")
    if status != "passed":
        return {"status": "pending", "reason": status, "sessions": len(sessions)}
    return {"status": "passed", "sessions": len(sessions), "scorer_version": pack["scorer_version"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True, help="Frozen case-boundaries/pack.json")
    parser.add_argument("--out", type=Path, required=True, help="New replay output directory")
    parser.add_argument("--live", action="store_true", help="Use the configured GLM model endpoint")
    args = parser.parse_args()
    ensure_loopback_no_proxy()
    if args.live:
        from patrol import model_connection

        connection = model_connection(False)
    else:
        connection = nullcontext("")
    with connection as url:
        options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if args.live else "local-fixture",
                   "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if args.live else "enterprise-boundaries-fixture"}
        result = evaluate(args.out, read_json(args.pack), options, args.live)
    print(f"{args.out.resolve() / 'report.json'} ({result['status']})")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
