#!/usr/bin/env python3
"""Fork a persisted On-call report and investigate with fresh, scoped HTTP evidence."""

import argparse
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import Lock
import time

from environment import DEFAULT_STATE, SERVICES, get_snapshot, probe_service, write_json
from hermes_runtime import (FOLLOWUP_PREFIX, ONCALL_SKILL, checkpoint_rows, ensure_loopback_no_proxy,
                            fork, open_sessions, preserve_skill_version, read_oncall_skill, run_turn)
from patrol import connect, decode_report, incidents, model_connection, writer_lock


PROBE_COUNT = 3


def followup_receipts(rows, scope, skill_sha):
    calls = {call["id"]: (row["id"], call["function"]["name"])
             for row in rows for call in row.get("tool_calls") or []}
    skill_items = []
    probes = []
    for row in rows:
        if row["role"] != "tool" or row["tool_call_id"] not in calls:
            continue
        call_item, name = calls[row["tool_call_id"]]
        receipt = json.loads(row["content"])
        if name == "skill_view" and receipt.get("success") and receipt.get("sha256") == skill_sha:
            skill_items.append(row["id"])
        elif name == "followup_probe":
            status = receipt.get("http_status")
            if (receipt.get("error") or receipt.get("service") != scope["service"]
                    or receipt.get("environment_id") != scope["environment_id"]
                    or type(status) is not int or not (200 <= status < 300 or 500 <= status < 600)):
                raise RuntimeError("Follow-up HTTP probe did not return valid scoped evidence")
            probes.append({**receipt, "item_id": row["id"], "call_item_id": call_item})
    if (len(skill_items) != 1 or len(probes) != PROBE_COUNT
            or skill_items[0] >= probes[0]["call_item_id"]
            or any(a["item_id"] >= b["call_item_id"] for a, b in zip(probes, probes[1:]))):
        raise RuntimeError("Follow-up must read the pinned Skill and make three sequential HTTP probes")
    return skill_items[0], probes


def investigate(state, environment_state, record, options, *, langfuse=False, sessions=None, branch_report=fork):
    original = json.loads(record["observation"])
    service = original["service"]
    environment_id = original["environment_id"]
    current = get_snapshot(environment_state, service)
    if (original["source"] != "live_lab_http_requests" or current["source"] != original["source"]
            or current["environment_id"] != environment_id or record["recovered_inspection_id"] is not None):
        raise RuntimeError("Follow-up requires the same live environment and an open incident")

    scope = {"incident_id": record["incident_id"], "service": service, "environment_id": environment_id}
    skill_sha = record["skill_sha256"]
    skill_read = False
    requested = 0
    probe_lock = Lock()

    def skill_view():
        nonlocal skill_read
        receipt = read_oncall_skill(skill_sha)
        skill_read = True
        return receipt

    def followup_probe():
        nonlocal requested
        with probe_lock:
            if not skill_read or requested >= PROBE_COUNT:
                return {"error": "Read the pinned Skill and use at most three follow-up probes"}
            requested += 1
            return probe_service(environment_state, service, environment_id=environment_id)

    prompt = FOLLOWUP_PREFIX + json.dumps(scope, ensure_ascii=False) + "\n" + (
        f"用 skill_view 读取 {ONCALL_SKILL}，执行其中的事故补查与值班交接任务。"
    )
    connection = open_sessions(state, langfuse=langfuse) if sessions is None else nullcontext(sessions)
    with connection as sessions:
        with writer_lock(state), closing(sqlite3.connect(state / "patrol.sqlite")) as patrol_db:
            saved = patrol_db.execute("SELECT recovered_inspection_id FROM incidents WHERE incident_id = ?",
                                      (scope["incident_id"],)).fetchone()
            if saved is None or saved[0] is not None:
                raise RuntimeError("The incident was closed before the follow-up started")
            prefix = checkpoint_rows(sessions, record)
            loaded = next((row for row in prefix if row["id"] == record["skill_item_id"] and row["role"] == "tool"), None)
            receipt = json.loads(loaded["content"]) if loaded else {}
            content = receipt.get("content", "").encode("utf-8")
            if (receipt.get("name") != ONCALL_SKILL or receipt.get("sha256") != skill_sha
                    or hashlib.sha256(content).hexdigest() != skill_sha):
                raise RuntimeError("The source report lacks the pinned Skill content")
            # Recover a version from pre-snapshot patrol records as well.
            preserve_skill_version(state, content)
            child, copies = branch_report(sessions, record)
        rows, _ = run_turn(sessions, child, prompt,
                           {"skill_view": skill_view, "followup_probe": followup_probe}, options,
                           task_id="followup-" + record["turn_id"] if langfuse else None)
        if checkpoint_rows(sessions, record) != prefix:
            raise RuntimeError("The source checkpoint changed during follow-up")

    skill_item, probes = followup_receipts(rows, scope, skill_sha)
    report = decode_report(rows[-1]["content"]) if rows and rows[-1]["role"] == "assistant" else None
    statuses = [probe["http_status"] for probe in probes]
    failures = sum(status >= 500 for status in statuses)
    assessment = "persistent" if failures == PROBE_COUNT else "intermittent" if failures else "not_reproduced"
    plan = {"action": "clear_lab_fault" if failures else "observe", "approval_required": bool(failures),
            "verification": "two_clean_windows" if failures else "continue_patrol"}
    expected = {**scope, "probe_http_statuses": statuses, "assessment": assessment,
                "root_cause": "unknown", "recovery_plan": plan}
    if (not isinstance(report, dict) or any(report.get(key) != value for key, value in expected.items())
            or not isinstance(report.get("analysis"), str) or not report["analysis"].strip()):
        raise RuntimeError("The follow-up report does not match the branch evidence")
    return {
        **scope,
        "source_session_id": record["session_id"], "source_report_item_id": record["report_item_id"],
        "session_id": child, "copied_report_item_id": copies[record["report_item_id"]]["id"],
        "skill_sha256": skill_sha, "skill_item_id": skill_item, "report_item_id": rows[-1]["id"],
        "probes": probes, "report": report,
    }


def page_operator(path, recipient, scope, result, evidence):
    page = {
        "incident_id": result["incident_id"], "scope": scope, "recipient": recipient,
        "channel": "local_oncall_inbox", "paged_at": time.time(), "evidence": evidence,
        "session_id": result["session_id"], "report_item_id": result["report_item_id"], "report": result["report"],
    }
    write_json(path, page)
    print(f"Page {recipient}: {scope} — {page['report']['analysis']} ({path})", file=sys.stderr, flush=True)
    return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--environment-state", type=Path)
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Use the local scripted model")
    parser.add_argument("--langfuse", action="store_true", help="Send native traces to the configured Langfuse project")
    parser.add_argument("--implementation", type=Path, help="Workshop implementation for fork and paging")
    parser.add_argument("--page-instance", help="Page independently when this instance finishes investigation")
    args = parser.parse_args()
    branch_report = fork
    notify = page_operator
    if args.implementation:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from implementation import load_implementation

        implementation = load_implementation(args.implementation)
        branch_report = implementation.branch_report
        notify = implementation.notify_operator
    state = args.state.resolve()
    if args.out.exists() or not (state / "patrol.sqlite").is_file():
        parser.error("Use an existing patrol state and a new output file")
    with closing(connect(state)) as db:
        record = next((item for item in incidents(db, args.service) if item["incident_id"] == args.incident_id), None)
    if not record:
        raise RuntimeError("No incident with that ID in the selected service")
    ensure_loopback_no_proxy()
    with model_connection(args.offline) as url:
        options = {
            "base_url": url, "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
            "model": "patrol-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2",
        }
        result = investigate(state, args.environment_state or state, record, options,
                             langfuse=args.langfuse, branch_report=branch_report)
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    if args.page_instance:
        scope = f"{args.page_instance}/{result['environment_id']}/{args.service}"
        notify(args.out.parent / "page.json", "lab-oncall-approver", scope, result,
               f"collection/{args.page_instance}/investigation.json")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, sqlite3.Error, ValueError, KeyError, ImportError) as exc:
        sys.exit(str(exc))
