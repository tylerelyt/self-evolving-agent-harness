"""Compare two On-call Skills through the same isolated Hermes replay cases."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from environment import write_json
from hermes_runtime import ONCALL_SKILL, install_oncall_skill, open_sessions
from patrol import connect, decode_report, inspect_service


@dataclass(frozen=True)
class ReplayCase:
    name: str
    statuses: tuple[int, ...]
    assessment: str
    service: str = "payments"
    environment_id: str = "patrol-replay"
    window_start: int = 1_700_000_000
    requests: int = 40
    error_rate: float = .25
    p95_ms: int = 110

    def snapshot(self, _state, service):
        return {
            "source": "frozen_business_replay",
            "service": service,
            "environment_id": self.environment_id,
            "window_start": self.window_start,
            "window_end": self.window_start + 30,
            "requests": self.requests,
            "error_rate": self.error_rate,
            "p95_ms": self.p95_ms,
        }

    def fixture(self):
        return {
            "snapshot": self.snapshot(None, self.service),
            "probe_http_statuses": list(self.statuses),
            "assessment": self.assessment,
        }


# Keep the visible metrics identical. The first probe determines the next action.
CASES = (
    ReplayCase("intermittent", (503, 200), "intermittent"),
    ReplayCase("persistent", (503, 503), "persistent"),
    ReplayCase("not-reproduced", (200,), "not_reproduced"),
)


def read_receipts(sessions, record, skill_sha):
    rows = sessions.get_messages(record["session_id"])
    by_item = {row["id"]: row for row in rows}
    calls = {
        call["id"]: (row["id"], call["function"]["name"])
        for row in rows for call in row.get("tool_calls") or []
    }

    skill = by_item[record["skill_item_id"]]
    loaded = json.loads(skill["content"])
    verified_skill = (
        skill["role"] == "tool"
        and calls.get(skill["tool_call_id"], (None, None))[1] == "skill_view"
        and loaded.get("name") == ONCALL_SKILL
        and loaded.get("sha256") == record["skill_sha256"] == skill_sha
        and sha256(loaded["content"].encode("utf-8")).hexdigest() == skill_sha
    )

    probes = [
        {"item_id": row["id"], "call_item_id": calls[row["tool_call_id"]][0],
         "receipt": json.loads(row["content"])}
        for row in rows
        if row["role"] == "tool" and calls.get(row["tool_call_id"], (None, None))[1] == "oncall_probe"
    ]
    report = decode_report(by_item[record["report_item_id"]]["content"])
    return verified_skill, probes, report


def replay(case, state, skill, options):
    state.mkdir(parents=True)
    snapshot = case.snapshot(state, case.service)
    statuses = iter(case.statuses)
    step = 0

    def probe(_state, service):
        nonlocal step
        status = next(statuses, None)
        if status is None:
            return {"error": "Unexpected additional probe in this replay"}
        step += 1
        return {
            "service": service, "path": "/work", "http_status": status,
            "environment_id": snapshot["environment_id"],
            "checked_at": snapshot["window_end"] + step,
        }

    try:
        with closing(connect(state)) as db, open_sessions(state) as sessions:
            digest = install_oncall_skill(state, skill)
            record = inspect_service(
                sessions, db, case.service, state, options, digest,
                snapshot=case.snapshot, probe_request=probe,
            )
            verified_skill, probes, report = read_receipts(sessions, record, digest)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        return {"passed": False, "valid_trace": False, "error": str(exc)}

    observed = tuple(item["receipt"].get("http_status") for item in probes)
    assessment = report.get("assessment")
    assessment_matches_evidence = (
        observed == case.statuses and assessment == case.assessment
        or len(case.statuses) == 2 and observed == case.statuses[:1]
        and assessment == "failure_observed"
    )
    valid_trace = (
        verified_skill
        and bool(probes)
        and all(record["skill_item_id"] < item["call_item_id"] < item["item_id"] < record["report_item_id"]
                for item in probes)
        and all(previous["item_id"] < current["call_item_id"] for previous, current in zip(probes, probes[1:]))
        and all(item["receipt"].get("service") == case.service
                and item["receipt"].get("environment_id") == snapshot["environment_id"] for item in probes)
        and report.get("probe_http_status") == case.statuses[0]
        and report.get("probe_http_statuses") == list(observed)
        and assessment_matches_evidence
        and json.loads(record["observation"]) == snapshot
    )
    return {
        "passed": valid_trace and observed == case.statuses and assessment == case.assessment,
        "valid_trace": valid_trace,
        "session_id": record["session_id"],
        "incident_id": record["incident_id"],
        "skill_sha256": digest,
        "skill_item_id": record["skill_item_id"],
        "report_item_id": record["report_item_id"],
        "probes": probes,
        "assessment": assessment,
    }


def evaluate(root: Path, baseline: Path, candidate: Path, options: dict, *, cases=CASES) -> dict:
    """Run identical cases in fresh Hermes homes and record the comparison."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    skills = {}
    for version, source in (("baseline", baseline), ("candidate", candidate)):
        pinned = root / version / "SKILL.md"
        pinned.parent.mkdir()
        content = source.read_bytes()
        pinned.write_bytes(content)
        skills[version] = {"path": pinned, "sha256": sha256(content).hexdigest()}

    results = []
    previous_home = os.environ.get("HERMES_HOME")
    try:
        for case in cases:
            results.append({
                "case": case.name,
                "fixture": case.fixture(),
                **{
                    version: replay(case, root / version / case.name, spec["path"], options)
                    for version, spec in skills.items()
                },
            })
    finally:
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home

    comparable = all(result["baseline"]["valid_trace"] for result in results)
    candidate_passed_all = all(result["candidate"]["passed"] for result in results)
    improved = [result["case"] for result in results
                if result["candidate"]["passed"] and not result["baseline"]["passed"]]
    baseline_sha = skills["baseline"]["sha256"]
    candidate_sha = skills["candidate"]["sha256"]
    decision = {
        "passed": comparable and candidate_passed_all and bool(improved) and baseline_sha != candidate_sha,
        "model": options["model"],
        "baseline_sha256": baseline_sha,
        "candidate_sha256": candidate_sha,
        "baseline_comparable": comparable,
        "candidate_passed_all": candidate_passed_all,
        "improved": improved,
        "cases": results,
    }
    write_json(root / "evaluation.json", decision)
    return decision
