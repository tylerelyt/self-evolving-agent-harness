"""Verify the shared-root lab capture against native HTTP and incident records."""

from contextlib import closing
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3

from enterprise_pack import read_sealed
from environment import TELEMETRY_SOURCE, classify, load_json
from export_workset import require, run_file, scoped_snapshot


CLUSTER = "fanout"
ROOT = "notifications"
AFFECTED = ("payments", "orders")
CONTROL = "inventory"
SERVICES = {ROOT, *AFFECTED, CONTROL}
APPROVER = "lab-oncall-approver"
FAILURE_FILE = "failure-evidence.json"
LIFECYCLE_FILE = "incident-lifecycle.json"
CAPTURE_SCHEMA = "capstone-shared-root-capture-v1"
FAILURE_SCHEMA = "capstone-shared-root-failure-v1"


def clock(value):
    require(type(value) in (int, float) and math.isfinite(value), "Invalid HTTP evidence timestamp")
    return value


def telemetry_bounds(window, service, environment, upstream):
    require((window["source"], window["service"], window["environment_id"], window["upstream_service"])
            == (TELEMETRY_SOURCE, service, environment, upstream), "Telemetry route or environment changed")
    begin, end = clock(window["window_start"]), clock(window["window_end"])
    require(begin < end, "Invalid telemetry observation window")
    return begin, end


def audit_phase(phase, environment, seen_traces, *, failed=False):
    require(isinstance(phase, dict) and phase.keys() == SERVICES, "HTTP phase lacks the full shared-root cohort")
    earliest, latest = [], []
    for service, item in phase.items():
        begin = clock(item["probe_started_at"])
        probe = item["probe"]
        end = clock(probe["checked_at"])
        snapshot = scoped_snapshot(item["snapshot"], service, environment)
        unhealthy = failed and service != CONTROL
        status = 503 if unhealthy else 200
        require((probe["service"], probe["environment_id"], probe["path"], probe["http_status"])
                == (service, environment, "/work", status) and not probe.get("error")
                and snapshot["window_start"] <= begin < end <= snapshot["window_end"],
                "HTTP probe failed its scope, status or snapshot timing check")
        quality = "abnormal" if unhealthy else "healthy"
        require(classify(snapshot) == quality and (snapshot["error_rate"] > 0 if unhealthy
                                                  else snapshot["error_rate"] == 0),
                "The shared-root failure or independent healthy control is missing")
        earliest.append(begin)
        latest.append(snapshot["window_end"])
        if service not in AFFECTED:
            require("trace_id" not in probe and {"span", "span_window", "log", "log_window"}.isdisjoint(item),
                    "A direct route unexpectedly claims a downstream trace")
            continue

        trace = probe["trace_id"]
        require(isinstance(trace, str) and re.fullmatch(r"[0-9a-f]{32}", trace) and trace not in seen_traces,
                "Frontend HTTP probes reused or omitted their native correlation IDs")
        seen_traces.add(trace)
        span = item["span"]
        span_start, span_end = clock(span["started_at"]), clock(span["time"])
        telemetry_start, telemetry_end = telemetry_bounds(item["span_window"], service, environment, ROOT)
        require((span["id"], span["trace_id"], span["service"], span["upstream_service"],
                 span["status"], span["upstream_status"]) == (f"span-{trace}", trace, service, ROOT, status, status)
                and telemetry_start <= span_start and begin <= span_start <= span_end <= end <= telemetry_end,
                "Frontend HTTP probe does not join to the same upstream route and span")
        latest.append(telemetry_end)
        if failed:
            log = item["log"]
            logged = clock(log["time"])
            log_start, log_end = telemetry_bounds(item["log_window"], ROOT, environment, None)
            require((log["id"], log["trace_id"], log["service"], log["caller_service"], log["status"],
                     log["fault_scope"], log["fault_mode"]) == (f"log-{trace}", trace, ROOT, service, 503, ROOT, "on")
                    and span_start <= logged <= span_end and log_start <= logged <= end <= log_end,
                    "Frontend failure does not join to an in-window shared-root fault log")
            latest.append(log_end)
        else:
            require({"log", "log_window"}.isdisjoint(item), "A healthy frontend claims a root failure log")
    if failed:
        control = phase[CONTROL]["snapshot"]
        failed_at = [phase[service]["probe"]["checked_at"] for service in (ROOT, *AFFECTED)]
        failed_at.extend(phase[service]["log"]["time"] for service in AFFECTED)
        require(all(control["window_start"] <= event <= control["window_end"] for event in failed_at),
                "The healthy control does not cover the concurrent failures")
    return min(earliest), max(latest)


def observation(scope, snapshot, quality, source, evidence):
    total = snapshot["requests"]
    return {"scope": scope, "total": total, "errors": round(total * snapshot["error_rate"]),
            "quality": quality, "source": source, "evidence": evidence}


def lifecycle_rows(path):
    for suffix in ("-wal", "-journal"):
        journal = path.with_name(path.name + suffix)
        require(not journal.exists() or journal.stat().st_size == 0, "IncidentRuntime has an uncheckpointed journal")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        incidents = [dict(row) for row in db.execute("SELECT * FROM incidents")]
        events = [{**row, "body": json.loads(row["body"])} for row in db.execute("SELECT * FROM events ORDER BY rowid")]
        operations = [{**row, "receipt": json.loads(row["receipt"])} for row in db.execute("SELECT * FROM operations")]
    return {"incidents": incidents, "events": events}, operations


def audit_lifecycle(root, capture):
    path = run_file(root, LIFECYCLE_FILE)
    stored, operations = lifecycle_rows(run_file(root, "incidents.sqlite3"))
    require(sha256(path.read_bytes()).hexdigest() == capture["lifecycle_sha256"] and load_json(path) == stored,
            "Sealed lifecycle export differs from the native IncidentRuntime database")
    incident, environment = capture["incident_id"], capture["environment_id"]
    scope = f"{CLUSTER}/{environment}/{ROOT}"
    operation, action = f"{incident}-clear-fault", "clear_lab_fault"
    request = {"operation": operation, "scope": scope, "action": action}
    require(len(operations) == 1, "Expected one scoped native action")
    receipt = operations[0]["receipt"]
    effect = receipt["effect"]
    cleared_at = clock(effect["cleared_at"])
    require(effect == {"adapter": "loopback_http_fault_file", "fault": f"{ROOT}.fault",
                       "already_clear": False, "cleared_at": cleared_at}
            and receipt == {**request, "adapter": "local_fixture", "status": "applied", "effect": effect}
            and operations == [{"id": operation, "incident": incident, "scope": scope, "action": action,
                                "approved_by": APPROVER, "receipt": receipt}],
            "Native effect did not clear exactly one approved root fault")
    require(stored["incidents"] == [{"id": incident, "scope": scope, "state": "CLOSED", "skill": None,
                                     "evidence": FAILURE_FILE, "healthy_windows": 2}],
            "Expected one closed shared-root incident and two verified recovery cohorts")

    failure = capture["failure"]
    expected = [
        ("trigger", "observation", observation(scope, failure[ROOT]["snapshot"], "unhealthy", "patrol", failure)),
        ("start", "investigation_started", {"skill_sha256": None}),
        ("request", "action_requested", {**request, "evidence": FAILURE_FILE}),
        ("approved", "action_approved", {"operation": operation, "scope": scope, "actor": APPROVER}),
        ("execute", "action_executed", receipt),
    ]
    expected.extend((f"recovery-{index}", "observation", observation(scope, cohort[ROOT], "healthy",
                                                                   "verification", cohort))
                    for index, cohort in enumerate(capture["recovery"], start=1))
    expected.append(("closed", "closed", {"actor": APPROVER}))
    require(stored["events"] == [{"key": f"{incident}-{suffix}", "incident": incident, "kind": kind, "body": body}
                                 for suffix, kind, body in expected],
            "Native incident chronology, approval or attached HTTP observations differ")
    return cleared_at


def audit_capture(root: Path) -> dict:
    """Return the sealed capture after verifying the original evidence without writes."""
    root = root.resolve(strict=True)
    capture, checksum = read_sealed(run_file(root, "capture.json"))
    require((capture["schema"], capture["status"], capture["cluster"], capture["root_service"],
             capture["affected_services"], capture["control_service"])
            == (CAPTURE_SCHEMA, "completed", CLUSTER, ROOT, list(AFFECTED), CONTROL),
            "Unknown shared-root capture scope")
    environment, incident = capture["environment_id"], capture["incident_id"]
    require(isinstance(environment, str) and re.fullmatch(r"[0-9a-f]{32}", environment)
            and isinstance(incident, str) and re.fullmatch(r"lab-shared-root-[0-9a-f]{32}", incident),
            "Shared-root capture lacks a valid environment or incident ID")
    injected_at = clock(capture["injected_at"])
    traces = set()
    _, baseline_end = audit_phase(capture["baseline"], environment, traces)
    failure_start, failure_end = audit_phase(capture["failure"], environment, traces, failed=True)
    restored_start, _ = audit_phase(capture["restored"], environment, traces)
    failure_path = run_file(root, FAILURE_FILE)
    failure, _ = read_sealed(failure_path)
    require(sha256(failure_path.read_bytes()).hexdigest() == capture["failure_sha256"]
            and failure == {"schema": FAILURE_SCHEMA, "incident_id": incident, "environment_id": environment,
                            "injected_at": injected_at, "observations": capture["failure"]},
            "The requested action lacks its original sealed failure evidence")

    recovery = capture["recovery"]
    require(isinstance(recovery, list) and len(recovery) == 2, "Expected exactly two recovery cohorts")
    cleared_at = audit_lifecycle(root, capture)
    require(baseline_end <= injected_at <= failure_start <= failure_end <= cleared_at,
            "Failure probes are outside the injected fault interval")
    boundary = cleared_at
    for cohort in recovery:
        require(isinstance(cohort, dict) and cohort.keys() == SERVICES, "Recovery cohort lacks a scoped service")
        windows = [scoped_snapshot(snapshot, service, environment) for service, snapshot in cohort.items()]
        require(all(classify(window) == "healthy" and window["error_rate"] == 0
                    and boundary < window["window_start"] for window in windows),
                "Recovery windows overlap the action or the prior cohort, or retain an error")
        boundary = max(window["window_end"] for window in windows)
    require(boundary <= restored_start, "Restored route probes precede the second recovery cohort")
    return {**capture, "sha256": checksum}
