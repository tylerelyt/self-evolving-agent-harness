"""Stage, curate, verify and later load a separate recovery-check method."""

from hashlib import sha256
import json
from pathlib import Path

from environment import write_json


SKILL = "oncall-recovery-window-check"


def recovery_evidence(iteration, proof):
    incident = proof["incident"]["incident_id"]
    branch = proof["investigation"]
    lifecycle = json.loads((iteration / "incident-lifecycle.json").read_text(encoding="utf-8"))
    state = next(item for item in lifecycle["incidents"] if item["id"] == incident)
    events = [item for item in lifecycle["events"] if item["incident"] == incident]
    executed = [item["body"] for item in events if item["kind"] == "action_executed"]
    windows = [item["body"]["evidence"] for item in events
               if item["kind"] == "observation" and item["body"]["source"] == "verification"]
    if state["state"] != "CLOSED" or len(executed) != 1 or len(windows) != 2:
        raise RuntimeError("Recovery Skill needs the two verified windows of a closed incident")
    return {
        "incident_id": incident, "scope": {key: branch[key] for key in ("service", "environment_id")},
        "source_session_id": branch["session_id"], "source_report_item_id": branch["report_item_id"],
        "cleared_at": executed[0]["effect"]["cleared_at"], "recovery_windows": windows,
    }


def worker(process, state, script, args, *, offline=False):
    state.mkdir(parents=True)
    output = state / "result.json"
    cli = [*args, "--out", str(output)]
    if offline:
        cli.append("--offline")
    with process(script, state, state / "worker.log", *cli) as child:
        if child.wait(timeout=180) != 0:
            raise RuntimeError(f"Recovery Skill {state.name} failed. Read {state / 'worker.log'}")
    return json.loads(output.read_text(encoding="utf-8"))


def staged_skill(state, relative, expected_hash):
    path = (state / relative).resolve(strict=True)
    if (Path(relative).is_absolute() or not path.is_relative_to(state.resolve())
            or path.parent.name != SKILL or path.name != "SKILL.md"
            or sha256(path.read_bytes()).hexdigest() != expected_hash):
        raise RuntimeError("Staged recovery Skill differs from its candidate path or hash")
    return path


def prepare_recovery_skill(run, iteration, proof, formal_skill, live, process):
    root = iteration / "recovery-skill"
    root.mkdir()
    evidence = root / "evidence.json"
    write_json(evidence, recovery_evidence(iteration, proof))
    formal_hash = sha256(formal_skill.read_bytes()).hexdigest()
    review = worker(process, root / "review", "recovery_skill_review.py",
                    ["--evidence", str(evidence), "--formal-skill", str(formal_skill)], offline=not live)
    if (review["source_sha256"] != sha256(evidence.read_bytes()).hexdigest()
            or review["formal_sha256"] != formal_hash or review["native_review"]["completed"] is not True
            or sha256(formal_skill.read_bytes()).hexdigest() != formal_hash
            or review["status"] not in ("proposed", "keep")):
        raise RuntimeError("Recovery Skill proposal differs from its source or changes the formal Skill")
    result = {"status": review["status"], "source": "evidence.json", "review": "review/result.json",
              "protected_skill_sha256": formal_hash}
    if review["status"] == "proposed":
        digest = review["candidate_sha256"]
        candidate = staged_skill(root / "review", review["candidate"], digest)
        curation = worker(process, root / "curator", "recovery_skill_curator.py",
                          ["--candidate", str(candidate), "--formal-skill", str(formal_skill)])
        retired, protected = curation["retired_draft"], curation["formal_protection"]
        if (curation["status"] != "curated" or curation["candidate_sha256"] != digest
                or curation["formal_sha256"] != formal_hash or curation["native_transitions"]["archived"] != 1
                or retired["state"] != "archived" or retired["listed_in_native_archive"] is not True
                or retired["recovery_verified"] is not True or protected["pinned"] is not True
                or protected["state"] != "active" or protected["source_unchanged"] is not True
                or protected["copy_unchanged"] is not True):
            raise RuntimeError("Curator did not preserve the intended candidate and protected Skill")
        curated = staged_skill(root / "curator", curation["candidate"], digest)
        gate = worker(process, root / "gate", "recovery_skill_task.py", ["--skill", str(curated)], offline=not live)
        if gate["skill_sha256"] != digest or gate["status"] not in ("passed", "rejected"):
            raise RuntimeError("Recovery Skill gate did not evaluate the curated candidate")
        result.update(status=gate["status"], curator="curator/result.json", gate="gate/result.json",
                      candidate_sha256=digest)
        if gate["status"] == "passed":
            cases = gate["cases"]
            if ({case["expected_decision"] for case in cases} != {"recovered", "unverified"}
                    or any(case["passed"] is not True for case in cases)):
                raise RuntimeError("Recovery Skill approval lacks passing positive and negative cases")
            content = curated.read_bytes()
            if sha256(content).hexdigest() != digest:
                raise RuntimeError("Recovery Skill changed after its independent evaluation")
            active = run / "recovery-methods" / "approved" / digest / SKILL / "SKILL.md"
            active.parent.mkdir(parents=True)
            active.write_bytes(content)
            result.update(status="approved", approved_skill=str(active.relative_to(run)))
    elif review["candidate"] is not None:
        raise RuntimeError("Keep must not release a recovery Skill candidate")
    if sha256(formal_skill.read_bytes()).hexdigest() != formal_hash:
        raise RuntimeError("The formal triage Skill changed during recovery method review")
    write_json(root / "sequence.json", result)
    return result


def adopt_recovery_skill(run, iteration, proof, approved, live, process):
    root = iteration / "recovery-skill"
    root.mkdir()
    evidence = root / "evidence.json"
    write_json(evidence, recovery_evidence(iteration, proof))
    skill = run / approved["approved_skill"]
    digest = approved["candidate_sha256"]
    if sha256(skill.read_bytes()).hexdigest() != digest:
        raise RuntimeError("The approved recovery Skill differs from the version evaluated")
    adoption = worker(process, root / "adopt", "recovery_skill_task.py",
                      ["--skill", str(skill), "--evidence", str(evidence)], offline=not live)
    if (adoption["status"] != "adopted" or adoption["skill_sha256"] != digest
            or adoption["source_sha256"] != sha256(evidence.read_bytes()).hexdigest()
            or adoption["case"]["incident_id"] != proof["incident"]["incident_id"]
            or adoption["case"]["passed"] is not True):
        raise RuntimeError("The next incident did not demonstrate use of the approved recovery Skill")
    return str((root / "adopt" / "result.json").relative_to(iteration))
