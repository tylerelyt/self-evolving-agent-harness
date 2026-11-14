"""Verify completed fleet evidence from persisted run artifacts and SessionDB."""

from contextlib import closing
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3

from environment import ALERT_SOURCE, DIAGNOSTIC_V1, DIAGNOSTIC_V2, classify, load_json
from export_workset import (build_workset, recorded_probes, require, run_file, scoped_snapshot,
                            session_rows, verify_skill)
from fact_handoff import read_evidence as read_handoff
from fact_review import FACT_PREFIX, MEMORY_FILE
from fleet_eval import CASES
from hermes_runtime import ONCALL_SKILL, PATROL_SKILL
from patrol import STEER_PREFIX, decode_report
from recovery_skill_flow import recovery_evidence
from recovery_skill_task import gate_cases, read_evidence as read_recovery
from route_task import read_evidence as read_route
from skillclaw_evolve import FORMAL_GROUP, STAGING_GROUP


INSTANCES = {"instance-a", "instance-b"}
SKILL_FILE = Path(ONCALL_SKILL) / "SKILL.md"


def read_json(root, path):
    return load_json(run_file(root, path))


def digest(root, path):
    return sha256(run_file(root, path).read_bytes()).hexdigest()


def session_turn(root, directory, result):
    db_path = run_file(root, directory / "hermes/state.db")
    wal = db_path.with_name(db_path.name + "-wal")
    require(not wal.exists() or wal.stat().st_size == 0, "Evidence SessionDB has an uncheckpointed WAL")
    session = result["session_id"]
    start, stop = result["prompt_item_id"], result["report_item_id"]
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        metadata = db.execute(
            "SELECT s.parent_session_id, COALESCE(p.prompt, s.system_prompt) "
            "FROM sessions s LEFT JOIN system_prompts p ON p.hash=s.system_prompt_hash WHERE s.id=?",
            (session,),
        ).fetchone()
        require(metadata is not None and metadata[0] is None, "Evidence must come from a fresh Session")
        rows = session_rows(db, session, start, stop)
    messages = [{**row, "tool_calls": json.loads(row["tool_calls"] or "[]")} for row in rows.values()]
    return messages, rows[start]["content"], metadata[1]


def audit_release(root, release, expected):
    manifest = release["manifest"]
    version = manifest["version"]
    require(re.fullmatch(r"[0-9a-f]{64}", expected) is not None
            and type(version) is int and version > 0
            and release["sha256"] == manifest["sha256"] == expected
            and manifest["name"] == ONCALL_SKILL and not release["receiver_errors"]
            and release["receivers"].keys() == INSTANCES
            and all(receiver["sha256"] == expected for receiver in release["receivers"].values()),
            "Formal release and receiver receipts disagree")
    snapshot = run_file(root, Path("formal/versions") / expected / SKILL_FILE)
    native = Path("formal/shared") / FORMAL_GROUP / "skills" / ONCALL_SKILL / "versions" / f"v{version}"
    bundle = read_json(root, native / "bundle.json")
    require(bundle == {key: manifest[key] for key in ("format", "entrypoint", "tree_sha256", "files")}
            and bundle["files"] == [{"path": "SKILL.md", "sha256": expected, "size": snapshot.stat().st_size}]
            and sha256(snapshot.read_bytes()).hexdigest() == digest(root, native / "SKILL.md") == expected,
            "Approved release bytes differ from the native version bundle")
    return version, expected


def audit_evolution(root, directory, round_report):
    evolution = read_json(root, directory / "staging/evolution.json")
    native = evolution["native_workflow"]
    baseline = round_report["active_sha256"]
    sessions = round_report["source_sessions"]
    require(evolution == round_report["evolution"] and evolution["baseline_sha256"] == baseline
            and len(evolution["sources"]) == len(sessions) == native["sessions"] == 2
            and native["skill_groups"] == 1 and native["no_skill_sessions"] == 0
            and not native["had_processing_error"] and native["uploaded_skills"] == 0
            and {entry["instance"]: entry["session_id"] for entry in evolution["sources"]} == sessions,
            "Native SkillClaw evolution is missing its two source sessions")
    staging = directory / "staging/shared" / STAGING_GROUP
    for entry in evolution["sources"]:
        archive = directory / "collection" / entry["instance"] / "session.json"
        source = read_json(root, archive)
        require(entry["archive_sha256"] == digest(root, archive) and entry["skill_sha256"] == baseline
                and source["session_id"] == entry["session_id"] and source["user_alias"] == entry["instance"],
                "A SkillClaw source archive differs from its reviewed Session")
    require(digest(root, staging / "skills" / SKILL_FILE) == baseline,
            "Native staging changed its baseline Skill")
    if evolution["status"] == "keep":
        require(not native["evolutions"], "A native keep contains an unexpected candidate")
        return None
    require(evolution["status"] == "candidate" and len(native["evolutions"]) == 1,
            "Unexpected native evolution outcome")
    item = native["evolutions"][0]
    job = item["validation_job_id"]
    require(isinstance(job, str) and re.fullmatch(r"[A-Za-z0-9_-]+", job)
            and item["action"] == "queued_for_validation" and item["skill_name"] == ONCALL_SKILL
            and len(item["session_ids"]) == 2 and set(item["session_ids"]) == set(sessions.values()),
            "Native validation job does not belong to the two patrol sessions")
    candidate = digest(root, staging / "candidate_skills" / job / "SKILL.md")
    require(candidate == evolution["candidate_sha256"], "Staged Skill differs from the candidate digest")
    return candidate


def audit_rollback_patrol(root, directory, proof, previous_case, restored, injected_at):
    from fleet import GROUP

    record = proof["incident"]
    scope = previous_case["snapshot"]
    service, environment = scope["service"], scope["environment_id"]
    session, incident = record["session_id"], record["incident_id"]
    require(re.fullmatch(rf"patrol-{service}-[0-9a-f]{{32}}", session)
            and re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident)
            and re.fullmatch(r"[0-9a-f]{32}", record["turn_id"])
            and record["skill_sha256"] == restored, "Invalid restored patrol identity or Skill")
    observation = scoped_snapshot(json.loads(record["observation"]), service, environment)
    require(classify(observation) == "abnormal" and observation["error_rate"] > 0
            and injected_at <= observation["window_end"], "Rollback patrol did not observe the injected failure")

    db_path = run_file(root, directory / "hermes/state.db")
    wal = db_path.with_name(db_path.name + "-wal")
    require(not wal.exists() or wal.stat().st_size == 0, "Rollback SessionDB has an uncheckpointed WAL")
    report_item, skill_item = record["report_item_id"], record["skill_item_id"]
    observation_item, steer_item = record["evidence_item_id"], record["steer_item_id"]
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        metadata = db.execute("SELECT parent_session_id, started_at FROM sessions WHERE id = ?", (session,)).fetchone()
        last_item = db.execute("SELECT MAX(id) FROM messages WHERE session_id = ?", (session,)).fetchone()[0]
        require(metadata is not None and metadata[0] is None and last_item == report_item
                and injected_at <= metadata[1] <= observation["window_end"],
                "Rollback evidence must come from a fresh completed Session after injection")
        rows = session_rows(db, session, 1, report_item)
    first = next(iter(rows.values()))
    require(first["role"] == "user" and record["turn_id"] in first["content"],
            "Fresh patrol is missing its recorded turn")
    calls = {call["id"]: (row["id"], call["function"]["name"])
             for row in rows.values() if row["role"] == "assistant"
             for call in json.loads(row["tool_calls"] or "[]")}
    for item in {observation_item, steer_item}:
        row = rows[item]
        call, name = calls[row["tool_call_id"]]
        observed = scoped_snapshot(json.loads(row["content"]), service, environment)
        require(row["role"] == "tool" and row["tool_name"] == name == "service_snapshot"
                and call < item < report_item and injected_at <= observed["window_end"],
                "Rollback snapshot lacks a matching scoped HTTP tool receipt")
        if item == observation_item:
            require(observed == observation, "Rollback observation differs from SessionDB")
        if item == steer_item:
            require(classify(observed) == "abnormal", "Rollback steer did not come from an abnormal observation")

    verify_skill(rows[skill_item], restored)
    skill_call, name = calls[rows[skill_item]["tool_call_id"]]
    require(name == "skill_view" and steer_item < skill_call < skill_item
            and json.loads(rows[skill_item]["content"]) == proof["skill"],
            "Rollback Skill read is missing or out of order")
    statuses = recorded_probes(rows, proof["probes"], "oncall_probe", {"service": service}, skill_item, report_item)
    require(statuses == [503], "Restored Skill did not show its single-probe behavior")
    probe = proof["probes"][0]
    result = decode_report(rows[report_item]["content"])
    require(record["probe_item_id"] == probe["item_id"]
            and type(probe["checked_at"]) in (int, float) and math.isfinite(probe["checked_at"])
            and observation["window_end"] <= probe["checked_at"]
            and rows[report_item]["role"] == "assistant" and result == proof["report"]
            and result["incident_id"] == incident and result["status"] == "abnormal"
            and result["probe_http_status"] == 503 and result["probe_http_statuses"] == statuses
            and result["assessment"] == "failure_observed", "Rollback report disagrees with its live HTTP probe")

    require(proof["archive"] == f"{previous_case['instance']}/session.json", "Unexpected rollback archive path")
    retained = directory / "session.json"
    native = directory.parent / "shared" / GROUP / "sessions" / f"{session}.json"
    require(digest(root, retained) == digest(root, native), "Retained rollback archive differs from SkillClaw")
    archive = read_json(root, retained)
    turns = archive["turns"]
    archived_tools = {entry["tool_call_id"]: entry for turn in turns for entry in turn["tool_results_raw"]}
    require(archive["session_id"] == session and archive["user_alias"] == previous_case["instance"]
            and archive["num_turns"] == len(turns) and not turns[-1].get("tool_calls")
            and decode_report(turns[-1]["response_text"]) == result,
            "Native rollback archive differs from the new patrol report")
    for item in {observation_item, steer_item, skill_item, probe["item_id"]}:
        row = rows[item]
        saved = archived_tools[row["tool_call_id"]]
        content, end = json.JSONDecoder().raw_decode(saved["content"])
        require(saved["name"] == row["tool_name"] and content == json.loads(row["content"]),
                "Native rollback archive differs from the new patrol tool receipt")
        if item == steer_item:
            delivery = saved["content"][end:].strip()
            require(delivery == record["steer_delivery"] and STEER_PREFIX in delivery,
                    "Native rollback archive did not retain the steer for this incident")
            alert = json.loads(delivery.split(STEER_PREFIX, 1)[1].splitlines()[0])
            source = json.loads(row["content"])
            identity = {"incident_id": incident, "service": service}
            if alert.get("source") == ALERT_SOURCE:
                verify_monitor_alert(alert, incident, source, injected_at)
            else:
                require(incident == f"lab-{service}-{record['turn_id'][:12]}"
                        and alert in ({**identity, "environment_id": source["environment_id"]},
                                      {**identity, "error_rate": source["error_rate"], "p95_ms": source["p95_ms"]}),
                        "Rollback steer differs from its HTTP snapshot")
    return session, incident


def audit_rollback_rehearsal(root, workset, rehearsal, previous, release):
    path = Path("rollback-rehearsal/patrol-evidence.json")
    restored = release["sha256"]
    require(rehearsal["status"] == "completed" and rehearsal["patrol_evidence"] == path.as_posix()
            and rehearsal["previous_sha256"] == previous and rehearsal["restored_sha256"] == restored,
            "Rollback rehearsal does not bind to the verified release chain")
    require(release["push"]["uploaded"] == 1
            and all(receiver["pull"]["downloaded"] == 1 for receiver in release["receivers"].values()),
            "Rollback did not record a native publication and two receiver pulls")
    published = datetime.fromisoformat(release["manifest"]["uploaded_at"])
    require(published.utcoffset() is not None, "Rollback publication time needs a timezone")
    published_at = published.timestamp()
    restored_at = rehearsal["restored_at"]
    injected = rehearsal["injected_at"]
    proofs = read_json(root, path)
    require(type(restored_at) in (int, float) and math.isfinite(restored_at)
            and isinstance(proofs, dict) and isinstance(injected, dict)
            and proofs.keys() == injected.keys() == INSTANCES, "Invalid rollback rehearsal scope or timing")
    prior_sessions = {case["provenance"][key] for case in workset["cases"]
                      for key in ("patrol_session_id", "investigation_session_id")}
    prior_incidents = {case["incident_id"] for case in workset["cases"]}
    prior_cases = {case["instance"]: case for case in workset["cases"] if case["round"] == 2}
    for instance, proof in proofs.items():
        case = prior_cases[instance]
        started = injected[instance]
        require(type(started) in (int, float) and math.isfinite(started)
                and case["recovery_windows"][-1]["window_end"] <= published_at <= restored_at <= started,
                "Rollback injection must follow verified recovery and restoration")
        session, incident = audit_rollback_patrol(root, path.parent / instance, proof, case, restored, started)
        require(session not in prior_sessions and incident not in prior_incidents,
                "Rollback patrol reused an earlier incident or Session")
        prior_sessions.add(session)
        prior_incidents.add(incident)
    return {"status": "passed", "restored_sha256": restored, "fresh_patrols": len(proofs)}


def audit_skillclaw(root, report, workset):
    from audit_extensions import audit_http_gate

    latest = report["seed_release"]
    seed = active = latest["sha256"]
    releases = [audit_release(root, latest, active)]
    adoptions = 0
    for number, round_report in enumerate(report["rounds"], start=1):
        directory = Path(f"round-{number}")
        require(round_report["active_sha256"] == active and round_report["source_sessions"].keys() == INSTANCES,
                "Patrol did not start with the last formal release")
        if releases[-1][0] != releases[0][0]:
            # build_workset already proved these case receipts in the actual patrol SessionDB.
            cases = [case for case in workset["cases"] if case["round"] == number]
            require(len(cases) == 2 and all(case["skill_sha256"] == active
                        and case["probe_statuses"] in ([503, 503], [503, 200])
                        and ((case["probe_statuses"] == [503, 503])
                             == (case["investigation"]["assessment"] == "persistent")) for case in cases)
                    and round_report["adoption_verified"] is True,
                    "The published Skill lacks two new sessions demonstrating conditional confirmation")
            adoptions += len(cases)
        else:
            require(round_report["adoption_verified"] is False, "Adoption was claimed before a candidate was released")
        candidate = audit_evolution(root, directory, round_report)
        decision = round_report["decision"]
        if candidate is None or candidate == active:
            require(decision == "keep" and "evaluation" not in round_report, "Keep did not preserve the baseline")
        else:
            eval_path = directory / "evaluation/evaluation.json"
            evaluation = audit_http_gate(run_file(root, eval_path).parent,
                                         {case.name: case.fixture() for case in CASES}, active, candidate)
            require(evaluation == round_report["evaluation"], "Business replay differs from the fleet report")
            if evaluation["passed"]:
                expected = "publish" if number < len(report["rounds"]) else "qualified"
            else:
                expected = "reject"
            require(decision == expected, "Business replay and SkillClaw decision disagree")
            if decision == "publish":
                latest = round_report["release"]
                releases.append(audit_release(root, latest, candidate))
                active = candidate
        require(("release" in round_report) is (decision == "publish"), "Round contains a release without a publish decision")
        require(round_report["formal_sha256"] == active, "Formal pointer advanced without publication")
    rollback = {"status": "pending", "reason": "No post-rollback patrol is proven by this completed run"}
    rehearsal = report.get("rollback_rehearsal")
    if "rollback" in report:
        require(not report.get("rollback_error") and active != seed, "Rollback did not restore a prior release")
        previous = active
        latest = report["rollback"]
        releases.append(audit_release(root, latest, seed))
        active = seed
        rollback["restored_sha256"] = seed
        if rehearsal is not None:
            require(adoptions == 2 and releases[-1][0] == releases[-2][0] + 1,
                    "Rollback rehearsal requires a new native version after demonstrated live adoption")
            rollback = audit_rollback_rehearsal(root, workset, rehearsal, previous, latest)
    elif rehearsal is not None:
        require(rehearsal == {"status": "pending", "reason": "no_adopted_release"} and not adoptions,
                "Rollback rehearsal claimed evidence without a restored release")
    hub = Path("formal/shared") / FORMAL_GROUP
    registry = read_json(root, hub / "evolve_skill_registry.json")[ONCALL_SKILL]
    remote = [json.loads(line) for line in run_file(root, hub / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    require((registry["version"], registry["content_sha"]) == releases[-1]
            and [(entry["version"], entry["content_sha"]) for entry in registry["history"]] == releases
            and remote == [latest["manifest"]]
            and digest(root, hub / "skills" / SKILL_FILE) == active
            and all(digest(root, Path("receivers") / alias / SKILL_FILE) == active for alias in INSTANCES),
            "Formal registry, manifest or receiver copies do not match the release chain")
    release = {"status": "passed" if adoptions else "pending", "formal_sha256": active,
               "publications": len(releases), "live_adoptions": adoptions}
    return release, rollback


def fact_from_case(case):
    snapshot, refs = case["snapshot"], case["provenance"]
    return {"incident_id": case["incident_id"], "instance": case["instance"],
            "environment_id": snapshot["environment_id"], "service": snapshot["service"],
            "source_session_id": refs["patrol_session_id"], "source_report_item_id": refs["patrol_report_item_id"],
            "investigation_session_id": refs["investigation_session_id"],
            "investigation_report_item_id": refs["investigation_report_item_id"],
            "probe_statuses": case["investigation"]["probe_statuses"],
            "recovery_end": case["recovery_windows"][-1]["window_end"], "source_skill_sha256": case["skill_sha256"]}


def audit_handoffs(root, report, workset):
    rounds = report["rounds"]
    reviewed = {}
    for case in workset["cases"]:
        number, instance = case["round"], case["instance"]
        entry = rounds[number - 1]["fact_reviews"][instance]
        directory = Path(f"round-{number}/fact-review") / instance
        fact = fact_from_case(case)
        source = directory / "evidence.json"
        proposal = read_json(root, directory / "proposal.json")
        native = proposal["native_review"]
        proposed = proposal["status"] == "proposed"
        require(fact == entry["evidence"] == read_json(root, source)
                and proposal["source_sha256"] == digest(root, source)
                and proposal["status"] == entry["status"] and proposal["status"] in ("proposed", "keep")
                and native["completed"] is True and native["memory_write_verified"] is proposed
                and proposal["fact"] == (fact if proposed else None),
                "Historical Memory proposal differs from the verified incident")
        if proposed:
            memory = run_file(root, directory / MEMORY_FILE)
            line = memory.read_text(encoding="utf-8").strip()
            require(native["memory_item"] == MEMORY_FILE.as_posix() and line.startswith(FACT_PREFIX)
                    and json.loads(line.removeprefix(FACT_PREFIX)) == fact
                    and sha256(memory.read_bytes()).hexdigest() == native["memory_sha256"],
                    "Historical Memory write differs from the source fact")
        if number == 1:
            reviewed[instance] = (fact, proposed)
    handoffs = rounds[1]["fact_handoff"]
    namespace = report["openviking"]["namespace"] if "openviking" in report else None
    require(handoffs.keys() == INSTANCES, "Peer handoff requires receipts from both receivers")
    recalled = 0
    for receiver, summary in handoffs.items():
        source = summary["source_instance"]
        require(source in INSTANCES - {receiver}, "Handoff did not use the peer's evidence")
        fact, proposed = reviewed[source]
        scope = {key: fact[key] for key in ("incident_id", "service", "environment_id")}
        path = Path("round-2/fact-handoff") / receiver / "handoff.json"
        saved = read_json(root, path)
        rows, prompt, system = session_turn(root, path.parent, saved)
        actual = read_handoff(rows, prompt, scope, saved["session_id"],
                              openviking_namespace=namespace, receiver=receiver)
        require(actual == saved and saved["fact"] == (fact if proposed else None)
                and summary["recalled"] is proposed and saved["current_status"] == "healthy"
                and saved["current_receipt"]["window_end"] > fact["recovery_end"]
                and fact["source_session_id"] not in prompt + (system or ""),
                "Fresh handoff receipts disagree with the peer fact or subsequent live HTTP observation")
        recalled += proposed
    return {"status": "passed" if recalled == len(INSTANCES) else "pending",
            "reviewed_incidents": len(workset["cases"]), "peer_recalls": recalled}


def audit_diagnostic_memory(root, report):
    path = Path("round-2/route-migration/sequence.json")
    require(report["rounds"][1]["route_revision"] == path.relative_to("round-2").as_posix(),
            "Diagnostic Memory sequence must be scoped to round two")
    sequence = read_json(root, path)
    directory, scope = path.parent, sequence["scope"]
    require(sequence["status"] == "adopted", "Diagnostic Memory was not adopted")
    baseline = read_json(root, directory / sequence["baseline"])
    require(baseline["path"] == DIAGNOSTIC_V1 and baseline["http_status"] == 200
            and all(baseline[key] == value for key, value in scope.items()), "Old diagnostic route lacks a valid baseline")
    for stage, route, expected in (("repair", DIAGNOSTIC_V1, [(DIAGNOSTIC_V1, 410), (DIAGNOSTIC_V2, 200)]),
                                    ("adoption", DIAGNOSTIC_V2, [(DIAGNOSTIC_V2, 200)])):
        artifact = directory / sequence[stage]
        result = read_json(root, artifact)
        rows, prompt, system = session_turn(root, artifact.parent, result)
        actual = read_route(rows, prompt, scope, route)
        memory = run_file(root, artifact.parent / MEMORY_FILE)
        require(all(result[key] == value for key, value in actual.items()) and result["scope"] == scope
                and result["native_memory_loaded"] is True and isinstance(system, str)
                and sha256(system.encode()).hexdigest() == result["system_prompt_sha256"]
                and result["loaded_memory_entry"] == memory.read_text(encoding="utf-8").strip()
                and result["loaded_memory_entry"] in system and route not in prompt
                and result["memory_sha256"] == sha256(memory.read_bytes()).hexdigest()
                and [(item["receipt"]["path"], item["receipt"]["http_status"]) for item in actual["tool_receipts"]]
                == expected, "Diagnostic Memory did not match the observed HTTP migration")
    repair = read_json(root, directory / sequence["repair"])
    adoption = read_json(root, directory / sequence["adoption"])
    review = read_json(root, directory / sequence["review"])
    native = review["native_review"]
    require(review["status"] == "proposed" and native["completed"] is True and native["memory_write_verified"] is True
            and review["source_sha256"] == digest(root, directory / sequence["repair"])
            and review["repair_session_id"] == repair["session_id"] != adoption["session_id"]
            and review["repair_report_item_id"] == repair["report_item_id"]
            and native["memory_before_sha256"] == repair["memory_sha256"]
            == digest(root, directory / sequence["original_memory"])
            and native["memory_sha256"] == sequence["active_memory_sha256"] == adoption["memory_sha256"]
            == digest(root, directory / sequence["active_memory"])
            and [(call["action"], call["success"]) for call in native["memory_tool_calls"]] == [("replace", True)],
            "Native Memory replacement does not connect the repair and subsequent adoption")
    return {"status": "passed", "memory_sha256": adoption["memory_sha256"]}


def audit_recovery_case(rows, prompt, evidence, candidate, case, expected):
    """Verify rejected traces too; rejection never attests task correctness."""
    try:
        actual = read_recovery(rows, prompt, evidence, candidate)
    except (RuntimeError, ValueError, TypeError) as error:
        require(case.get("valid_trace") is False and case.get("passed") is False
                and case.get("report") is None and case.get("error") == str(error)
                and case["expected_decision"] == expected,
                "Invalid recovery trace was not preserved as a rejected case")
        return False
    expected_report = {**evidence["scope"], "incident_id": evidence["incident_id"], "decision": expected}
    require(case.get("valid_trace", True) is True
            and all(case[key] == value for key, value in actual.items())
            and case["expected_decision"] == expected
            and case["passed"] is (actual["report"] == expected_report),
            "Recovery gate differs from its SessionDB receipts")
    return case["passed"]


def audit_recovery_skill(root, report):
    first, second = report["rounds"]
    directory = Path("round-1/recovery-skill")
    sequence = read_json(root, directory / "sequence.json")
    review = read_json(root, directory / sequence["review"])
    source = directory / sequence["source"]
    proof = read_json(root, first["patrol_evidence"])["instance-a"]
    require(sequence == first["recovery_skill"] and read_json(root, source) == recovery_evidence(root / "round-1", proof)
            and review["source_sha256"] == digest(root, source) and review["native_review"]["completed"] is True
            and sequence["protected_skill_sha256"] == review["formal_sha256"] == first["active_sha256"],
            "Recovery Skill review is detached from the recorded recovery or protected Skill")
    if sequence["status"] == "keep":
        require(review["status"] == "keep" and "recovery_skill_adoption" not in second, "Keep cannot adopt a recovery Skill")
        return {"status": "pending", "decision": "keep"}
    candidate = sequence["candidate_sha256"]
    curator_path, gate_path = (directory / sequence[key] for key in ("curator", "gate"))
    curator, gate = read_json(root, curator_path), read_json(root, gate_path)
    retired = curator["retired_draft"]
    require(review["status"] == "proposed" and curator["status"] == "curated"
            and curator["candidate_sha256"] == review["candidate_sha256"] == gate["skill_sha256"] == candidate
            and digest(root, (directory / sequence["review"]).parent / review["candidate"])
            == digest(root, curator_path.parent / curator["candidate"]) == candidate
            and curator["formal_sha256"] == digest(root, curator_path.parent / "hermes/skills" / SKILL_FILE)
            == first["active_sha256"]
            and curator["native_transitions"]["archived"] == 1 and retired["state"] == "archived"
            and retired["recovery_verified"] is True and retired["listed_in_native_archive"] is True
            and retired["sha256"] == digest(root, curator_path.parent / retired["archive"])
            and curator["formal_protection"] == {"pinned": True, "state": "active", "source_unchanged": True,
                                                "copy_unchanged": True}, "Curator evidence does not preserve the protected Skill")
    gate_inputs = {case_id: (evidence, expected) for case_id, evidence, expected in gate_cases()}
    require(len(gate["cases"]) == len(gate_inputs) and {case["case_id"] for case in gate["cases"]} == gate_inputs.keys(),
            "Recovery Skill gate is missing its independent positive or negative cases")
    for case in gate["cases"]:
        evidence, expected = gate_inputs[case["case_id"]]
        rows, prompt, _ = session_turn(root, gate_path.parent / case["case_id"], case)
        audit_recovery_case(rows, prompt, evidence, candidate, case, expected)
    approved = all(case["passed"] for case in gate["cases"])
    require(sequence["status"] == ("approved" if approved else "rejected")
            and gate["status"] == ("passed" if approved else "rejected"), "Recovery Skill decision disagrees with its gate")
    if not approved:
        require("recovery_skill_adoption" not in second, "Rejected recovery Skill was adopted")
        return {"status": "pending", "decision": "rejected"}
    require(digest(root, sequence["approved_skill"]) == candidate, "Approved recovery Skill differs from the evaluated bytes")
    adoption_path = Path("round-2") / second["recovery_skill_adoption"]
    adoption = read_json(root, adoption_path)
    source = Path("round-2/recovery-skill/evidence.json")
    evidence = recovery_evidence(root / "round-2", read_json(root, second["patrol_evidence"])["instance-b"])
    case = adoption["case"]
    rows, prompt, _ = session_turn(root, adoption_path.parent / "adopted-incident", case)
    actual = read_recovery(rows, prompt, evidence, candidate)
    require(adoption["status"] == "adopted" and adoption["skill_sha256"] == candidate
            and adoption["source_sha256"] == digest(root, source) and evidence == read_json(root, source)
            and case["passed"] is True and all(case[key] == value for key, value in actual.items())
            and actual["report"] == {**evidence["scope"], "incident_id": evidence["incident_id"], "decision": "recovered"},
            "Fresh incident did not demonstrate the approved recovery Skill")
    return {"status": "passed", "gate_cases": len(gate_inputs), "adopted_sha256": candidate}


def audit_patrol_skill(root, directory, records, expected):
    db_path = run_file(root, directory / "hermes/state.db")
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        for record in records:
            session, item = record["session_id"], record["evidence_item_id"]
            prompt, = db.execute("SELECT MAX(id) FROM messages WHERE session_id=? AND role='user' AND id < ?",
                                 (session, item)).fetchone()
            rows = db.execute("""SELECT content FROM messages WHERE session_id=? AND role='tool'
                AND tool_name='patrol_skill_view' AND id > ? AND id < ?""", (session, prompt, item)).fetchall()
            require(rows, "Patrol must read the routine Skill before measuring")
            result = json.loads(rows[-1][0])
            require(result["success"] is True and result["name"] == PATROL_SKILL and result["sha256"] == expected
                    and sha256(result["content"].encode("utf-8")).hexdigest() == expected,
                    "Patrol Skill differs from the version that the fleet pinned")


def verify_monitor_alert(alert, incident, observation, injected_at):
    require(alert == {"incident_id": incident, "service": observation["service"],
                      "environment_id": observation["environment_id"], "source": ALERT_SOURCE,
                      "emitted_at": alert.get("emitted_at")}
            and type(alert["emitted_at"]) in (int, float)
            and injected_at <= alert["emitted_at"] <= observation["window_end"],
            "Monitor alert is outside the incident scope or the injection timeline")


def audit_patrol_loop(root, report):
    workers = report["patrol_workers"]
    require(workers.keys() == INSTANCES and len(set(workers.values())) == len(INSTANCES)
            and all(type(pid) is int and pid > 0 for pid in workers.values()), "Invalid fleet worker identities")
    last_recovery = dict.fromkeys(workers, 0)
    pages = 0
    for phase in report["rounds"]:
        number = phase["round"]
        loop_path = f"round-{number}/patrol-loop.json"
        require(phase["patrol_loop"] == loop_path, "Unexpected patrol loop evidence path")
        loops = read_json(root, loop_path)
        proofs = read_json(root, phase["patrol_evidence"])
        recovered = read_json(root, phase["recovery"])
        lifecycle = read_json(root, phase["incident_lifecycle"])
        require(loops.keys() == workers.keys(), "Patrol loop lacks both workers")
        for alias, loop in loops.items():
            directory = Path(f"round-{number}/collection") / alias
            branch = proofs[alias]["investigation"]
            incident = proofs[alias]["incident"]
            incident_id = incident["incident_id"]
            service, environment = branch["service"], branch["environment_id"]
            before, resumed, healthy = (loop[key] for key in ("before", "resumed", "recovered"))
            page = read_json(root, directory / "page.json")
            detected = read_json(root, directory / "detected.json")
            events = {event["kind"]: event["body"] for event in lifecycle["events"]
                      if event["incident"] == incident_id}
            require(detected == {"pid": workers[alias], "incident_id": incident_id}
                    and page["incident_id"] == incident_id and page["scope"] == f"{alias}/{environment}/{service}"
                    and page["channel"] == "local_oncall_inbox" and page["recipient"] == events["handoff"]["owner"]
                    and page["evidence"] == events["handoff"]["evidence"]
                    and page["session_id"] == branch["session_id"] and page["report_item_id"] == branch["report_item_id"]
                    and page["report"] == branch["report"] and page["report"]["analysis"].strip()
                    and page["report"]["recovery_plan"] == {"action": events["action_requested"]["action"],
                        "approval_required": True, "verification": "two_clean_windows"},
                    "The page does not carry the verified scoped investigation and recovery plan")
            observation = json.loads(incident["observation"])
            incident_time = observation["window_end"]
            if "alert_source" in report:
                alert = read_json(root, directory / "alert.json")
                verify_monitor_alert(alert, incident_id, observation, loop["injected_at"])
                delivery = incident["steer_delivery"].split(STEER_PREFIX, 1)[1].splitlines()[0]
                require(report["alert_source"] == ALERT_SOURCE and json.loads(delivery) == alert,
                        "Hermes steer does not carry the original monitoring event")
            require(last_recovery[alias] <= before["checked_at"] < loop["injected_at"] <= incident_time
                    <= branch["probes"][-1]["checked_at"] <= page["paged_at"] < resumed["checked_at"]
                    <= events["action_executed"]["effect"]["cleared_at"] < recovered[alias]["window_end"]
                    < healthy["checked_at"] and resumed["session_id"] == healthy["session_id"] == incident["session_id"],
                    "Patrol did not resume on the same worker after paging and before recovery")
            for path in (directory / "patrol.sqlite", Path("patrols") / alias / "patrol.sqlite"):
                db_path = run_file(root, path)
                with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                    db.row_factory = sqlite3.Row
                    for record in (before, resumed, healthy):
                        row = db.execute("SELECT * FROM inspections WHERE id = ?", (record["id"],)).fetchone()
                        observation = json.loads(record["observation"])
                        require(row is not None and dict(row) == record and record["service"] == service
                                and observation["environment_id"] == environment
                                and record["status"] == classify(observation), "Patrol checkpoint differs from SQLite")
                    closed = db.execute("SELECT recovered_inspection_id FROM incidents WHERE incident_id = ?",
                                        (incident_id,)).fetchone()
                    require(closed is not None and closed[0] == healthy["id"] and before["status"] == "healthy"
                            and healthy["status"] == "healthy", "Patrol state does not link the closed incident to recovery")
            if "patrol_skill_sha256" in report:
                audit_patrol_skill(root, directory, (before, incident, resumed, healthy), report["patrol_skill_sha256"])
            last_recovery[alias] = healthy["checked_at"]
            pages += 1
    result = {"status": "passed", "workers": len(workers), "pages": pages}
    if "alert_source" in report:
        result["monitor_alerts"] = pages
    return result


def audit_core(root, report):
    """Return named read-only checks for the completed two-round fleet."""
    root = root.resolve(strict=True)
    require(len(report["rounds"]) == 2, "Fleet audit requires the two course rounds")
    workset = build_workset(root, report)
    sessions = {case["provenance"][key] for case in workset["cases"]
                for key in ("patrol_session_id", "investigation_session_id")}
    require(len(workset["cases"]) == 4 and len(sessions) == 8
            and report["http_workset"] == "http-workset.json" and read_json(root, report["http_workset"]) == workset,
            "HTTP working examples differ from the scoped incident, SessionDB and recovery records")
    try:
        release, rollback = audit_skillclaw(root, report, workset)
        checks = {
            "patrol_and_recovery": {"status": "passed", "rounds": 2, "incidents": len(workset["cases"])},
            "skillclaw_release": release,
            "peer_handoff": audit_handoffs(root, report, workset),
            "diagnostic_memory": audit_diagnostic_memory(root, report),
            "recovery_skill": audit_recovery_skill(root, report),
            "rollback": rollback,
        }
        if "patrol_workers" in report or any("patrol_loop" in phase for phase in report["rounds"]):
            checks["continuous_patrol"] = audit_patrol_loop(root, report)
        if "implementation" in report:
            checks["implementation"] = audit_implementation(root, report)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    return checks


def audit_implementation(root, report):
    """Reconstruct the chosen source and hook evidence without importing its code."""
    from implementation import implementation_evidence, verify_stage_check

    declared = report["implementation"]
    snapshot = run_file(root, declared["snapshot"])
    binding = read_json(root, snapshot.parent.relative_to(root) / "binding.json")
    if "stage_check" in declared:
        check = run_file(root, snapshot.parent.relative_to(root) / "stage-check.json")
        binding["stage_check"] = verify_stage_check(check, binding["sha256"], live=report["mode"] == "live")
    reconstructed = implementation_evidence(snapshot, binding)
    require(reconstructed == declared, "Selected implementation differs from its saved source or hook receipts")
    verified = (report["status"] == "complete" and reconstructed["all_hooks_executed"]
                and any(phase.get("decision") == "publish" for phase in report["rounds"])
                and any(phase.get("adoption_verified") is True for phase in report["rounds"]))
    require(report.get("closed_loop_verified") is verified,
            "Closed-loop claim differs from the selected implementation and publication/adoption evidence")
    return {"status": "passed", "sha256": reconstructed["sha256"],
            "executed_hooks": reconstructed["executed_hooks"], "closed_loop_verified": verified,
            "stage_check_verified": "stage_check" in reconstructed}
