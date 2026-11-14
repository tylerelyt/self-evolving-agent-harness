"""Run the On-call workflow from patrol evidence to verified Skill adoption."""

from dataclasses import dataclass
from pathlib import Path
import json

from oncall_runtime import (
    ONCALL_SKILL, TARGETS, process, adopt_recovery_skill, capture_distractors, collect_patrols,
    evaluate, evolve, finish_patrols, get_snapshot, handoff_facts, investigate_round,
    load_json, newer_checkpoint, prepare_recovery_skill, recover, release,
    release_allowed, revise_diagnostic_memory, review_facts, seal_distractors,
    set_faults, sha256, start_enterprise_faults, verify_adoption, wait_for_patrols,
    write_json,
)
from hermes_runtime import fork as branch_report
from investigation import page_operator as notify_operator
from patrol import steer_oncall as route_alert

release_policy = release_allowed


def patrol_config():
    return {"services": ["orders", "payments", "inventory", "notifications"], "sample": 2, "interval": 1}


def recall_memory(state, memory, environment_state, scope, options):
    from fact_handoff import handoff

    return handoff(state, memory, environment_state, scope, options)


@dataclass
class RoundTask:
    number: int
    iteration: Path
    result: dict
    sources: list[Path]
    before: str


def run_once(runtime, index, faults):
    if runtime.enterprise:
        from enterprise_pack import capture, finish_round

    iteration = runtime.root / f"round-{index}"
    iteration.mkdir()
    handoff = None
    route_revision = None
    if runtime.prior_facts is not None:
        runtime.step(f"Round {index}: recall the other instance's historical fact and check current HTTP metrics")
        handoff = handoff_facts(iteration, runtime.environments, runtime.prior_facts, runtime.shared, runtime.live,
                               openviking=runtime.viking, implementation_file=runtime.implementation_file)
    if index == 2:
        runtime.step(f"Round {index}: verify a diagnostic route migration, revise Memory and adopt it in a fresh session")
        route_revision = revise_diagnostic_memory(iteration, runtime.environments["instance-a"],
                                                 runtime.prior_facts["instance-a"]["evidence"], runtime.live)
    ready = wait_for_patrols(runtime.workers, lambda alias, state: newer_checkpoint(
        state, TARGETS[alias], runtime.report["healthy_baseline"][alias]["window_end"],
        runtime.report["healthy_baseline"][alias]["environment_id"], healthy=True,
    ))
    runtime.step(f"Round {index}: inject faults while both Hermes patrols are running")
    enterprise_round = runtime.root / "enterprise" / f"round-{index}"

    def inject():
        if not runtime.enterprise:
            return set_faults(runtime.environments, faults, cleanup=runtime.stack)
        plan = start_enterprise_faults(
            enterprise_round, index, runtime.environments, runtime.report["healthy_baseline"], faults, cleanup=runtime.stack,
        )
        return {alias: target["injected_at"] for alias, target in plan["targets"].items()}

    proof, sources, injected_at = collect_patrols(
        iteration / "collection", runtime.environments, runtime.workers, runtime.receivers, sha256(runtime.baseline), runtime.options, inject,
    )
    adoption_verified = runtime.previous is not None
    if adoption_verified:
        verify_adoption(proof, faults)
    before = sha256(runtime.baseline)
    result = {
        "round": index, "active_sha256": before, "decision": "pending",
        "patrol_evidence": str((iteration / "collection" / "patrol-evidence.json").relative_to(runtime.root)),
        "incident_lifecycle": str((iteration / "incident-lifecycle.json").relative_to(runtime.root)),
        "source_sessions": {alias: entry["incident"]["session_id"] for alias, entry in proof.items()},
        "adoption_verified": adoption_verified,
    }
    if handoff is not None:
        result["fact_handoff"] = handoff
    if route_revision is not None:
        result["route_revision"] = route_revision
    runtime.report["rounds"].append(result)
    write_json(runtime.report_path, runtime.report)
    runtime.step(f"Round {index}: fork both saved reports and collect scoped follow-up evidence")
    investigate_round(iteration / "collection", runtime.environments, proof, runtime.workers, runtime.live, langfuse=runtime.langfuse,
                      implementation_file=runtime.implementation_file)
    if runtime.enterprise:
        plan = load_json(enterprise_round / "fault-plan.json")
        runtime.step(f"Round {index}: freeze live dependency spans, logs and metrics before clearing faults")
        capture(enterprise_round, plan, runtime.environments, proof)
        result["enterprise_capture"] = str((enterprise_round / "capture.json").relative_to(runtime.root))
        distractors_before = capture_distractors(plan, runtime.environments)
    runtime.step(f"Round {index}: page the on-call operator, resume patrol and verify approved recovery")
    recovered, resumed = recover(iteration, runtime.environments, proof, runtime.workers)
    runtime.recovered = recovered
    write_json(iteration / "recovery.json", recovered)
    result["recovery"] = str((iteration / "recovery.json").relative_to(runtime.root))
    if runtime.enterprise:
        decoy_after = {
            alias: get_snapshot(state, plan["targets"][alias]["decoy_service"])
            for alias, state in runtime.environments.items()
        }
        lifecycle = json.loads((iteration / "incident-lifecycle.json").read_text(encoding="utf-8"))
        finish_round(enterprise_round, lifecycle, decoy_after)
        seal_distractors(enterprise_round, iteration, plan, proof, distractors_before,
                         capture_distractors(plan, runtime.environments))
        for alias, state in runtime.environments.items():
            (state / f"{plan['targets'][alias]['decoy_service']}.fault").unlink()
    healthy = finish_patrols(iteration / "collection", runtime.workers, proof, recovered)
    loop_path = iteration / "patrol-loop.json"
    write_json(loop_path, {alias: {"before": ready[alias], "injected_at": injected_at[alias],
                                  "resumed": resumed[alias], "recovered": healthy[alias]}
                           for alias in runtime.workers})
    result["patrol_loop"] = str(loop_path.relative_to(runtime.root))
    runtime.previous = None
    write_json(runtime.report_path, runtime.report)
    runtime.step(f"Round {index}: review completed incident evidence in isolated Hermes workers")
    runtime.prior_facts = review_facts(iteration, proof, recovered, runtime.shared, runtime.live,
                               openviking=runtime.viking["namespace"] if runtime.viking is not None else None)
    result["fact_reviews"] = runtime.prior_facts
    if runtime.shared.is_file():
        runtime.report["shared_facts"] = runtime.shared.name
    if index == 1:
        runtime.step(f"Round {index}: propose, curate and independently evaluate a recovery-check Skill")
        runtime.recovery_skill = prepare_recovery_skill(runtime.root, iteration, proof["instance-a"], runtime.baseline, runtime.live, process)
        result["recovery_skill"] = runtime.recovery_skill
        if runtime.gepa:
            from gepa_experiment import compare

            runtime.step(f"Round {index}: compare offline GEPA search on frozen HTTP cases")
            comparison = iteration / "gepa"
            result["gepa"] = str((comparison / "report.json").relative_to(runtime.root))
            write_json(runtime.report_path, runtime.report)
            compare(comparison, runtime.baseline, runtime.options, live=runtime.live)
    elif index == 2 and runtime.recovery_skill["status"] == "approved":
        runtime.step(f"Round {index}: use the approved recovery-check Skill on a new incident")
        result["recovery_skill_adoption"] = adopt_recovery_skill(
            runtime.root, iteration, proof["instance-b"], runtime.recovery_skill, runtime.live, process,
        )
    write_json(runtime.report_path, runtime.report)

    return RoundTask(index, iteration, result, sources, before)


def learn(runtime, task):
    from engineering_analysis import build_analysis

    runtime.step(f"Round {task.number}: evolve from the two completed sessions")
    proofs = load_json(task.iteration / "collection" / "patrol-evidence.json")
    analysis = build_analysis(task.iteration, [
        {"instance": alias, "worker": runtime.workers[alias][0], "record": proof["incident"],
         "branch_path": task.iteration / "collection" / alias / "investigation.json"}
        for alias, proof in proofs.items()
    ])
    task.result["engineering_analysis"] = analysis
    candidate, evolution = evolve(task.iteration / "staging", runtime.baseline, task.sources,
                                  runtime.options if runtime.live else None, engineering_analysis=analysis)
    task.result["evolution"] = evolution
    task.result["decision"] = "keep"
    if candidate is not None and sha256(candidate) != task.before:
        runtime.step(f"Round {task.number}: replay baseline and candidate against the same cases")
        evaluation = evaluate(task.iteration / "evaluation", runtime.baseline, candidate, runtime.options,
                              release_policy=runtime.implementation.release_policy)
        task.result["evaluation"] = evaluation
        if evaluation["passed"]:
            approved = evaluation["candidate_sha256"]
            if evaluation["baseline_sha256"] != task.before:
                raise RuntimeError("The evaluation did not use the active baseline")
            if task.number == len(runtime.faults):
                # A final-round candidate has no live task left to demonstrate adoption.
                task.result["decision"] = "qualified"
            else:
                runtime.previous = runtime.baseline, task.before
                task.result["release"] = release(runtime.formal, candidate, approved, runtime.receivers)
                runtime.baseline = runtime.formal / "versions" / approved / ONCALL_SKILL / "SKILL.md"
                task.result["decision"] = "publish"
        else:
            task.result["decision"] = "reject"
    task.result["formal_sha256"] = sha256(runtime.baseline)
    write_json(runtime.report_path, runtime.report)


def run(runtime):
    for number, faults in enumerate(runtime.faults, start=1):
        task = run_once(runtime, number, faults)
        try:
            learn(runtime, task)
        except Exception as exc:
            if not runtime.learning_error(task, exc):
                break


if __name__ == "__main__":
    from fleet import main

    main()
