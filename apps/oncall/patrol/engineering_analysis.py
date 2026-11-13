"""Turn verified investigation branches into bounded engineering guidance."""

from hashlib import sha256
import json
from pathlib import Path
import sys

from environment import load_json, write_json
from fixture import CONFIRMATION_STEP


SCHEMA = "course-engineering-analysis-v1"
POLICY_ID = "bounded-probe-review-v1"
CAPSTONE = Path(__file__).resolve().parents[1]


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assess(source):
    if str(CAPSTONE) not in sys.path:
        sys.path.insert(0, str(CAPSTONE))
    # Import lazily because the shared verifier reads fleet and Memory helpers.
    from workbench_memory import verified_branch
    from fleet import receipt

    worker = Path(source["worker"]).resolve()
    record = source["record"]
    path = Path(source["branch_path"]).resolve()
    branch = load_json(path)
    scope = verified_branch(worker, record, branch)
    initial = receipt(worker, record)
    if sha256(initial["skill"]["content"].encode()).hexdigest() != record["skill_sha256"]:
        raise ValueError("Engineering review source Skill differs from its original receipt")
    statuses = [probe["http_status"] for probe in branch["probes"]]
    if len(statuses) != 3 or any(type(status) is not int or status // 100 not in (2, 5) for status in statuses):
        raise ValueError("Engineering review requires three recorded follow-up probes")
    failures = sum(status >= 500 for status in statuses)
    assessment = "persistent" if failures == 3 else "intermittent" if failures else "not_reproduced"
    if branch["report"]["assessment"] != assessment or branch["report"]["root_cause"] != "unknown":
        raise ValueError("The branch conclusion differs from its recorded probes")
    initial_statuses = [probe["http_status"] for probe in initial["probes"]]
    recommend = len(initial_statuses) == 1 and initial_statuses[0] >= 500
    return {
        "instance": source["instance"], **scope,
        "source_session_id": record["session_id"], "source_report_item_id": record["report_item_id"],
        "source_skill_sha256": record["skill_sha256"],
        "branch_session_id": branch["session_id"], "branch_report_item_id": branch["report_item_id"],
        "branch_skill_item_id": branch["skill_item_id"], "branch_sha256": sha256(path.read_bytes()).hexdigest(),
        "initial_probe_http_statuses": initial_statuses, "followup_probe_http_statuses": statuses,
        "assessment": assessment,
        "engineer_assessment": (
            f"初判实际探测为 {initial_statuses}，报告分支的三次新探测为 {statuses}，表现为 {assessment}。"
            "这些回执只能支持当时的故障表现，根因仍未知。单次 5xx 不足以区分持续和间歇失败。"
        ),
        "method_recommendation": {
            "action": "add_bounded_second_probe" if recommend else "retain_existing_probe_rule",
            "instruction": CONFIRMATION_STEP if recommend else "保留已有的有界探测复核，不增加无依据的根因判断。",
            "scope": "只修订通用调查方法，恢复仍须审批并由后续独立巡检验证。",
        },
    }


def guidance_for(sources):
    cases = [assess(source) for source in sources]
    if (len(cases) < 2 or len({case["instance"] for case in cases}) != len(cases)
            or len({case["source_session_id"] for case in cases}) != len(cases)):
        raise ValueError("Engineering analysis requires distinct instances and original sessions")
    return {"schema": SCHEMA, "policy": {
        "id": POLICY_ID, "author": "course-scripted-engineer", "kind": "deterministic_evidence_template",
        "implementation": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
    }, "cases": cases}


def build_analysis(root, sources):
    """Persist independent analysis while keeping fork sessions outside the native queue."""
    sources = [{**source, "worker": str(Path(source["worker"]).resolve()),
                "branch_path": str(Path(source["branch_path"]).resolve())} for source in sources]
    guidance = guidance_for(sources)
    path = Path(root).resolve() / "engineering-analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"schema": SCHEMA, "sources": sources, "guidance": guidance,
                      "guidance_sha256": digest(guidance)})
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
            "guidance_sha256": digest(guidance)}


def load_analysis(reference, baseline_sha, initial_ids):
    """Recheck source branches and bind guidance to the exact original evolution batch."""
    path = Path(reference["path"])
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("Engineering analysis artifact changed before evolution")
    artifact = json.loads(raw)
    guidance = guidance_for(artifact["sources"])
    if (artifact.get("schema") != SCHEMA or artifact["guidance"] != guidance
            or digest(guidance) != artifact["guidance_sha256"]
            or artifact["guidance_sha256"] != reference["guidance_sha256"]):
        raise ValueError("Engineering guidance differs from its verified evidence or policy")
    cases = guidance["cases"]
    if ({case["source_session_id"] for case in cases} != set(initial_ids)
            or any(case["source_skill_sha256"] != baseline_sha for case in cases)
            or any(case["branch_session_id"] in initial_ids for case in cases)):
        raise ValueError("Engineering guidance does not match the initial-only evolution batch")
    return guidance
