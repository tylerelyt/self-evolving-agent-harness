#!/usr/bin/env python3
"""Run two rounds of real HTTP patrols through native SkillClaw."""

import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from uuid import uuid4

from environment import ALERT_SOURCE, classify, get_snapshot, inject_fault, load_json, read_alert, write_json
from hermes_runtime import ONCALL_SKILL, PATROL_SKILL, ensure_loopback_no_proxy
from patrol import checkpoint, connect, decode_report, incidents, link_recovery, model_connection, writer_lock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from incident_runtime import IncidentRuntime

SEED = HERE / "skills" / ONCALL_SKILL / "SKILL.md"
PATROL_SEED = HERE / "skills" / PATROL_SKILL / "SKILL.md"
GROUP = "patrol-learning"
APPROVER = "lab-oncall-approver"
TARGETS = {"instance-a": "payments", "instance-b": "orders"}
FAULTS = (
    {"instance-a": "on", "instance-b": "intermittent"},
    {"instance-a": "intermittent", "instance-b": "on"},
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def process(script, state, log, *args):
    with log.open("w") as output:
        child = subprocess.Popen(
            [sys.executable, "-B", str(HERE / script), "--state", str(state), *args],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            yield child
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def wait_for_metrics(environments, predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observations = {}
        for alias, state in environments.items():
            try:
                snapshot = get_snapshot(state, TARGETS[alias])
            except (OSError, ValueError, KeyError, URLError):
                continue
            if predicate(alias, snapshot):
                observations[alias] = snapshot
        if observations.keys() == environments.keys():
            return observations
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for the two HTTP environments")


def set_faults(environments, modes, *, cleanup):
    injected = {}
    for alias, state in environments.items():
        injected[alias] = time.time()
        inject_fault(state, TARGETS[alias], modes[alias], cleanup=cleanup)
    return injected


def start_enterprise_faults(path, index, environments, healthy, faults, *, cleanup):
    from environment import TRACE_DEPENDENCIES

    path.mkdir(parents=True)
    targets = {}
    for alias, state in environments.items():
        peer = next(other for other in environments if other != alias)
        decoy = TARGETS[peer]
        decoy_time = time.time()
        inject_fault(state, decoy, "on", cleanup=cleanup)
        service = TARGETS[alias]
        targets[alias] = {
            "service": service, "root_service": TRACE_DEPENDENCIES[service],
            "excluded_cluster": peer, "mode": faults[alias],
            "environment_id": healthy[alias]["environment_id"],
            "decoy_service": decoy, "decoy_injected_at": decoy_time,
        }
    for alias, injected_at in set_faults(environments, faults, cleanup=cleanup).items():
        targets[alias]["injected_at"] = injected_at
    plan = {"round": index, "targets": targets}
    write_json(path / "fault-plan.json", plan)
    return plan


def capture_distractors(plan, environments):
    from enterprise_pack import telemetry
    from environment import probe_service

    observations = {}
    for alias, target in plan["targets"].items():
        state = environments[alias]
        service, environment_id = target["decoy_service"], target["environment_id"]
        started = time.time()
        probe = probe_service(state, service, environment_id=environment_id)
        snapshot = scoped_observation(state, service, environment_id)
        data = telemetry(state, service, environment_id)
        # Direct /work responses carry no trace ID. Keep a separately observed direct log.
        log = next((row for row in reversed(data["logs"])
                    if started <= row["time"] <= probe["checked_at"]
                    and row["status"] == 503 and row["caller_service"] is None
                    and row["fault_mode"] == "on"), None)
        if (probe["http_status"] != 503 or "trace_id" in probe or log is None
                or data["upstream_service"] is not None or data["traces"]
                or classify(snapshot) != "abnormal" or snapshot["error_rate"] == 0
                or not snapshot["window_start"] <= log["time"] <= probe["checked_at"] <= snapshot["window_end"]):
            raise RuntimeError(f"{alias}/{service}: missing scoped evidence for the independent fault")
        observations[alias] = {
            "probe_started_at": started, "probe": probe, "snapshot": snapshot, "log": log,
            "telemetry_window": {key: data[key] for key in ("window_start", "window_end")},
        }
    return observations


def seal_distractors(path, iteration, plan, proofs, before, after):
    from enterprise_pack import sealed

    lifecycle = json.loads((iteration / "incident-lifecycle.json").read_text(encoding="utf-8"))
    recovery_path = iteration / "recovery.json"
    recovered = json.loads(recovery_path.read_text(encoding="utf-8"))
    clear_times = {event["incident"]: event["body"]["effect"]["cleared_at"]
                   for event in lifecycle["events"] if event["kind"] == "action_executed"}
    incidents = {}
    for alias, target in plan["targets"].items():
        incident = proofs[alias]["incident"]["incident_id"]
        first, last = before[alias], after[alias]
        if (not target["decoy_injected_at"] <= first["probe_started_at"]
                <= first["probe"]["checked_at"] <= first["snapshot"]["window_end"] < clear_times[incident]
                or not recovered[alias]["window_end"] < last["probe_started_at"]
                or first["log"]["id"] == last["log"]["id"]):
            raise RuntimeError(f"{alias}: independent failure receipts do not bracket target recovery")
        incidents[alias] = {
            "target_incident_id": incident, "service": target["decoy_service"],
            "environment_id": target["environment_id"], "injected_at": target["decoy_injected_at"],
            "before": first, "after": last,
        }
    sealed(path / "same-cluster-distractors.json", {
        "schema": "capstone-same-cluster-distractors-v1", "round": plan["round"], "incidents": incidents,
        "fault_plan_sha256": sha256(path / "fault-plan.json"), "capture_sha256": sha256(path / "capture.json"),
        "recovery_sha256": sha256(recovery_path),
    })


def scoped_observation(state, service, environment_id):
    observation = get_snapshot(state, service)
    if observation["source"] != "live_lab_http_requests" or observation["environment_id"] != environment_id:
        raise RuntimeError("The investigation belongs to a different HTTP environment")
    return observation


def clear_lab_fault(state, service, environment_id):
    observation = scoped_observation(state, service, environment_id)
    flag = state / f"{service}.fault"
    already_clear = not flag.exists()
    if not already_clear and classify(observation) != "abnormal":
        raise RuntimeError("The approved HTTP scope is not currently reporting this fault")
    flag.unlink(missing_ok=True)
    return {"adapter": "loopback_http_fault_file", "fault": flag.name,
            "already_clear": already_clear, "cleared_at": time.time()}


def recover(iteration, environments, proofs, workers):
    """Authorize fixture recovery, then prove it with two disjoint HTTP windows."""
    from investigation import page_operator

    runtime = IncidentRuntime(iteration / "incidents.sqlite3")
    scopes = {}
    lower_bounds = {}
    pages = {}

    def healthy_window(alias, observation):
        return (observation["source"] == "live_lab_http_requests"
                and observation["environment_id"] == scopes[alias][2]
                and observation["window_start"] > lower_bounds[alias]
                and classify(observation) == "healthy" and observation["error_rate"] == 0)

    try:
        for alias, state in environments.items():
            proof = proofs[alias]
            record = proof["incident"]
            incident = record["incident_id"]
            observation = json.loads(record["observation"])
            service = TARGETS[alias]
            if (observation["source"] != "live_lab_http_requests" or observation["service"] != service
                    or classify(observation) != "abnormal"):
                raise RuntimeError(f"{alias}: cannot authorize recovery without scoped HTTP evidence")
            environment_id = observation["environment_id"]
            investigation = proof["investigation"]
            if (investigation["incident_id"] != incident or investigation["environment_id"] != environment_id
                    or investigation["service"] != service or investigation["source_session_id"] != record["session_id"]
                    or investigation["session_id"] == record["session_id"]
                    or investigation["source_report_item_id"] != record["report_item_id"]
                    or investigation["skill_sha256"] != record["skill_sha256"]):
                raise RuntimeError(f"{alias}: follow-up evidence does not belong to the incident")
            scope = f"{alias}/{environment_id}/{service}"
            scopes[alias] = incident, scope, environment_id
            runtime.observe(
                f"{incident}-trigger", incident, scope, observation["requests"],
                round(observation["requests"] * observation["error_rate"]),
                quality="unhealthy", evidence=observation,
            )
            runtime.start(incident, record["skill_sha256"])
            operation = f"{incident}-clear-fault"
            evidence = f"collection/{alias}/investigation.json"
            plan = investigation["report"]["recovery_plan"]
            if plan["approval_required"]:
                runtime.request_action(incident, operation, evidence, action=plan["action"])
            runtime.handoff(incident, APPROVER, [operation] if plan["approval_required"] else [])
            page_path = iteration / "collection" / alias / "page.json"
            if page_path.is_file():
                page = load_json(page_path)
                if (page["incident_id"] != incident or page["scope"] != scope
                        or page["recipient"] != APPROVER or page["evidence"] != evidence
                        or page["session_id"] != investigation["session_id"]
                        or page["report_item_id"] != investigation["report_item_id"]
                        or page["report"] != investigation["report"]):
                    raise RuntimeError(f"{alias}: page does not refer to the verified investigation")
                pages[alias] = page
            else:
                pages[alias] = page_operator(page_path, APPROVER, scope, investigation, evidence)

        resumed = wait_for_patrols(workers, lambda alias, state: newer_checkpoint(
            state, TARGETS[alias], pages[alias]["paged_at"], scopes[alias][2],
        ))
        for alias, state in environments.items():
            incident, scope, environment_id = scopes[alias]
            service = TARGETS[alias]
            if (not pages[alias]["report"]["recovery_plan"]["approval_required"]
                    or classify(scoped_observation(state, service, environment_id)) != "abnormal"):
                raise RuntimeError(f"{alias}: the paged incident has no current fault eligible for lab recovery")
            operation = f"{incident}-clear-fault"
            runtime.approve(incident, operation, scope, APPROVER)
            receipt = runtime.execute(
                incident, operation, scope, apply=lambda: clear_lab_fault(state, service, environment_id),
            )
            lower_bounds[alias] = receipt["effect"]["cleared_at"]

        for index in (1, 2):
            windows = wait_for_metrics(environments, healthy_window)
            for alias, window in windows.items():
                incident, scope, _ = scopes[alias]
                runtime.observe(
                    f"{incident}-recovery-{index}", incident, scope, window["requests"], 0,
                    source="verification", quality="healthy", evidence=window,
                )
                lower_bounds[alias] = window["window_end"]
        for incident, _, _ in scopes.values():
            runtime.close(incident, APPROVER)
        return windows, resumed
    finally:
        write_json(iteration / "incident-lifecycle.json", runtime.report())


def receipt(state, incident):
    with closing(sqlite3.connect(state / "hermes" / "state.db")) as db:
        def content(item_id):
            row = db.execute("SELECT session_id, content FROM messages WHERE id = ?", (item_id,)).fetchone()
            if row is None or row[0] != incident["session_id"]:
                raise RuntimeError(f"Missing Hermes receipt {item_id}")
            return row[1]

        report = decode_report(content(incident["report_item_id"]))
        skill = json.loads(content(incident["skill_item_id"]))
        if skill["sha256"] != incident["skill_sha256"]:
            raise RuntimeError("Hermes Skill receipt differs from the incident record")
        rows = db.execute("""
            SELECT id, role, tool_name, tool_call_id, tool_calls, content FROM messages
            WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id
        """, (incident["session_id"], incident["skill_item_id"], incident["report_item_id"]))
        issued = {}
        probes = []
        for item_id, role, tool_name, call_id, calls, payload in rows:
            if role == "assistant" and calls:
                issued.update({call["id"]: item_id for call in json.loads(calls)})
            if role == "tool" and tool_name == "oncall_probe":
                probes.append({
                    "item_id": item_id, "call_item_id": issued[call_id], **json.loads(payload),
                })
    return {"incident": incident, "report": report, "skill": skill, "probes": probes}


def wait_for_patrols(workers, read, timeout=180):
    deadline = time.monotonic() + timeout
    results = {}
    while time.monotonic() < deadline:
        for alias, (state, child) in workers.items():
            if child.poll() is not None:
                raise RuntimeError(f"Patrol {alias} stopped. Read {state / 'patrol.log'}")
            if alias not in results and (value := read(alias, state)) is not None:
                results[alias] = value
        if results.keys() == workers.keys():
            return results
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for patrols: {', '.join(workers.keys() - results.keys())}")


def newer_checkpoint(state, service, after, environment_id, *, healthy=False):
    if not (state / "patrol.sqlite").is_file():
        return None
    with closing(connect(state, readonly=True)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'checkpoints'").fetchone() is None:
            return None
        record = checkpoint(db, service)
    if record is None or record["checked_at"] <= after:
        return None
    observation = json.loads(record["observation"])
    if observation["environment_id"] != environment_id:
        return None
    if healthy and (record["status"] != "healthy" or observation["error_rate"] != 0):
        return None
    return record


def start_patrols(root, environments, stack, live, *, scoped, langfuse, implementation_file=None):
    workers = {}
    for alias, environment in environments.items():
        state = root / "patrols" / alias
        state.mkdir(parents=True)
        args = ["run", "--fleet-worker", "--interval", "1", "--environment-state", str(environment)]
        if not live:
            args.append("--offline")
        if scoped:
            args.extend(("--service", TARGETS[alias]))
        if langfuse:
            args.append("--langfuse")
        if implementation_file is not None:
            args.extend(("--implementation", str(implementation_file)))
        workers[alias] = state, stack.enter_context(process("patrol.py", state, state / "patrol.log", *args))
    return workers


def collect_patrols(root, environments, workers, receivers, skill_hash, options, inject):
    from skillclaw_bridge import capture

    root.mkdir()
    with ExitStack() as stack:
        dispatches = {}
        for alias, (state, _) in workers.items():
            directory = root / alias
            directory.mkdir()
            skill = receivers[alias] / ONCALL_SKILL / "SKILL.md"
            if sha256(skill) != skill_hash:
                raise RuntimeError(f"{alias} did not receive the approved Skill")
            proxy = stack.enter_context(capture(root, alias, receivers[alias], options["base_url"],
                                                options["api_key"], options["model"], group_id=GROUP))
            dispatches[state] = {"directory": str(directory), "service": TARGETS[alias], "armed_at": time.time(),
                                 "skill": str(skill), "skill_sha256": skill_hash, "capture_url": proxy}
        injected_at = inject()
        wait_for_metrics(environments, lambda alias, value: value["window_end"] >= injected_at[alias]
                         and value["requests"] >= 25 and classify(value) == "abnormal")
        for state, dispatch in dispatches.items():
            write_json(state / "dispatch.json", dispatch)

        def detected(alias, _state):
            path = root / alias / "detected.json"
            return load_json(path) if path.is_file() else None

        detections = wait_for_patrols(workers, detected)
    records = {}
    for alias, (state, child) in workers.items():
        with closing(connect(state, readonly=True)) as db:
            record = next((item for item in incidents(db, TARGETS[alias])
                           if item["incident_id"] == detections[alias]["incident_id"]), None)
        if (record is None or record["skill_sha256"] != skill_hash or detections[alias]["pid"] != child.pid
                or json.loads(record["observation"])["window_end"] < injected_at[alias]):
            raise RuntimeError(f"{alias}: the running patrol did not record the injected fault with the pinned Skill")
        records[alias] = state, record
    return (*archive_patrols(root, records, skill_hash), injected_at)


def patrol_round(root, environments, receivers, skill_hash, options, live, *, scoped=False, langfuse=False):
    from skillclaw_bridge import capture

    root.mkdir()
    work = {}
    with ExitStack() as stack:
        proxies = {
            alias: stack.enter_context(capture(
                root, alias, receivers[alias], options["base_url"], options["api_key"],
                options["model"], group_id=GROUP,
            ))
            for alias in environments
        }
        children = {}
        for alias, state in environments.items():
            instance = root / alias
            instance.mkdir()
            work[alias] = instance
            skill = receivers[alias] / ONCALL_SKILL / "SKILL.md"
            if sha256(skill) != skill_hash:
                raise RuntimeError(f"{alias} did not receive the approved Skill")
            args = [
                "run", "--oncall", "--rounds", "1", "--sample", "4",
                "--environment-state", str(state), "--skill-file", str(skill),
                "--capture-url", proxies[alias],
                "--incident-id", read_alert(state, TARGETS[alias])["incident_id"],
                "--service", TARGETS[alias],
            ]
            if not live:
                args.append("--offline")
            if langfuse:
                args.append("--langfuse")
            children[alias] = stack.enter_context(process(
                "patrol.py", instance, instance / "patrol.log", *args,
            ))
        for alias, child in children.items():
            if child.wait(timeout=180) != 0:
                raise RuntimeError(f"Patrol {alias} failed. Read {work[alias] / 'patrol.log'}")
    records = {}
    for alias, state in work.items():
        with closing(connect(state)) as db:
            found = incidents(db, TARGETS[alias])
        if len(found) != 1 or found[0]["skill_sha256"] != skill_hash:
            raise RuntimeError(f"{alias} did not complete exactly one investigation with the pinned Skill")
        records[alias] = state, found[0]
    return archive_patrols(root, records, skill_hash)


def archive_patrols(root, records, skill_hash):
    from skillclaw_bridge import incident_archive

    proofs, sources = {}, []
    for alias, (state, record) in records.items():
        path = incident_archive(
            root, alias, record["session_id"], group_id=GROUP,
            incident_id=record["incident_id"], skill_sha256=skill_hash,
        )
        retained = root / alias / "session.json"
        shutil.copyfile(path, retained)
        proofs[alias] = {**receipt(state, record), "archive": str(retained.relative_to(root))}
        sources.append(retained)
    write_json(root / "patrol-evidence.json", proofs)
    return proofs, sources


def investigate_round(root, environments, proofs, workers, live, *, langfuse=False, implementation_file=None):
    with ExitStack() as stack:
        children = {}
        for alias, environment_state in environments.items():
            state = root / alias
            args = [
                "--environment-state", str(environment_state), "--service", TARGETS[alias],
                "--incident-id", proofs[alias]["incident"]["incident_id"],
                "--out", str(state / "investigation.json"),
                "--page-instance", alias,
            ]
            if not live:
                args.append("--offline")
            if langfuse:
                args.append("--langfuse")
            if implementation_file is not None:
                args.extend(("--implementation", str(implementation_file)))
            children[alias] = stack.enter_context(process(
                "investigation.py", workers[alias][0], state / "investigation.log", *args,
            ))
        for alias, child in children.items():
            state = root / alias
            if child.wait(timeout=180) != 0:
                raise RuntimeError(f"Follow-up {alias} failed. Read {state / 'investigation.log'}")
            proofs[alias]["investigation"] = json.loads((state / "investigation.json").read_text(encoding="utf-8"))
    write_json(root / "patrol-evidence.json", proofs)


def finish_patrols(root, workers, proofs, recovered):
    healthy = wait_for_patrols(workers, lambda alias, state: newer_checkpoint(
        state, TARGETS[alias], recovered[alias]["window_end"], recovered[alias]["environment_id"], healthy=True,
    ))
    for alias, (state, _) in workers.items():
        incident = proofs[alias]["incident"]["incident_id"]
        directory = root / alias
        with writer_lock(state), closing(connect(state)) as db:
            if not link_recovery(db, incident, healthy[alias]["id"]):
                raise RuntimeError(f"{alias}: unable to link the closed incident to the patrol checkpoint")
            with closing(sqlite3.connect(directory / "patrol.sqlite")) as saved:
                db.backup(saved)
            (directory / "hermes").mkdir()
            with (closing(sqlite3.connect(state / "hermes/state.db")) as sessions,
                  closing(sqlite3.connect(directory / "hermes/state.db")) as saved):
                sessions.backup(saved)
        (state / "dispatch.json").unlink()
    return healthy


def review_facts(iteration, proofs, recovered, shared, live, *, openviking=None):
    from shared_facts import publish_fact

    if openviking is not None:
        from openviking_handoff import publish_fact as publish_openviking

    review_root = iteration / "fact-review"
    entries = {}
    with ExitStack() as stack:
        children = {}
        for alias, proof in proofs.items():
            incident = proof["incident"]
            branch = proof["investigation"]
            fact = {
                "incident_id": incident["incident_id"], "instance": alias,
                "environment_id": branch["environment_id"], "service": branch["service"],
                "source_session_id": incident["session_id"], "source_report_item_id": incident["report_item_id"],
                "investigation_session_id": branch["session_id"], "investigation_report_item_id": branch["report_item_id"],
                "probe_statuses": [probe["http_status"] for probe in branch["probes"]],
                "recovery_end": recovered[alias]["window_end"], "source_skill_sha256": incident["skill_sha256"],
            }
            state = review_root / alias
            state.mkdir(parents=True)
            evidence = state / "evidence.json"
            proposal_path = state / "proposal.json"
            write_json(evidence, fact)
            entries[alias] = {"evidence": fact, "proposal": str(proposal_path.relative_to(iteration))}
            args = ["--evidence", str(evidence), "--out", str(proposal_path)]
            if not live:
                args.append("--offline")
            children[alias] = stack.enter_context(process("fact_review.py", state, state / "review.log", *args))
        for alias, child in children.items():
            if child.wait(timeout=180) != 0:
                raise RuntimeError(f"Background review {alias} failed. Read {review_root / alias / 'review.log'}")

    for alias, entry in entries.items():
        proposal = json.loads((iteration / entry["proposal"]).read_text(encoding="utf-8"))
        status = proposal["status"]
        native = proposal["native_review"]
        if (proposal["source_sha256"] != sha256(review_root / alias / "evidence.json")
                or status not in ("proposed", "keep")
                or native["completed"] is not True or native["memory_write_verified"] is not (status == "proposed")
                or proposal["fact"] != (entry["evidence"] if status == "proposed" else None)):
            raise RuntimeError(f"{alias}: background proposal differs from the verified incident evidence")
        memory_file = Path("hermes/memories/MEMORY.md")
        if status == "proposed" and (native["memory_item"] != memory_file.as_posix()
                                     or native["memory_sha256"] != sha256(review_root / alias / memory_file)):
            raise RuntimeError(f"{alias}: native review Memory read-back differs from the proposal")
        entry["status"] = status
    for entry in entries.values():
        if entry["status"] == "proposed":
            if openviking is None:
                publish_fact(shared, entry["evidence"])
            else:
                entry["openviking"] = publish_openviking(openviking, entry["evidence"])
    return entries


def handoff_facts(iteration, environments, prior, shared, live, *, openviking=None):
    handoff_root = iteration / "fact-handoff"
    entries = {}
    shared_before = sha256(shared) if shared.is_file() else None
    with ExitStack() as stack:
        children = {}
        for receiver, source in (("instance-a", "instance-b"), ("instance-b", "instance-a")):
            fact = prior[source]["evidence"]
            state = handoff_root / receiver
            state.mkdir(parents=True)
            output = state / "handoff.json"
            args = ["--incident-id", fact["incident_id"], "--service", fact["service"],
                    "--environment-id", fact["environment_id"], "--environment-state", str(environments[source]),
                    "--out", str(output)]
            if openviking is None:
                args.extend(("--memory", str(shared)))
            else:
                args.extend(("--openviking", "--openviking-namespace", openviking["namespace"],
                             "--receiver", receiver))
            if not live:
                args.append("--offline")
            children[receiver] = stack.enter_context(process("fact_handoff.py", state, state / "handoff.log", *args))
            entries[receiver] = {"source_instance": source, "receipt": str(output.relative_to(iteration))}
        for receiver, child in children.items():
            if child.wait(timeout=180) != 0:
                raise RuntimeError(f"Handoff {receiver} failed. Read {handoff_root / receiver / 'handoff.log'}")

    if (sha256(shared) if shared.is_file() else None) != shared_before:
        raise RuntimeError("The shared fact store changed during read-only handoff")
    for receiver, entry in entries.items():
        source = prior[entry["source_instance"]]
        receipt = json.loads((iteration / entry["receipt"]).read_text(encoding="utf-8"))
        expected = source["evidence"] if source["status"] == "proposed" else None
        current = receipt["current_receipt"]
        if (receipt["status"] != ("recalled" if expected is not None else "memory_missing")
                or receipt["memory_receipt"]["historical_only"] is not True
                or receipt["memory_receipt"]["fact"] != expected or current["source"] != "live_lab_http_requests"
                or current["environment_id"] != source["evidence"]["environment_id"]
                or current["service"] != source["evidence"]["service"]
                or current["window_end"] <= source["evidence"]["recovery_end"]
                or classify(current) != "healthy" or receipt["current_status"] != "healthy"
                or receipt["session_id"] in (source["evidence"]["source_session_id"],
                                             source["evidence"]["investigation_session_id"])):
            raise RuntimeError(f"{receiver}: cross-instance recall or current HTTP evidence could not be verified")
        if openviking is not None:
            memory = receipt["memory_receipt"]
            runbook = openviking["runbooks"][current["service"]]
            fact_publication = source["openviking"] if expected is not None else {}
            if (memory["backend"] != "openviking"
                    or (memory["runbook_uri"], memory["runbook_sha256"]) != (runbook["uri"], runbook["sha256"])
                    or (memory["fact_uri"], memory["fact_sha256"])
                    != (fact_publication.get("uri"), fact_publication.get("sha256"))
                    or receipt["report"]["runbook_uri"] != runbook["uri"]
                    or receipt["report"]["fact_uri"] != fact_publication.get("uri")):
                raise RuntimeError(f"{receiver}: OpenViking citation differs from the published resource")
            entry.update(backend="openviking", runbook_uri=runbook["uri"], fact_uri=memory["fact_uri"])
        entry["recalled"] = expected is not None
    return entries


def revise_diagnostic_memory(iteration, environment, fact, live):
    from environment import DIAGNOSTIC_V1, DIAGNOSTIC_V2, migrate_diagnostics, probe_diagnostic
    from route_task import ROUTE_PREFIX

    root = iteration / "route-migration"
    root.mkdir()
    scope = {key: fact[key] for key in ("service", "environment_id")}
    service, environment_id = scope["service"], scope["environment_id"]
    before = probe_diagnostic(environment, service, DIAGNOSTIC_V1, environment_id)
    if before["http_status"] != 200 or before["active"] is not True:
        raise RuntimeError("Cannot seed diagnostic Memory without a valid old route")
    write_json(root / "before.json", before)
    old_memory = root / "original-memory.md"
    original = ROUTE_PREFIX + json.dumps({**scope, "path": DIAGNOSTIC_V1}, sort_keys=True, separators=(",", ":"))
    old_memory.write_text(original + "\n", encoding="utf-8")
    original_sha = sha256(old_memory)
    migrate_diagnostics(environment, service)

    def worker(phase, script, args):
        state = root / phase
        state.mkdir()
        output = state / "result.json"
        cli = [*args, "--out", str(output)]
        if not live:
            cli.append("--offline")
        with process(script, state, state / "worker.log", *cli) as child:
            if child.wait(timeout=180) != 0:
                raise RuntimeError(f"Diagnostic Memory {phase} failed. Read {state / 'worker.log'}")
        return json.loads(output.read_text(encoding="utf-8"))

    task_args = ["--environment-state", str(environment), "--service", service, "--environment-id", environment_id]
    repair = worker("repair", "route_task.py", [*task_args, "--memory-file", str(old_memory)])
    repair_probes = [item["receipt"] for item in repair["tool_receipts"]]
    if (repair["scope"] != scope or repair["memory_sha256"] != original_sha
            or repair["native_memory_loaded"] is not True or repair["loaded_memory_entry"] != original
            or [(item["path"], item["http_status"]) for item in repair_probes] != [(DIAGNOSTIC_V1, 410), (DIAGNOSTIC_V2, 200)]
            or repair_probes[0]["replacement"] != DIAGNOSTIC_V2 or repair["report"]["path"] != DIAGNOSTIC_V2):
        raise RuntimeError("Diagnostic repair did not verify both the retired and the current route")

    repair_file = root / "repair" / "result.json"
    proposal = worker("review", "route_revision.py", ["--repair", str(repair_file), "--memory-file", str(old_memory)])
    native = proposal["native_review"]
    memory_item = Path("hermes/memories/MEMORY.md")
    revised_memory = root / "review" / memory_item
    if (proposal["status"] != "proposed" or proposal["source_sha256"] != sha256(repair_file)
            or proposal["repair_session_id"] != repair["session_id"] or proposal["repair_report_item_id"] != repair["report_item_id"]
            or native["completed"] is not True or native["memory_write_verified"] is not True
            or native["memory_before_sha256"] != original_sha or native["memory_item"] != memory_item.as_posix()
            or native["memory_sha256"] != sha256(revised_memory)
            or [call["action"] for call in native["memory_tool_calls"]] != ["replace"]):
        raise RuntimeError("Native review did not verify a scoped Memory replacement")
    revised = revised_memory.read_text(encoding="utf-8").strip()
    if (not revised.startswith(ROUTE_PREFIX)
            or json.loads(revised.removeprefix(ROUTE_PREFIX)) != {**scope, "path": DIAGNOSTIC_V2}):
        raise RuntimeError("Revised Memory does not contain the verified diagnostic route")

    adoption = worker("adopt", "route_task.py", [*task_args, "--memory-file", str(revised_memory)])
    probes = [item["receipt"] for item in adoption["tool_receipts"]]
    if (adoption["scope"] != scope or adoption["session_id"] == repair["session_id"]
            or adoption["native_memory_loaded"] is not True or adoption["loaded_memory_entry"] != revised
            or adoption["memory_sha256"] != native["memory_sha256"] or adoption["report"]["path"] != DIAGNOSTIC_V2
            or [(item["path"], item["http_status"]) for item in probes] != [(DIAGNOSTIC_V2, 200)]
            or sha256(old_memory) != original_sha or sha256(root / "repair" / memory_item) != original_sha
            or sha256(revised_memory) != native["memory_sha256"]):
        raise RuntimeError("Fresh diagnostic task did not adopt the revised Memory without changing either source")

    result = {"status": "adopted", "scope": scope, "baseline": "before.json", "repair": "repair/result.json",
              "review": "review/result.json", "adoption": "adopt/result.json", "original_memory": old_memory.name,
              "active_memory": str(revised_memory.relative_to(root)), "active_memory_sha256": native["memory_sha256"]}
    write_json(root / "sequence.json", result)
    return str((root / "sequence.json").relative_to(iteration))


def verify_adoption(proof, faults):
    """Require the same conditional behavior in the new live lab tasks."""
    for alias, mode in faults.items():
        entry = proof[alias]
        probes = entry["probes"]
        report = entry["report"]
        statuses, assessment = ([503, 200], "intermittent") if mode == "intermittent" else ([503, 503], "persistent")
        if ([probe.get("http_status") for probe in probes] != statuses
                or report.get("probe_http_statuses") != statuses
                or report.get("assessment") != assessment
                or probes[0]["item_id"] >= probes[1]["call_item_id"]):
            raise RuntimeError(f"{alias} did not demonstrate the approved Skill's conditional probe")


def run_rollback_rehearsal(root, report, environments, receivers, recovered, options, live):
    from skillclaw_evolve import FORMAL_GROUP, release

    first, last = report["rounds"]
    if first["decision"] != "publish" or not last["adoption_verified"]:
        report["rollback_rehearsal"] = {"status": "pending", "reason": "no_adopted_release"}
        return

    formal = root / "formal"
    previous_sha = last["formal_sha256"]
    seed_sha = report["seed_release"]["sha256"]
    result = {"status": "running", "previous_sha256": previous_sha}
    report["rollback_rehearsal"] = result
    report_path = root / "report.json"
    write_json(report_path, report)
    try:
        entrypoint = Path(ONCALL_SKILL) / "SKILL.md"
        published = formal / "shared" / FORMAL_GROUP / "skills" / entrypoint
        if (previous_sha == seed_sha or previous_sha != first["release"]["sha256"]
                or sha256(published) != previous_sha
                or any(sha256(directory / entrypoint) != previous_sha for directory in receivers.values())):
            raise RuntimeError("Rollback requires both receivers to hold the adopted formal release")
        for alias, state in environments.items():
            flag = state / f"{TARGETS[alias]}.fault"
            if flag.exists() or flag.is_symlink():
                raise RuntimeError(f"{alias}: clear the existing lab fault before rehearsing rollback")

        seed = formal / "versions" / seed_sha / entrypoint
        rollback = release(formal, seed, seed_sha, receivers, restoring=True)
        report["rollback"] = rollback
        if rollback["receiver_errors"] or rollback["receivers"].keys() != receivers.keys():
            report["rollback_error"] = "Some receivers did not restore the approved Skill"
            raise RuntimeError(report["rollback_error"])
        restored_at = time.time()
        if any(window["window_end"] >= restored_at for window in recovered.values()):
            raise RuntimeError("Rollback must follow both verified recovery windows")
        result.update(restored_sha256=seed_sha, restored_at=restored_at)

        with ExitStack() as cleanup:
            injected = set_faults(environments, dict.fromkeys(environments, "on"), cleanup=cleanup)
            result["injected_at"] = injected
            write_json(report_path, report)
            wait_for_metrics(environments, lambda alias, obs: (
                obs["environment_id"] == recovered[alias]["environment_id"]
                and obs["window_end"] >= injected[alias]
                and obs["requests"] >= 25 and obs["error_rate"] >= 0.05
                and (alert := read_alert(environments[alias], TARGETS[alias])) is not None
                and alert["emitted_at"] >= injected[alias]
            ))
            drill = root / "rollback-rehearsal"
            proofs, _ = patrol_round(drill, environments, receivers, seed_sha, options, live, scoped=True)
            result["patrol_evidence"] = str((drill / "patrol-evidence.json").relative_to(root))
            earlier = {session for phase in report["rounds"] for session in phase["source_sessions"].values()}
            for alias, proof in proofs.items():
                incident = proof["incident"]
                observation = json.loads(incident["observation"])
                probes, verdict = proof["probes"], proof["report"]
                if (incident["session_id"] in earlier or incident["skill_sha256"] != seed_sha
                        or observation["environment_id"] != recovered[alias]["environment_id"]
                        or [probe.get("http_status") for probe in probes] != [503]
                        or not restored_at <= injected[alias] <= observation["window_end"] <= probes[0]["checked_at"]
                        or verdict.get("probe_http_status") != 503 or verdict.get("probe_http_statuses") != [503]
                        or verdict.get("assessment") != "failure_observed"):
                    raise RuntimeError(f"{alias}: fresh patrol did not demonstrate the restored Skill")
                earlier.add(incident["session_id"])
        result["status"] = "completed"
    except BaseException:
        result["status"] = "failed"
        raise


def run(root, options, live, *, gepa=False, enterprise=False, langfuse=False, openviking=False,
        rehearse_rollback=False, implementation_file=None):
    sys.path.insert(0, str(HERE.parent))
    from implementation import REFERENCE, load_implementation
    from oncall_runtime import Runtime

    implementation_file = Path(implementation_file or REFERENCE).resolve()
    implementation = load_implementation(implementation_file)
    with Runtime(root, options, live, gepa=gepa, enterprise=enterprise,
                 langfuse=langfuse, openviking=openviking, rehearse_rollback=rehearse_rollback,
                 implementation_file=implementation_file) as runtime:
        runtime.implementation = implementation
        implementation.run(runtime)
        runtime.finish()
    return runtime.report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run continuous On-call service or a controlled two-round rehearsal")
    parser.add_argument("--out", type=Path, required=True, help="New directory for the complete run")
    parser.add_argument("--serve", action="store_true", help="Keep both On-call instances running for external incidents")
    parser.add_argument("--live", action="store_true", help="Use the configured GLM model endpoint")
    parser.add_argument("--gepa", action="store_true", help="Run the optional Lesson 20 offline GEPA comparison")
    parser.add_argument("--enterprise", action="store_true", help="Capture and score multi-service incident snapshots")
    parser.add_argument("--langfuse", action="store_true", help="Send patrol and follow-up traces to the configured Langfuse project")
    parser.add_argument("--openviking", action="store_true", help="Use the configured OpenViking server for cross-instance handoff")
    parser.add_argument("--rehearse-rollback", action="store_true",
                        help="Restore the prior approved Skill and verify it in fresh lab patrols")
    parser.add_argument("--implementation", type=Path, help="Use this On-call implementation throughout the run")
    args = parser.parse_args(argv)
    if args.serve:
        if any((args.gepa, args.enterprise, args.langfuse, args.openviking, args.rehearse_rollback)):
            parser.error("Extension and rollback flags apply to the controlled rehearsal")
        sys.path.insert(0, str(HERE.parent))
        from implementation import REFERENCE
        from oncall_service import serve

        ensure_loopback_no_proxy()
        serve(args.out.resolve(), args.live, (args.implementation or REFERENCE).resolve())
        return
    if args.openviking:
        from openviking_handoff import check_openviking

        try:
            ensure_loopback_no_proxy()
            check_openviking()
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    if args.langfuse:
        if not args.enterprise:
            parser.error("--langfuse requires --enterprise")
        from hermes_runtime import check_langfuse

        try:
            check_langfuse()
        except (ImportError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    if args.gepa:
        from gepa_search import require_gepa, require_live_reflection

        try:
            require_gepa()
            if args.live:
                require_live_reflection()
        except (ImportError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    root = args.out.resolve()
    with model_connection(not args.live) as url:
        root.mkdir(parents=True, exist_ok=False)
        options = {
            "base_url": url,
            "api_key": os.environ["GLM_API_KEY"] if args.live else "local-fixture",
            "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if args.live else "patrol-fixture",
        }
        report = run(root, options, args.live, gepa=args.gepa, enterprise=args.enterprise,
                     langfuse=args.langfuse, openviking=args.openviking, rehearse_rollback=args.rehearse_rollback,
                     implementation_file=args.implementation)
    print(f"{root / 'report.json'} ({report['status']})", flush=True)


if __name__ == "__main__":
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    main()
