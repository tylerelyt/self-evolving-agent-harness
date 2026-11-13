#!/usr/bin/env python3
"""Capture two actual routes through one fault, one approval, and shared recovery."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
import time
from uuid import uuid4

from enterprise_pack import check_metrics, sealed, telemetry
from environment import SHARED_ROOT_ROUTES, classify, get_snapshot, load_json, probe_service, write_json
from export_workset import require
from fleet import APPROVER, clear_lab_fault, process, sha256

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from incident_runtime import IncidentRuntime

SCHEMA = "capstone-shared-root-capture-v1"
FAILURE_SCHEMA = "capstone-shared-root-failure-v1"
CLUSTER = "fanout"
ROOT = "notifications"
CONTROL = "inventory"
AFFECTED = tuple(SHARED_ROOT_ROUTES)
SERVICES = (*AFFECTED, ROOT, CONTROL)
TELEMETRY_WINDOW = ("source", "service", "environment_id", "window_start", "window_end", "upstream_service")


def expected_status(service, status):
    return 200 if service == CONTROL else status


def matches_status(snapshot, status):
    if status == 200:
        return classify(snapshot) == "healthy" and snapshot["error_rate"] == 0
    return classify(snapshot) == "abnormal" and snapshot["error_rate"] > 0


def wait_observations(state, status, *, environment_id=None, after=None):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            snapshots = {service: get_snapshot(state, service) for service in SERVICES}
        except (OSError, KeyError, ValueError):
            time.sleep(.25)
            continue
        scoped_id = environment_id or snapshots[ROOT]["environment_id"]
        for service, snapshot in snapshots.items():
            check_metrics(snapshot, service, scoped_id)
        if all(matches_status(snapshot, expected_status(service, status))
               and (after is None or snapshot["window_start"] > after)
               for service, snapshot in snapshots.items()):
            return snapshots
        time.sleep(.25)
    raise RuntimeError(f"Timed out waiting for all four services to show the expected HTTP {status} phase")


def matched_event(events, trace_id, kind):
    matches = [event for event in events if event["trace_id"] == trace_id]
    require(len(matches) == 1 and matches[0]["id"] == f"{kind}-{trace_id}",
            f"Expected one matching {kind} for {trace_id}")
    return matches[0]


def capture_phase(state, environment_id, status, *, after):
    observations = {}
    for service in SERVICES:
        started = time.time()
        probe = probe_service(state, service, environment_id=environment_id)
        snapshot = get_snapshot(state, service)
        check_metrics(snapshot, service, environment_id)
        expected = expected_status(service, status)
        require(probe["http_status"] == expected and matches_status(snapshot, expected)
                and after <= started <= probe["checked_at"] <= snapshot["window_end"]
                and snapshot["window_start"] <= probe["checked_at"],
                f"{service} HTTP probe or metrics disagreed with the expected {expected} phase")
        observations[service] = {"probe_started_at": started, "probe": probe, "snapshot": snapshot}

    root = telemetry(state, ROOT, environment_id)
    require(root["upstream_service"] is None and all("trace_id" not in observations[service]["probe"]
                                                      for service in (ROOT, CONTROL)),
            "The root and healthy control must be direct services")
    for service in AFFECTED:
        row = observations[service]
        trace_id = row["probe"]["trace_id"]
        data = telemetry(state, service, environment_id)
        span = matched_event(data["traces"], trace_id, "span")
        require(data["upstream_service"] == ROOT and span["upstream_service"] == ROOT
                and span["status"] == span["upstream_status"] == status
                and row["probe_started_at"] <= span["started_at"] <= span["time"] <= row["probe"]["checked_at"],
                f"{service} trace does not prove an actual HTTP call to {ROOT}")
        row.update(span=span, span_window={key: data[key] for key in TELEMETRY_WINDOW})
        if status == 503:
            log = matched_event(root["logs"], trace_id, "log")
            control = observations[CONTROL]["snapshot"]
            require(log["caller_service"] == service and log["status"] == 503
                    and log["fault_scope"] == ROOT and log["fault_mode"] == "on"
                    and span["started_at"] <= log["time"] <= span["time"]
                    and control["window_start"] <= log["time"] <= control["window_end"],
                    f"{service} span does not match a {ROOT} failure during the healthy control window")
            row.update(log=log, log_window={key: root[key] for key in TELEMETRY_WINDOW})
    return observations


def capture(root):
    """Run the isolated HTTP profile in a new directory and return its sealed capture."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    state = root / "environment"
    state.mkdir()
    runtime = IncidentRuntime(root / "incidents.sqlite3")
    incident = f"lab-shared-root-{uuid4().hex}"
    report = {"schema": SCHEMA, "status": "running", "cluster": CLUSTER, "root_service": ROOT,
              "affected_services": list(AFFECTED), "control_service": CONTROL, "incident_id": incident,
              "recovery": []}
    capture_path = root / "capture.json"
    lifecycle_path = root / "incident-lifecycle.json"
    failure_path = root / "failure-evidence.json"
    try:
        with ExitStack() as stack:
            stack.enter_context(process("environment.py", state, root / "environment.log", "serve", "--shared-root"))
            initial = wait_observations(state, 200)
            environment_id = initial[ROOT]["environment_id"]
            report["environment_id"] = environment_id
            scope = f"{CLUSTER}/{environment_id}/{ROOT}"
            report["baseline"] = capture_phase(state, environment_id, 200,
                                               after=max(row["window_end"] for row in initial.values()))

            flag = state / f"{ROOT}.fault"
            report["injected_at"] = time.time()
            flag.touch(exist_ok=False)
            stack.callback(flag.unlink, missing_ok=True)
            wait_observations(state, 503, environment_id=environment_id)
            failure = capture_phase(state, environment_id, 503, after=report["injected_at"])
            report["failure"] = failure
            sealed(failure_path, {"schema": FAILURE_SCHEMA, "incident_id": incident,
                                  "environment_id": environment_id, "injected_at": report["injected_at"],
                                  "observations": failure})
            report["failure_sha256"] = sha256(failure_path)

            metrics = failure[ROOT]["snapshot"]
            runtime.observe(incident + "-trigger", incident, scope, metrics["requests"],
                            round(metrics["requests"] * metrics["error_rate"]), quality="unhealthy", evidence=failure)
            runtime.start(incident, None)
            operation = incident + "-clear-fault"
            runtime.request_action(incident, operation, failure_path.name, action="clear_lab_fault")
            runtime.approve(incident, operation, scope, APPROVER)
            require(set(state.glob("*.fault")) == {flag}, "Recovery must target the single injected root fault")
            receipt = runtime.execute(incident, operation, scope,
                                      apply=lambda: clear_lab_fault(state, ROOT, environment_id))
            effect = receipt["effect"]
            require(not effect["already_clear"] and not list(state.glob("*.fault")),
                    "Approved recovery did not clear exactly the shared root fault")

            boundary = effect["cleared_at"]
            for index in (1, 2):
                cohort = wait_observations(state, 200, environment_id=environment_id, after=boundary)
                runtime.observe(f"{incident}-recovery-{index}", incident, scope, cohort[ROOT]["requests"], 0,
                                source="verification", quality="healthy", evidence=cohort)
                report["recovery"].append(cohort)
                boundary = max(row["window_end"] for row in cohort.values())
            report["restored"] = capture_phase(state, environment_id, 200, after=boundary)
            trace_ids = {report[phase][service]["probe"]["trace_id"]
                         for phase in ("baseline", "failure", "restored") for service in AFFECTED}
            require(len(trace_ids) == 3 * len(AFFECTED), "Every routed probe must have an independent trace ID")
            runtime.close(incident, APPROVER)
        report["status"] = "completed"
    except BaseException as exc:
        report.update(status="failed", error=str(exc) or type(exc).__name__)
        raise
    finally:
        write_json(lifecycle_path, runtime.report())
        report["lifecycle_sha256"] = sha256(lifecycle_path)
        sealed(capture_path, report)
    return load_json(capture_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Fresh directory for the native shared-root capture")
    args = parser.parse_args()
    result = capture(args.out)
    print(f"{args.out.resolve() / 'capture.json'} ({result['status']})", flush=True)


if __name__ == "__main__":
    main()
