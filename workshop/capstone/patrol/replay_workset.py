#!/usr/bin/env python3
"""Replay a captured incident's initial HTTP observations in a fresh Hermes session."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock

from environment import SERVICES, alert_event, classify, write_json
from export_workset import LIVE_HTTP, SCHEMA, USAGE, scoped_snapshot
from fleet_eval import read_receipts
from hermes_runtime import PATROL_SKILL, ensure_loopback_no_proxy, install_oncall_skill, open_sessions
from patrol import connect, inspect_service, model_connection


RESULT_SCHEMA = "capstone-http-work-replay-v1"
REPLAY_SOURCE = "captured_http_replay"


def load_case(path, incident_id):
    raw = path.read_bytes()
    workset = json.loads(raw)
    if (not isinstance(workset, dict) or workset.get("schema") != SCHEMA
            or workset.get("usage") != USAGE or not isinstance(workset.get("cases"), list)):
        raise ValueError(f"Expected a {SCHEMA} workset with usage {USAGE}")

    cases = {}
    for case in workset["cases"]:
        key = case.get("incident_id") if isinstance(case, dict) else None
        if not isinstance(key, str) or not key.strip() or key in cases:
            raise ValueError("Every workset incident must have a unique ID")
        cases[key] = case
    if incident_id not in cases:
        raise ValueError("The workset does not contain the selected incident")

    case = cases[incident_id]
    captured = case.get("snapshot")
    invalid_snapshot = f"The selected incident lacks a valid abnormal {LIVE_HTTP} snapshot"
    if (not isinstance(captured, dict) or captured.get("service") not in SERVICES
            or not isinstance(captured.get("environment_id"), str) or not captured["environment_id"].strip()):
        raise ValueError(invalid_snapshot)
    try:
        snapshot = scoped_snapshot(captured, captured["service"], captured["environment_id"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(invalid_snapshot) from exc
    if classify(snapshot) != "abnormal":
        raise ValueError(invalid_snapshot)
    snapshot["source"] = REPLAY_SOURCE

    statuses = case.get("probe_statuses")
    if (not isinstance(statuses, list) or not statuses
            or any(type(status) is not int or status // 100 not in (2, 5) for status in statuses)):
        raise ValueError("The selected incident lacks captured initial 2xx/5xx probe statuses")
    skill_sha = case.get("skill_sha256")
    if not isinstance(skill_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", skill_sha):
        raise ValueError("The selected incident lacks its original Skill SHA-256")
    return snapshot, statuses, skill_sha, sha256(raw).hexdigest()


def run_replay(state, skill, options, snapshot, statuses):
    served = []
    extra_requests = 0
    probe_lock = Lock()

    def probe(_state, service):
        nonlocal extra_requests
        with probe_lock:
            if len(served) == len(statuses):
                extra_requests += 1
                return {"error": "No further initial probe was captured", "status": "insufficient_capture"}
            status = statuses[len(served)]
            served.append(status)
        return {"source": REPLAY_SOURCE, "service": service, "environment_id": snapshot["environment_id"],
                "path": "/work", "http_status": status}

    skill_sha = install_oncall_skill(state, skill)
    patrol_sha = install_oncall_skill(state, name=PATROL_SKILL)
    result = {"skill_sha256": skill_sha, "status": "incomplete_replay", "trace_verified": False}
    with closing(connect(state)) as db, open_sessions(state) as sessions:
        try:
            record = inspect_service(sessions, db, snapshot["service"], state, options, skill_sha,
                                     patrol_skill_sha=patrol_sha,
                                     snapshot=lambda _state, _service: snapshot, probe_request=probe,
                                     incoming_alert=alert_event(snapshot, emitted_at=snapshot["window_end"]))
            result.update(session_id=record["session_id"], report_item_id=record["report_item_id"],
                          evidence_item_id=record["evidence_item_id"], replay_incident_id=record.get("incident_id"))
            verified_skill, probes, report = read_receipts(sessions, record, skill_sha)
            observations = [item for item in probes if "http_status" in item["receipt"]]
            observed = [item["receipt"]["http_status"] for item in observations]
            result["probe_item_ids"] = [item["item_id"] for item in observations]
            if (verified_skill and observed and observed == served
                    and all(record["skill_item_id"] < item["call_item_id"] < item["item_id"] < record["report_item_id"]
                            and item["receipt"].get("source") == REPLAY_SOURCE
                            and item["receipt"].get("service") == snapshot["service"]
                            and item["receipt"].get("environment_id") == snapshot["environment_id"]
                            for item in observations)
                    and all(a["item_id"] < b["call_item_id"] for a, b in zip(observations, observations[1:]))
                    and len(probes) == len(observations) + extra_requests
                    and report.get("probe_http_statuses") == observed
                    and json.loads(record["observation"]) == snapshot):
                result.update(status="observed", trace_verified=True, skill_item_id=record["skill_item_id"],
                              reported_assessment=report.get("assessment"))
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            result["error_type"] = type(exc).__name__
            if "session_id" not in result:
                entries = sessions.list_sessions_rich(limit=2)
                if len(entries) == 1:
                    result["session_id"] = entries[0]["id"]

    if extra_requests:
        result["status"] = "insufficient_capture"
    result.update(probe_statuses=served, captured_probe_count=len(statuses),
                  unused_probe_count=len(statuses) - len(served), extra_probe_requests=extra_requests)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workset", type=Path, required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New output directory for the replay and its report")
    parser.add_argument("--live", action="store_true", help="Use a configured GLM endpoint instead of the local script model")
    args = parser.parse_args()
    try:
        snapshot, statuses, source_skill_sha, workset_sha = load_case(args.workset, args.incident_id)
        if not args.skill.is_file():
            raise ValueError("--skill must point to a readable Skill file")
        if args.out.exists() or args.out.is_symlink():
            raise ValueError("--out must point to a new directory")
        ensure_loopback_no_proxy()
        with model_connection(not args.live) as url:
            options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if args.live else "local-fixture",
                       "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if args.live else "patrol-fixture"}
            args.out.mkdir(parents=True, exist_ok=False)
            result = run_replay(args.out, args.skill, options, snapshot, statuses)
    except (OSError, RuntimeError, ValueError, ImportError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")

    result.update(schema=RESULT_SCHEMA, workset_schema=SCHEMA, usage=USAGE,
                  incident_id=args.incident_id, workset_sha256=workset_sha,
                  source_skill_sha256=source_skill_sha, snapshot=snapshot,
                  mode="live_model" if args.live else "local_script_model")
    report_path = args.out / "report.json"
    write_json(report_path, result)
    print(f"{report_path} ({result['status']})")
    return int(result["status"] == "incomplete_replay")


if __name__ == "__main__":
    raise SystemExit(main())
