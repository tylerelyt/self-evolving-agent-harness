#!/usr/bin/env python3
"""Export scoped HTTP working examples from a completed fleet run."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

from environment import SERVICES, classify, load_json
from patrol import decode_report


SCHEMA = "capstone-http-workset-v1"
USAGE = "working_only"
TELEMETRY = ("source", "service", "environment_id", "window_start", "window_end",
             "requests", "error_rate", "p95_ms")
LIVE_HTTP = "live_lab_http_requests"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run_file(run, relative):
    path = (run / relative).resolve(strict=True)
    require(path.is_relative_to(run) and path.is_file(), "Evidence must be a file inside the run")
    return path


def scoped_snapshot(data, service, environment_id):
    snapshot = {name: data[name] for name in TELEMETRY}
    require(snapshot["source"] == LIVE_HTTP and snapshot["service"] == service
            and snapshot["environment_id"] == environment_id, "HTTP snapshot scope differs from the incident")
    require(all(type(snapshot[key]) in (int, float) and math.isfinite(snapshot[key])
                for key in ("window_start", "window_end", "error_rate", "p95_ms"))
            and snapshot["window_start"] < snapshot["window_end"]
            and type(snapshot["requests"]) is int and snapshot["requests"] >= 10
            and 0 <= snapshot["error_rate"] <= 1 and snapshot["p95_ms"] >= 0,
            "HTTP snapshot has invalid measurements")
    return snapshot


def session_rows(db, session_id, start, stop):
    require(type(start) is int and type(stop) is int and 0 < start < stop, "Invalid SessionDB Item bounds")
    return {row["id"]: row for row in db.execute("""
        SELECT id, role, tool_name, tool_call_id, tool_calls, content FROM messages
        WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id
    """, (session_id, start, stop))}


def verify_skill(row, digest):
    result = json.loads(row["content"])
    require(row["role"] == "tool" and row["tool_name"] == "skill_view" and result["success"] is True
            and result["name"] == "oncall-alert-triage"
            and result["sha256"] == digest == sha256(result["content"].encode("utf-8")).hexdigest(),
            "SessionDB Skill receipt differs from the round Skill")


def recorded_probes(rows, expected, name, scope, skill_item, report_item):
    calls = {call["id"]: (row["id"], call["function"]["name"])
             for row in rows.values() if row["role"] == "assistant" and row["tool_calls"]
             for call in json.loads(row["tool_calls"])}
    probes = []
    previous = skill_item
    for row in rows.values():
        if row["role"] != "tool" or row["tool_name"] != name:
            continue
        item = row["id"]
        call_item, tool = calls[row["tool_call_id"]]
        result = json.loads(row["content"])
        status = result["http_status"]
        require(tool == name and previous < call_item < item < report_item
                and all(result[key] == value for key, value in scope.items())
                and result["path"] == "/work" and type(status) is int
                and status // 100 in (2, 5) and not result.get("error"),
                "SessionDB probe is outside scope, out of order or invalid")
        probes.append({**result, "item_id": item, "call_item_id": call_item})
        previous = item
    require(probes and probes == expected, "Export evidence differs from the SessionDB probe receipts")
    return [probe["http_status"] for probe in probes]


def session_probes(path, proof, branch, snapshot, digest):
    record = proof["incident"]
    origin = record["session_id"]
    patrol_item = record["report_item_id"]
    branch_item = branch["report_item_id"]
    copied_item = branch["copied_report_item_id"]
    wal = path.with_name(path.name + "-wal")
    require(not wal.exists() or wal.stat().st_size == 0, "SessionDB has an uncheckpointed WAL")
    # Completed runs are immutable evidence. The URI keeps reads from creating journal or lock files.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        parent = db.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (branch["session_id"],)).fetchone()
        require(parent is not None and parent[0] == origin, "SessionDB investigation is missing its parent")
        initial = session_rows(db, origin, record["evidence_item_id"], patrol_item)
        followup = session_rows(db, branch["session_id"], copied_item, branch_item)
    receipt = initial[record["evidence_item_id"]]
    observed = json.loads(receipt["content"])
    require(receipt["role"] == "tool" and receipt["tool_name"] == "service_snapshot"
            and all(observed[key] == value for key, value in snapshot.items()),
            "HTTP snapshot differs from the SessionDB metrics receipt")
    require(initial[patrol_item]["role"] == followup[copied_item]["role"] == followup[branch_item]["role"] == "assistant"
            and decode_report(initial[patrol_item]["content"]) == proof["report"]
            and initial[patrol_item]["content"] == followup[copied_item]["content"]
            and decode_report(followup[branch_item]["content"]) == branch["report"],
            "SessionDB reports do not match the evidence and copied parent Item")
    verify_skill(initial[record["skill_item_id"]], digest)
    verify_skill(followup[branch["skill_item_id"]], digest)
    scope = {"service": snapshot["service"]}
    patrol = recorded_probes(initial, proof["probes"], "oncall_probe", scope, record["skill_item_id"], patrol_item)
    scope["environment_id"] = snapshot["environment_id"]
    investigation = recorded_probes(followup, branch["probes"], "followup_probe", scope,
                                    branch["skill_item_id"], branch_item)
    require(record["probe_item_id"] == proof["probes"][0]["item_id"]
            and proof["report"]["probe_http_status"] == patrol[0]
            and proof["report"].get("probe_http_statuses", patrol) == patrol,
            "Patrol report or Item pointer differs from the initial HTTP receipts")
    return patrol, investigation


def recovery_evidence(lifecycle, last_window, case, branch_path, *, paged=False):
    incident = case["incident_id"]
    snapshot = case["snapshot"]
    service = snapshot["service"]
    environment_id = snapshot["environment_id"]
    scope = f"{case['instance']}/{environment_id}/{service}"
    states = [state for state in lifecycle["incidents"] if state["id"] == incident]
    events = [event for event in lifecycle["events"] if event["incident"] == incident]
    require(len(states) == 1 and states[0]["state"] == "CLOSED" and states[0]["healthy_windows"] == 2
            and states[0]["scope"] == scope and states[0]["skill"] == case["skill_sha256"]
            and states[0]["evidence"] == branch_path,
            "Incident lifecycle does not close with scoped investigation and recovery")
    kinds = ["observation", "investigation_started", "action_requested", "action_approved",
             "action_executed", "observation", "observation", "closed"]
    if paged:
        kinds.insert(3, "handoff")
    require([event["kind"] for event in events] == kinds,
            "Incident lifecycle lacks ordered observation, approval and recovery events")
    if paged:
        handoff = events.pop(3)["body"]
        require(handoff["state"] == "AWAITING_APPROVAL" and handoff["evidence"] == branch_path
                and handoff["pending"] == [events[2]["body"]["operation"]]
                and handoff["owner"] == events[3]["body"]["actor"], "Invalid on-call handoff")
    trigger, started, requested, approved, executed, first, second, _ = (event["body"] for event in events)
    require(trigger["evidence"] == snapshot and trigger["quality"] == "unhealthy" and trigger["scope"] == scope
            and started["skill_sha256"] == case["skill_sha256"] and requested["evidence"] == branch_path
            and requested["action"] == executed["action"] == "clear_lab_fault"
            and requested["scope"] == approved["scope"] == executed["scope"] == scope
            and requested["operation"] == approved["operation"] == executed["operation"]
            and executed["status"] == "applied", "Recovery approval differs from the scoped incident evidence")
    lower_bound = executed["effect"]["cleared_at"]
    require(type(lower_bound) in (int, float) and math.isfinite(lower_bound)
            and lower_bound >= snapshot["window_end"], "Recovery action has an invalid time")
    windows = []
    for body in (first, second):
        window = scoped_snapshot(body["evidence"], service, environment_id)
        require(body["scope"] == scope and body["source"] == "verification" and body["quality"] == "healthy"
                and body["errors"] == window["error_rate"] == 0 and body["total"] == window["requests"]
                and classify(window) == "healthy" and lower_bound < window["window_start"],
                "Recovery windows must be healthy, in scope, after approval and nonoverlapping")
        lower_bound = window["window_end"]
        windows.append(window)
    require(windows[-1] == last_window, "Final recovery snapshot differs from the lifecycle")
    return windows


def export_case(run, round_data, instance, proof, lifecycle, last_window):
    number = round_data["round"]
    record = proof["incident"]
    incident = record["incident_id"]
    observed = json.loads(record["observation"])
    service, environment_id = observed["service"], observed["environment_id"]
    require(service in SERVICES and re.fullmatch(r"[0-9a-f]{32}", environment_id)
            and re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident)
            and re.fullmatch(rf"patrol-{service}-[0-9a-f]{{32}}", record["session_id"])
            and record["session_id"] == round_data["source_sessions"][instance],
            "Patrol evidence does not identify a scoped lab incident")
    snapshot = scoped_snapshot(observed, service, environment_id)
    digest = record["skill_sha256"]
    require(classify(snapshot) == "abnormal" and re.fullmatch(r"[0-9a-f]{64}", digest)
            and digest == round_data["active_sha256"] and proof["report"]["incident_id"] == incident,
            "Patrol observation, report and round Skill differ")
    collection = Path(round_data["patrol_evidence"]).parent
    branch_relative = collection / instance / "investigation.json"
    db_relative = collection / instance / "hermes" / "state.db"
    branch = load_json(run_file(run, branch_relative))
    scope = {"incident_id": incident, "service": service, "environment_id": environment_id}
    require(branch == proof["investigation"] and all(branch[key] == value for key, value in scope.items())
            and branch["source_session_id"] == record["session_id"]
            and branch["source_report_item_id"] == record["report_item_id"]
            and re.fullmatch(r"patrol-fork-[0-9a-f]{32}", branch["session_id"])
            and branch["skill_sha256"] == digest, "Investigation artifact does not fork the scoped patrol report")
    initial, followup = session_probes(run_file(run, db_relative), proof, branch, snapshot, digest)
    branch_report = branch["report"]
    failures = sum(status >= 500 for status in followup)
    assessment = "persistent" if failures == 3 else "intermittent"
    require(len(followup) == 3 and failures > 0 and branch_report["assessment"] == assessment
            and branch_report["probe_http_statuses"] == followup and branch_report["root_cause"] == "unknown"
            and all(branch_report[key] == value for key, value in scope.items()),
            "Investigation classification differs from its three scoped HTTP receipts")
    case = {
        "incident_id": incident, "instance": instance, "round": number,
        "snapshot": snapshot, "probe_statuses": initial, "skill_sha256": digest,
        "provenance": {
            "patrol_evidence": round_data["patrol_evidence"], "investigation": branch_relative.as_posix(),
            "incident_lifecycle": round_data["incident_lifecycle"], "recovery": round_data["recovery"],
            "session_db": db_relative.as_posix(),
            "patrol_session_id": record["session_id"], "patrol_observation_item_id": record["evidence_item_id"],
            "patrol_skill_item_id": record["skill_item_id"], "patrol_report_item_id": record["report_item_id"],
            "patrol_probe_item_ids": [probe["item_id"] for probe in proof["probes"]],
            "investigation_session_id": branch["session_id"], "investigation_skill_item_id": branch["skill_item_id"],
            "investigation_report_item_id": branch["report_item_id"],
            "investigation_copied_report_item_id": branch["copied_report_item_id"],
            "investigation_probe_item_ids": [probe["item_id"] for probe in branch["probes"]],
        },
        "investigation": {"probe_statuses": followup, "assessment": assessment, "root_cause": "unknown"},
    }
    case["recovery_windows"] = recovery_evidence(lifecycle, last_window, case,
                                                  branch_relative.relative_to(f"round-{number}").as_posix(),
                                                  paged="patrol_loop" in round_data)
    return case


def build_workset(run: Path, report=None) -> dict:
    run = Path(run).resolve(strict=True)
    if report is None:
        report = load_json(run_file(run, "report.json"))
    require(report["status"] == "complete" and report["mode"] in ("local-script-model", "live")
            and report["rounds"], "Export requires a completed fleet report")
    cases = []
    seen = set()
    for number, round_data in enumerate(report["rounds"], start=1):
        round_root = f"round-{number}"
        require(type(round_data["round"]) is int and round_data["round"] == number
                and round_data["patrol_evidence"] == f"{round_root}/collection/patrol-evidence.json"
                and round_data["incident_lifecycle"] == f"{round_root}/incident-lifecycle.json"
                and round_data["recovery"] == f"{round_root}/recovery.json", "Unexpected round evidence paths")
        proofs = load_json(run_file(run, round_data["patrol_evidence"]))
        lifecycle = load_json(run_file(run, round_data["incident_lifecycle"]))
        recovery = load_json(run_file(run, round_data["recovery"]))
        require(proofs and proofs.keys() == round_data["source_sessions"].keys() == recovery.keys()
                and len(lifecycle["incidents"]) == len(proofs), "Round evidence disagrees about its instances")
        for instance, proof in sorted(proofs.items()):
            require(re.fullmatch(r"instance-[a-z0-9]+", instance), "Unexpected fleet instance identifier")
            case = export_case(run, round_data, instance, proof, lifecycle, recovery[instance])
            require(case["incident_id"] not in seen, "Incident appears more than once in the workset")
            seen.add(case["incident_id"])
            cases.append(case)
    return {"schema": SCHEMA, "usage": USAGE, "source_mode": report["mode"], "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    workset = build_workset(args.run)
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(workset, output)
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        sys.exit(f"Cannot export HTTP workset: {exc}")
