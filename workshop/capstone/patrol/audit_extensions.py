"""Read-only checks of optional fleet evidence and separately exported artifacts."""

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from enterprise_replay import lesson_module, read_session
from export_workset import recorded_probes, require, run_file, session_rows, verify_skill
from fact_handoff import check_openviking_memory
from gepa_cases import split_manifest
from openviking_handoff import fact_body, fact_uri, runbook_body, runbook_uri
from patrol import decode_report


def read_json(root, relative):
    return json.loads(run_file(root, relative).read_text(encoding="utf-8"))


def file_sha(root, relative):
    return sha256(run_file(root, relative).read_bytes()).hexdigest()


def checkpointed_db(root, relative):
    path = run_file(root, relative)
    wal = path.with_name(path.name + "-wal")
    require(not wal.exists() or wal.stat().st_size == 0, "Evidence SessionDB has an uncheckpointed WAL")
    return path


def immutable_db(root, relative):
    path = checkpointed_db(root, relative)
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return closing(db)


def check(status, **evidence):
    return {"status": status, **evidence}


def unique(rows, key):
    indexed = {row[key]: row for row in rows}
    require(len(indexed) == len(rows), f"Duplicate {key} in audit evidence")
    return indexed


def audit_http_trace(root, directory, fixture, evidence, skill):
    if evidence["valid_trace"] is False:
        require(evidence["passed"] is False, "An invalid HTTP replay cannot pass")
        return
    require(evidence["valid_trace"] is True and evidence["skill_sha256"] == skill,
            "HTTP replay used a different Skill")
    item, session = evidence["report_item_id"], evidence["session_id"]
    with immutable_db(root, f"{directory}/hermes/state.db") as db:
        rows = session_rows(db, session, 1, item)
        parent = db.execute("SELECT parent_session_id FROM sessions WHERE id=?", (session,)).fetchone()
    require(parent is not None and parent[0] is None, "HTTP replay did not use a fresh SessionDB session")
    skill_item = evidence["skill_item_id"]
    verify_skill(rows[skill_item], skill)
    probes = [{**probe["receipt"], "item_id": probe["item_id"], "call_item_id": probe["call_item_id"]}
              for probe in evidence["probes"]]
    snapshot = fixture["snapshot"]
    metrics = [json.loads(row["content"]) for row in rows.values()
               if row["role"] == "tool" and row["tool_name"] == "service_snapshot"]
    require(len(metrics) == 1 and all(metrics[0][key] == value for key, value in snapshot.items()),
            "HTTP replay used a different frozen snapshot")
    scope = {key: snapshot[key] for key in ("service", "environment_id")}
    observed = recorded_probes(rows, probes, "oncall_probe", scope, skill_item, item)
    report = decode_report(rows[item]["content"])
    expected = fixture["probe_http_statuses"]
    passed = observed == expected and evidence["assessment"] == fixture["assessment"]
    require(rows[item]["role"] == "assistant" and report["assessment"] == evidence["assessment"]
            and report["probe_http_statuses"] == observed and report["probe_http_status"] == expected[0]
            and (passed or (len(expected) == 2 and observed == expected[:1]
                            and evidence["assessment"] == "failure_observed"))
            and evidence["passed"] is passed,
            "HTTP replay conclusion disagrees with its tool Items and frozen case")


def audit_http_gate(root, case_fixtures, baseline_sha, selected_sha):
    evaluation = read_json(root, "evaluation.json")
    variants = {"baseline": baseline_sha, "candidate": selected_sha}
    cases = unique(evaluation["cases"], "case")
    require(cases.keys() == case_fixtures.keys(), "HTTP replay gate contains a different frozen case set")
    for name, skill in variants.items():
        require(evaluation[f"{name}_sha256"] == file_sha(root, f"{name}/SKILL.md") == skill,
                "HTTP replay gate changed the pinned Skill")
        for case, evidence in cases.items():
            require(evidence["fixture"] == case_fixtures[case], "HTTP replay gate fixture changed")
            audit_http_trace(root, f"{name}/{case}", case_fixtures[case], evidence[name], skill)
    valid = all(case["baseline"]["valid_trace"] for case in cases.values())
    passed = all(case["candidate"]["passed"] for case in cases.values())
    improved = [name for name, case in cases.items() if case["candidate"]["passed"] and not case["baseline"]["passed"]]
    require(evaluation["baseline_comparable"] is valid and evaluation["candidate_passed_all"] is passed
            and evaluation["improved"] == improved
            and evaluation["passed"] is (valid and passed and bool(improved) and baseline_sha != selected_sha),
            "HTTP replay gate summary disagrees with its cases")
    return evaluation


def audit_gepa(root, round_data):
    relative = round_data.get("gepa")
    if relative is None:
        return check("not_requested")
    path = run_file(root, relative)
    base = path.parent
    report = read_json(root, relative)
    search = read_json(base, "search/report.json")
    manifest = split_manifest()
    baseline = round_data["active_sha256"]
    selected = search["selected_sha256"]
    require(report["role"] == "offline_comparison" and report["release_authority"] == "skillclaw"
            and search == report["search"] and report["splits"] == manifest
            and search["gepa_version"] == "0.0.27" and report["seed_sha256"] == search["baseline_sha256"] == baseline,
            "GEPA experiment changed its seed, fixed splits or search report")
    fixtures = {role: {entry["case"]: {key: entry[key] for key in ("snapshot", "probe_http_statuses", "assessment")}
                       for entry in cases} for role, cases in manifest["splits"].items()}
    train, validation = fixtures["train"], fixtures["validation"]
    budget = 2 * (len(train) + len(validation))
    calls = search["hermes_sessions"]
    proposals = search["proposal_calls"]
    require(type(calls) is int and 0 < calls <= budget
            and search["max_metric_calls"] == search["max_hermes_sessions"] == budget
            and search["total_metric_calls"] == calls and search["max_proposals"] == 1
            and type(proposals) is int and 0 <= proposals <= 1,
            "GEPA search exceeded its declared budget")
    candidates = unique(search["candidates"], "sha256")
    require(1 <= len(candidates) <= proposals + 1
            and search["candidates"][0]["sha256"] == baseline
            and all(candidate["index"] == index and candidate["parent_indices"] == [0 if index else None]
                    for index, candidate in enumerate(search["candidates"])),
            "GEPA candidate lineage disagrees with the bounded search")
    rollouts, scores = {}, {}
    for item in sorted((base / "search/rollouts").glob("*/result.json")):
        artifact = item.relative_to(base / "search")
        trace = read_json(base, Path("search") / artifact)
        case, role, skill = trace["case"], trace["split"], trace["candidate_sha256"]
        require(role in ("train", "validation") and case in fixtures[role]
                and trace["artifact_dir"] == str(artifact.parent),
                "GEPA searched a release or holdout case, or crossed its rollout path")
        fixture = fixtures[role][case]
        result = trace["result"]
        require(result["valid_trace"] is True and trace["service"] == fixture["snapshot"]["service"]
                and trace["expected_probe_count"] == len(fixture["probe_http_statuses"]),
                "GEPA rollout differs from its frozen case")
        require(file_sha(base, f"search/skills/{skill}/SKILL.md") == skill, "GEPA rollout Skill changed")
        audit_http_trace(base, str(Path("search") / artifact.parent), fixture, result, skill)
        rollouts[trace["artifact_dir"]] = trace
        if role == "validation":
            require((skill, case) not in scores, "Duplicate GEPA validation rollout")
            scores[skill, case] = float(result["passed"])
    require(len(rollouts) == calls and len({trace["result"]["session_id"] for trace in rollouts.values()}) == calls,
            "GEPA search budget differs from the saved replay sessions")
    for skill, candidate in candidates.items():
        expected = {case: scores[skill, case] for case in validation}
        require(candidate["validation_case_scores"] == expected
                and candidate["validation_score"] == sum(expected.values()) / len(expected),
                "GEPA validation score disagrees with the actual frozen replays")
    best = max(candidate["validation_score"] for candidate in candidates.values())
    initial = candidates[baseline]["validation_score"]
    require((search["baseline_validation_score"], search["best_validation_score"], search["validation_gain"])
            == (initial, best, best - initial), "GEPA selection scores disagree with the candidate table")
    mode = "model" if search["proposal_source"] == "official_gepa_reflection" else "scripted"
    require(report.get("reflection_mode", "scripted") == mode, "GEPA report describes a different reflection mode")
    audit_gepa_reflection(base, search, rollouts)
    if selected is None:
        require(report["status"] == "no_validation_gain" and best == initial
                and not any(role in report for role in ("selected", "release", "holdout")),
                "GEPA used a candidate without independent validation gain")
        return check("passed", decision="no_validation_gain", search_sessions=calls, reflection=mode)
    require(report["status"] == "completed" and selected != baseline and best > initial
            and candidates[selected]["validation_score"] == best
            and report["selected"]["sha256"] == file_sha(base, report["selected"]["file"]) == selected,
            "GEPA selected Skill differs from the best validation candidate")
    gates = {}
    for role in ("release", "holdout"):
        summary = report[role]
        require(summary["file"] == f"{role}/evaluation.json", "GEPA gate points to a different split")
        gate = audit_http_gate(run_file(base, summary["file"]).parent, fixtures[role], baseline, selected)
        require(summary["gate_passed"] is gate["passed"] and summary["cases"] == len(gate["cases"])
                and all(summary[f"{version}_passed"] == sum(case[version]["passed"] for case in gate["cases"])
                        for version in ("baseline", "candidate")),
                "GEPA gate summary disagrees with its case evidence")
        gates[role] = gate["passed"]
    return check("passed", decision="candidate_evaluated", search_sessions=calls, gates=gates, reflection=mode)


def audit_gepa_reflection(root, search, rollouts):
    source, proposals = search["proposal_source"], search["proposal_calls"]
    material = None
    if source == "scripted_training_receipts":
        if proposals:
            proposal = read_json(root, "search/proposal.json")
            proposed_sha = proposal["proposed_sha256"]
            require(proposal["source"] == source
                    and file_sha(root, f"search/skills/{proposed_sha}/SKILL.md") == proposed_sha
                    and proposal["changed"] is (proposed_sha != search["baseline_sha256"])
                    and search["selected_sha256"] in (None, proposed_sha),
                    "GEPA scripted reflection source or proposal changed")
            material = proposal["training_reflection"]
    else:
        require(source == "official_gepa_reflection", "Unknown GEPA reflection source")
        reflection = search["reflection"]
        require(reflection["requests"] == proposals <= reflection["request_limit"] == 1
                and len(reflection["receipts"]) == proposals,
                "GEPA model reflection exceeded its request budget")
        if proposals:
            require(reflection["receipts"] == ["reflection/001.json"]
                    and reflection["dataset"] == "training-reflection.json",
                    "GEPA model reflection receipt paths changed")
            material = read_json(root, "search/training-reflection.json")
            receipt = read_json(root, "search/reflection/001.json")
            choice = receipt["response"]["choices"][0]
            require(receipt["model"] == reflection["model"]
                    and choice["finish_reason"] == "stop" and choice["message"]["content"].strip(),
                    "GEPA model reflection lacks a completed request and response")
            from gepa_search import require_gepa, split_skill

            require_gepa()
            from gepa.strategies.instruction_proposal import InstructionProposalSignature

            seed_path = run_file(root, f"search/skills/{search['baseline_sha256']}/SKILL.md")
            header, body = split_skill(seed_path)
            rendered = InstructionProposalSignature.prompt_renderer({
                "current_instruction_doc": body, "dataset_with_feedback": material["skill_body"],
            })
            require(receipt["messages"] == [{"role": "user", "content": rendered}],
                    "GEPA model request differs from the official rendering of training-only evidence")
            candidate = InstructionProposalSignature.output_extractor(choice["message"]["content"])["new_instruction"]
            proposed_sha = sha256((header + candidate).encode()).hexdigest()
            require(file_sha(root, f"search/skills/{proposed_sha}/SKILL.md") == proposed_sha
                    and search["selected_sha256"] in (None, proposed_sha),
                    "GEPA model response does not produce the recorded proposal")
    if material is None:
        return
    require(set(material) == {"skill_body"} and material["skill_body"],
            "GEPA reflection has no training material")
    seen = set()
    for row in material["skill_body"]:
        reference = row["rollout"]
        trace = rollouts[reference]
        result = trace["result"]
        require(reference not in seen and trace["split"] == "train"
                and row["Inputs"] == {"case": trace["case"], "service": trace["service"]}
                and row["Generated Outputs"] == {
                    "assessment": result["assessment"],
                    "http_statuses": [probe["receipt"]["http_status"] for probe in result["probes"]],
                }
                and row["Feedback"] == {"score": float(result["passed"]),
                                        "expected_probe_count": trace["expected_probe_count"]},
                "GEPA reflection contains material outside its training replay evidence")
        seen.add(reference)


def audit_openviking(root, report):
    external = report.get("openviking")
    if external is None:
        return check("not_requested")
    rounds = report["rounds"]
    namespace = external["namespace"]
    services = {source["evidence"]["service"]: alias for alias, source in rounds[0]["fact_reviews"].items()}
    require(external["runbooks"].keys() == services.keys(), "OpenViking runbook coverage differs from the fleet")
    for service, alias in services.items():
        require(external["runbooks"][service] == {
            "uri": runbook_uri(namespace, service), "publisher": alias,
            "sha256": sha256(runbook_body(service, alias).encode()).hexdigest(),
        }, "OpenViking runbook publication has different bytes or source")
    for round_data in rounds:
        for alias, source in round_data["fact_reviews"].items():
            fact = source["evidence"]
            expected = ({
                "uri": fact_uri(namespace, fact["incident_id"], fact["environment_id"]),
                "publisher": alias, "sha256": sha256(fact_body(fact).encode()).hexdigest(),
            } if source["status"] == "proposed" else None)
            require(source.get("openviking") == expected, "OpenViking fact publication differs from reviewed evidence")
    first, second = rounds
    require(second["fact_handoff"].keys() == first["fact_reviews"].keys(),
            "OpenViking handoffs do not cover both peers")
    for receiver, handoff in second["fact_handoff"].items():
        source = handoff["source_instance"]
        require(source != receiver and handoff["backend"] == "openviking", "OpenViking lookup used a different peer")
        prior = first["fact_reviews"][source]
        fact = prior["evidence"]
        service = fact["service"]
        relative = Path("round-2") / handoff["receipt"]
        result = read_json(root, relative)
        memory = result["memory_receipt"]
        scope = {key: fact[key] for key in ("incident_id", "service", "environment_id")}
        try:
            check_openviking_memory(memory, namespace, receiver, scope)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        expected = prior.get("openviking") or {"uri": None, "sha256": None}
        runbook = external["runbooks"][service]
        require(result["scope"] == scope and memory["runbook"] == runbook_body(service, source)
                and memory["fact"] == (fact if expected["uri"] else None)
                and (memory["runbook_sha256"], memory["fact_sha256"]) == (runbook["sha256"], expected["sha256"])
                and (handoff["runbook_uri"], handoff["fact_uri"]) == (runbook["uri"], expected["uri"])
                and (result["report"]["runbook_uri"], result["report"]["fact_uri"]) == (runbook["uri"], expected["uri"]),
                "OpenViking citation differs from this run's publication or the final report")
    return check("passed", namespace=namespace, peer_handoffs=len(second["fact_handoff"]))


def audit_enterprise_pack(root, report):
    from enterprise_langfuse import digest, prepare, verify

    entry = report["enterprise"]
    pack = read_json(root, entry["pack"])
    verify(pack)
    require(entry["ready"] == len(pack["ready"]) and entry["pending"] == len(pack["pending"]),
            "Enterprise pack counts disagree with the fleet report")
    source = read_json(root, "enterprise/rollouts.json")
    annotations = read_json(root, "enterprise/annotations.json")
    require(pack["versions"]["source_sha256"] == file_sha(root, "enterprise/rollouts.json")
            and pack["versions"]["annotations_sha256"] == file_sha(root, "enterprise/annotations.json"),
            "Enterprise source Rollouts or annotations changed")
    traces = unique(source["traces"], "id")
    expected_traces, snapshots = set(), {}
    for round_data in report["rounds"]:
        index = round_data["round"]
        path = f"enterprise/round-{index}"
        capture = read_json(root, f"{path}/capture.json")
        capture_sha = capture.pop("sha256")
        recovery = read_json(root, f"{path}/recovery-evidence.json")
        require(capture_sha == digest(capture)
                and capture["plan_sha256"] == file_sha(root, f"{path}/fault-plan.json")
                and capture["source_sha256"] == file_sha(root, round_data["patrol_evidence"])
                and recovery.pop("sha256") == digest(recovery)
                and recovery["source_sha256"] == file_sha(root, f"round-{index}/incident-lifecycle.json")
                and recovery["capture_sha256"] == capture_sha,
                "Enterprise capture or recovery does not match its original evidence")
        proofs = read_json(root, round_data["patrol_evidence"])
        require(proofs.keys() == capture["cases"].keys(), "Enterprise capture lost a fleet case")
        for alias, proof in proofs.items():
            incident, branch = proof["incident"], proof["investigation"]
            incident_id = incident["incident_id"]
            captured = capture["cases"][alias]
            require(captured["incident_id"] == incident_id, "Enterprise capture changed the incident scope")
            if captured["snapshot"] is not None:
                snapshot = read_json(root, f"enterprise/snapshots/{incident_id}.json")
                require(snapshot == captured["snapshot"] and incident_id not in snapshots,
                        "Enterprise snapshot changed or was reused across splits")
                snapshots[incident_id] = snapshot
            for turn in (incident, branch):
                key = f"session:{turn['session_id']}:item:{turn['report_item_id']}"
                meta = traces[key]["metadata"]
                require(key not in expected_traces and meta["incident_id"] == meta["snapshot_id"] == incident_id
                        and meta["cluster"] == alias and meta["split"] == {1: "dev", 2: "holdout"}[index]
                        and meta["skill_version"] == incident["skill_sha256"],
                        "Enterprise source Rollout changed the run, split, or Skill")
                expected_traces.add(key)
    reconstructed = prepare(source, annotations, snapshots)
    require(expected_traces == traces.keys()
            and all(reconstructed[key] == pack[key] for key in ("ready", "pending")),
            "Enterprise prepared cases differ from the frozen source artifacts")
    return pack


def audit_enterprise_replay(root, pack):
    root = root.resolve(strict=True)
    report = read_json(root, "report.json")
    require(report["pack_sha256"] == pack["pack_sha256"]
            and report["scorer_version"] == pack["versions"]["scorer_version"],
            "Enterprise replay used a different case pack or scorer")
    cases = unique(pack["ready"], "id")
    grader, monitor = lesson_module("grader"), lesson_module("monitor")
    protocol = lesson_module("worker")
    sessions = set()
    for split in ("dev", "holdout"):
        pairs = unique(report[split], "case")
        expected = {key for key, case in cases.items() if case["split"] == split}
        ran_split = (report["status"] != "insufficient_ready_cases" if split == "dev"
                     else report["selection"] == "candidate")
        require(pairs.keys() == (expected if ran_split else set()), "Enterprise replay changed its case split")
        for case_id, pair in pairs.items():
            case = cases[case_id]
            for variant in ("baseline", "candidate"):
                summary = pair[variant]
                relative = f"{split}/{case_id}/{variant}/evaluation.json"
                require(summary["file"] == relative, "Enterprise replay points outside its case and variant")
                result = read_json(root, relative)
                require(summary["grade"] == result["grade"] and summary["session_id"] == result.get("session_id"),
                        "Enterprise replay summary disagrees with its saved evaluation")
                if result["status"] == "completed":
                    state = run_file(root, relative).parent
                    skill = run_file(state, "SKILL.md").read_bytes()
                    require(read_json(state, "task.json") == case["input"]
                            and read_json(state, "snapshot.json") == case["snapshot"]
                            and result["skill_sha256"] == report["skill_sha256"][variant] == sha256(skill).hexdigest(),
                            "Enterprise replay changed its frozen input or Skill")
                    checkpointed_db(state, "hermes/state.db")
                    try:
                        persisted = read_session(state, result, case["input"]["prompt"] + "\n\n" + protocol.OUTPUT_INSTRUCTION,
                                                 case["snapshot"], skill, monitor.query)
                    except RuntimeError as exc:
                        raise ValueError(str(exc)) from exc
                    require(all(result[key] == value for key, value in persisted.items())
                            and result["session_id"] not in sessions, "Enterprise replay tool Items or session changed")
                    sessions.add(result["session_id"])
                require(grader.grade(case, result) == result["grade"],
                        "Enterprise case grade disagrees with the frozen case and replay")
    status = report["status"]
    require(status in {"completed", "unscored", "insufficient_ready_cases"},
            "Enterprise replay has no completed or explicit pending result")
    if status != "completed":
        return check("pending", reason=status, sessions=len(sessions))
    require(all(pair[variant]["grade"]["status"] == "scored" for split in ("dev", "holdout")
                for pair in report[split] for variant in ("baseline", "candidate")),
            "Completed enterprise replay contains unscored cases")
    improved = (all(pair["candidate"]["grade"]["passed"] for pair in report["dev"])
                and any(not pair["baseline"]["grade"]["passed"] for pair in report["dev"]))
    require(report["selection"] == ("candidate" if improved else "baseline")
            and report["holdout_passed"] == (all(pair["candidate"]["grade"]["passed"] for pair in report["holdout"])
                                              if improved else None),
            "Enterprise selection or holdout decision differs from the saved grades")
    return check("passed", sessions=len(sessions), selection=report["selection"], holdout_passed=report["holdout_passed"])


def audit_langfuse(root, artifacts):
    from enterprise_langfuse import comparable_cases, confirm_trace, digest, local_sources, map_tasks, prepare, verify

    artifacts = artifacts.resolve(strict=True)
    pack, rollouts, turns, annotations, snapshots = local_sources(root)
    report = read_json(artifacts, "report.json")
    index = read_json(artifacts, "trace-index.json")
    source = read_json(artifacts, "collected.json")
    native = read_json(artifacts, "prepared/pack.json")
    verify(native)
    traces = unique(source["traces"], "id")
    # The pinned SDK uses the first 16 bytes of SHA-256 for seeded IDs.
    expected = {
        sha256(f"{turn['session_id']}::{turn['task_id']}".encode()).hexdigest()[:32]: (key, turn)
        for key, turn in turns.items()
    }
    require(index.keys() == traces.keys() == expected.keys() and len(expected) == len(turns)
            and report["native_traces"] == len(expected),
            "Langfuse trace index does not cover this fleet's exact sessions and tasks")
    tasks = {}
    for trace_id, (key, turn) in expected.items():
        fields = ("session_id", "task_id", "user_item_id", "report_item_id", "role", "round", "instance", "incident_id")
        require(index[trace_id] == {"source": key, **{field: turn[field] for field in fields},
                                    **confirm_trace(traces[trace_id], turn)},
                "Langfuse trace index disagrees with the native trace and SessionDB")
        tasks[trace_id] = {field: rollouts[key][field] for field in ("input", "metadata")}
    mapped = read_json(artifacts, "mapped.json")
    require(mapped == map_tasks(source, tasks) and read_json(artifacts, "annotations.json") == annotations
            and all(read_json(artifacts, f"snapshots/{key}.json") == value for key, value in snapshots.items()),
            "Langfuse mapping changed the task source, snapshot, or independent annotation")
    reconstructed = prepare(mapped, annotations, snapshots)
    require(all(reconstructed[key] == native[key] for key in ("ready", "pending"))
            and native["versions"]["source_sha256"] == file_sha(artifacts, "mapped.json")
            and native["versions"]["annotations_sha256"] == file_sha(artifacts, "annotations.json")
            and comparable_cases(native) == comparable_cases(pack)
            and report["pack_sha256"] == native["pack_sha256"]
            and report["ready"] == len(native["ready"]) and report["pending"] == len(native["pending"]),
            "Native Langfuse pack changed the fleet's labelled cases")
    if report["status"] != "roundtrip_complete":
        require(report["status"] in {"verified", "failed", "publishing", "reading_back"},
                "Langfuse report does not record a verified or pending roundtrip")
        return check("pending", reason="dataset_roundtrip_unverified", native_traces=len(index)), None

    exported = read_json(artifacts, "exported.json")
    published = read_json(artifacts, "published.json")
    restored = read_json(artifacts, "restored/pack.json")
    verify(restored)
    sent = unique(published["items"], "id")
    received = unique(exported["items"], "id")
    require(exported["dataset_name"] == published["dataset_name"] == report["dataset"]
            and exported["requested_version"] == published["export_version_hint"]
            == report["dataset_version"] == restored["versions"]["dataset_version"]
            and restored["versions"]["export_sha256"] == digest(exported)
            and sent == received and not restored["pending"]
            and comparable_cases(restored) == comparable_cases(native),
            "Langfuse read-back differs from the published dataset version")
    items = {item["metadata"]["case_id"]: item for item in received.values()}
    native_cases = unique(native["ready"], "id")
    require(len(items) == len(received) and items.keys() == native_cases.keys(),
            "Langfuse dataset items do not map one-to-one to the native cases")
    for case in restored["ready"]:
        case_id = case["id"]
        item, original = items[case_id], native_cases[case_id]
        metadata, provenance = item["metadata"], case["provenance"]
        require(metadata["provenance"] == original["provenance"] and metadata["versions"] == native["versions"]
                and metadata["pack_sha256"] == native["pack_sha256"] and metadata["annotation"] == case["annotation"]
                and metadata["snapshot_id"] == case["snapshot"]["snapshot_id"]
                and metadata["snapshot_version"] == case["snapshot"]["version"]
                and metadata["snapshot_sha256"] == digest(case["snapshot"])
                and metadata["split"] == case["split"] and metadata["scenario"] == case["scenario"]
                and item["input"] == case["input"] and item["expected_output"] == case["annotation"]["expected_output"]
                and item["source_trace_id"] in original["provenance"]["trace_ids"]
                and set(original["provenance"]["trace_ids"]) <= index.keys()
                and provenance == {
                    **original["provenance"], "dataset_name": report["dataset"],
                    "dataset_id": exported["dataset_id"], "project_id": exported["project_id"],
                    "dataset_version": report["dataset_version"], "dataset_item_id": item["id"],
                    "source_trace_id": item["source_trace_id"], "source_observation_id": item.get("source_observation_id"),
                }
                and case["imported_versions"] == native["versions"],
                "Restored case changed its source Trace, immutable dataset item, or version")
    return check("passed", native_traces=len(index), dataset_version=report["dataset_version"]), restored


def audit_extensions(root, report, *, langfuse=None, replay=None):
    require(replay is None or langfuse is not None, "Restored replay requires its Langfuse export")
    rounds = report["rounds"]
    require(rounds[0].get("gepa") in (None, "round-1/gepa/report.json")
            and all(round_data.get("gepa") is None for round_data in rounds[1:]),
            "GEPA comparison is outside its fleet round")
    checks = {
        "openviking": audit_openviking(root, report),
        "gepa": audit_gepa(root, rounds[0]),
        "enterprise": check("not_requested"),
        "langfuse": check("not_requested"),
        "restored_replay": check("not_requested"),
    }
    if report.get("enterprise") is not None:
        pack = audit_enterprise_pack(root, report)
        evaluation = run_file(root, report["enterprise"]["evaluation"])
        require(read_json(evaluation.parent, "report.json")["status"] == report["enterprise"]["status"],
                "Enterprise evaluation status differs from the fleet report")
        checks["enterprise"] = audit_enterprise_replay(evaluation.parent, pack)
        if report.get("enterprise_cases") is not None:
            from enterprise_cases import audit as audit_enterprise_cases

            checks["enterprise_cases"] = audit_enterprise_cases(root, report["enterprise_cases"], pack, report["mode"])
        else:
            require(not (root / "enterprise/case-boundaries").exists()
                    and not any((root / f"enterprise/round-{index}/same-cluster-distractors.json").exists()
                                for index in (1, 2)), "Fleet report omitted the boundary evaluation")
    else:
        require(report.get("enterprise_cases") is None, "Boundary evaluation requires the source enterprise pack")
    if langfuse is not None:
        require(report.get("langfuse") is not None, "Langfuse evidence was supplied for a run without capture")
        checks["langfuse"], restored = audit_langfuse(root, langfuse)
        if replay is not None:
            require(restored is not None, "Restored replay requires the verified dataset roundtrip")
            checks["restored_replay"] = audit_enterprise_replay(replay, restored)
        elif restored is not None:
            checks["restored_replay"] = check("pending", reason="restored_pack_replay_not_supplied")
    elif report.get("langfuse") is not None:
        checks["langfuse"] = check("pending", reason="trace_and_dataset_export_not_supplied")
    if report.get("langfuse") is not None:
        checks["langfuse_deployment"] = check("pending", reason="local_artifacts_do_not_attest_the_deployed_service")
    if any(checks[name]["status"] != "not_requested" for name in ("gepa", "enterprise")):
        checks["model_effectiveness"] = check("pending", reason="separate_real_model_outcome_evidence_required")
    return checks
