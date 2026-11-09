#!/usr/bin/env python3
"""Capture one shared-root fault per split and replay its full impact in Hermes."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys

from enterprise_pack import digest, file_digest, iso, row
from enterprise_replay import instruction, lesson_module, read_json, read_session, replay
from environment import write_json
from export_workset import require, run_file
from fanout_capture_audit import audit_capture
from fanout_fixture import FANOUT_RULE, respond
import fanout_gate as gate
from hermes_runtime import ensure_loopback_no_proxy


HERE = Path(__file__).resolve().parent
SEED = HERE / "skills/oncall-investigation/SKILL.md"
CASE_PROTOCOL = HERE / "enterprise_case_gate.py"
SCHEMA = "capstone-shared-root-fanout-v1"
SPLITS = ("dev", "holdout")
VARIANTS = ("baseline", "candidate")
FIXTURE_MODEL = "enterprise-fanout-fixture"


def make_case(capture, split):
    incident, cluster = capture["incident_id"], capture["cluster"]
    services = capture["affected_services"]
    root, control = capture["root_service"], capture["control_service"]
    failure = capture["failure"]
    qualified_root = f"{cluster}/{root}"

    def evidence(identifier, kind, service, when, value, *, owner=incident):
        return {**row(identifier, kind, cluster, service, when, value), "incident_id": owner}

    target_rows = [evidence("root-metrics", "metrics", root, failure[root]["snapshot"]["window_end"],
                            failure[root]["snapshot"])]
    for service in services:
        observation = failure[service]
        snapshot, span, log = (observation[key] for key in ("snapshot", "span", "log"))
        target_rows.extend([
            evidence(f"{service}-alert", "alerts", service, snapshot["window_end"], snapshot),
            evidence(f"{service}-span", "traces", service, span["time"], {**span, "upstream": qualified_root}),
            evidence(f"{service}-dependency", "dependencies", service, span["time"],
                     {"upstream": [qualified_root], "trace_id": span["trace_id"]}),
            evidence(f"root-log-{service}", "logs", root, log["time"], log),
        ])
    health = failure[control]["snapshot"]
    control_row = evidence("healthy-control", "metrics", control, health["window_end"], health,
                           owner=f"control-{incident}")
    rows = [*target_rows, control_row]
    times = [item["time"] for item in rows]
    start, end = min(times), max(times)
    scope = {"cluster": cluster, "service": services[0], "start": start, "end": end}
    prompt = ("CAPSTONE_SCOPE " + json.dumps(scope, sort_keys=True) + "\n"
              + f"CAPSTONE_INCIDENT {incident}\n"
              + "仅调查这个工单。从告警服务出发，确认共同上游及全部受影响服务。"
              "同集群中可能有仍健康的服务，查证后单独列出。用实际工具回执支持结论。")
    queries = sorted({(item["kind"], item["service"]) for item in rows})
    case = {
        "id": f"{incident}@{cluster}", "split": split, "scenario": gate.SCENARIO,
        "input": {"prompt": prompt},
        "snapshot": {"incident_id": incident, "snapshot_id": incident, "version": SCHEMA,
                     "coverage": {"start": start, "end": end, "complete": True}, "rows": rows,
                     "manifest": {"clusters": {cluster: list(lesson_module("monitor").KINDS)}}},
        "annotation": {
            "expected_output": {"root_cause": qualified_root, "affected_services": [f"{cluster}/{s}" for s in services],
                                "excluded_services": [f"{cluster}/{control}"], "needed_evidence": []},
            "required_evidence": [item["id"] for item in target_rows], "control_evidence": [control_row["id"]],
            "required_queries": [{"kind": kind, "cluster": cluster, "service": service, "status": "covered"}
                                 for kind, service in queries],
        },
    }
    gate.validate_case(case)
    return case


def build_pack(root):
    captures = {split: audit_capture(run_file(root, f"captures/{split}/capture.json").parent) for split in SPLITS}
    require(len({item["environment_id"] for item in captures.values()}) == len(SPLITS)
            and len({item["incident_id"] for item in captures.values()}) == len(SPLITS)
            and len({item["failure"][service]["span"]["trace_id"] for item in captures.values()
                     for service in item["affected_services"]}) == 2 * len(SPLITS),
            "Development and holdout must be independent environments, faults and traces")
    pack = {"schema": SCHEMA, "scorer_version": gate.SCORER_VERSION, "scorer_sha256": file_digest(gate.__file__),
            "case_protocol_sha256": file_digest(CASE_PROTOCOL),
            "monitor_sha256": file_digest(lesson_module("monitor").__file__),
            "source_sha256": {split: item["sha256"] for split, item in captures.items()},
            "cases": {split: make_case(item, split) for split, item in captures.items()}}
    return {**pack, "pack_sha256": digest(pack)}


def skills():
    baseline = SEED.read_bytes()
    return {"baseline": baseline, "candidate": baseline.rstrip() + f"\n\n{FANOUT_RULE}\n".encode()}


def selection(dev):
    grades = {key: dev[key]["grade"] for key in VARIANTS}
    if any(item["status"] != "scored" for item in grades.values()):
        return None
    return next((key for key in VARIANTS if grades[key]["passed"]), None)


def decision(selected, holdout):
    if selected is None:
        return "no_dev_qualification"
    if holdout["grade"]["status"] != "scored":
        return "holdout_unscored"
    if not holdout["grade"]["passed"]:
        return "holdout_rejected"
    return "candidate_qualified" if selected == "candidate" else "retain_baseline"


def attempt(root, case, variant, skill, options):
    directory = root / "replay" / case["split"] / variant
    try:
        result = replay(directory, case, skill, options, lesson_module("monitor").query, case_protocol=True)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        result = {"status": "infrastructure_error", "error": str(exc)}
    result["grade"] = gate.grade(case, result)
    output = directory / "evaluation.json"
    write_json(output, result)
    return {"file": str(output.relative_to(root)), "session_id": result.get("session_id"), "grade": result["grade"]}


def run(root, options, live):
    from fanout_capture import capture

    root = root.resolve()
    root.mkdir(parents=True)
    (root / "captures").mkdir()
    variants = skills()
    report = {"schema": SCHEMA, "mode": "live" if live else "local-script-model", "status": "capturing",
              "model": options["model"] if live else FIXTURE_MODEL,
              "skill_sha256": {key: sha256(content).hexdigest() for key, content in variants.items()},
              "candidate_source": "teaching-skill-revision", "dev": {}, "selection": None, "holdout": None}
    output = root / "report.json"
    write_json(output, report)
    try:
        print("Capture and recover the shared-root fault in two fresh HTTP environments", flush=True)
        with ThreadPoolExecutor(max_workers=len(SPLITS)) as pool:
            pending = [pool.submit(capture, root / "captures" / split) for split in SPLITS]
            for future in pending:
                future.result()
        pack = build_pack(root)
        write_json(root / "pack.json", pack)
        report.update(status="evaluating", pack_sha256=pack["pack_sha256"])
        write_json(output, report)
        if live:
            connection = nullcontext(options["base_url"])
        else:
            sys.path.insert(0, str(HERE.parent))
            from local_model import serve

            connection = serve(respond)
        with connection as url:
            runtime = {**options, "model": report["model"], "base_url": url}
            print("Compare both Skills on the development fault", flush=True)
            for variant, skill in variants.items():
                report["dev"][variant] = attempt(root, pack["cases"]["dev"], variant, skill, runtime)
                write_json(output, report)
            selected = selection(report["dev"])
            report["selection"] = selected
            write_json(output, report)
            if selected is not None:
                print(f"Replay the frozen {selected} Skill on the independent holdout", flush=True)
                report["holdout"] = attempt(root, pack["cases"]["holdout"], selected, variants[selected], runtime)
        report.update(status="complete", decision=decision(report["selection"], report["holdout"]))
    except BaseException as exc:
        report.update(status="failed", error=str(exc) or type(exc).__name__)
        raise
    finally:
        write_json(output, report)
    return report


def audit_attempt(root, case, variant, summary, expected_skill, model, sessions):
    relative = f"replay/{case['split']}/{variant}/evaluation.json"
    require(summary["file"] == relative, "Shared-root replay points outside its assigned variant")
    path = run_file(root, relative)
    result = read_json(path)
    if result["status"] == "completed":
        state = path.parent
        skill = run_file(state, "SKILL.md").read_bytes()
        database = run_file(state, "hermes/state.db")
        wal = database.with_name("state.db-wal")
        require((not wal.exists() or wal.stat().st_size == 0) and skill == expected_skill
                and read_json(run_file(state, "task.json")) == case["input"]
                and read_json(run_file(state, "snapshot.json")) == case["snapshot"],
                "Shared-root replay inputs, Skill or persisted SessionDB changed")
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
            stored_model = db.execute("SELECT model FROM sessions WHERE id=?", (result["session_id"],)).fetchone()
        require(stored_model == (model,), "Shared-root replay model differs from the recorded Session")
        actual = read_session(state, result, case["input"]["prompt"] + "\n\n" + instruction(True),
                              case["snapshot"], skill, lesson_module("monitor").query)
        require(all(result[key] == value for key, value in actual.items()) and result["session_id"] not in sessions,
                "Shared-root replay changed its tool receipts or reused a Session")
        sessions.add(result["session_id"])
    require(summary["session_id"] == result.get("session_id") and summary["grade"] == result["grade"] == gate.grade(case, result),
            "Shared-root grade disagrees with the original SessionDB evidence")


def audit(root):
    root = root.resolve(strict=True)
    pack, report = read_json(run_file(root, "pack.json")), read_json(run_file(root, "report.json"))
    variants = skills()
    require(pack == build_pack(root) and report["schema"] == SCHEMA and report["status"] == "complete"
            and report["pack_sha256"] == pack["pack_sha256"] and report["candidate_source"] == "teaching-skill-revision"
            and report["skill_sha256"] == {key: sha256(content).hexdigest() for key, content in variants.items()}
            and report["dev"].keys() == variants.keys() and report["mode"] in {"local-script-model", "live"}
            and (report["model"] == FIXTURE_MODEL) == (report["mode"] == "local-script-model"),
            "Shared-root pack or evaluation does not match its independently captured source")
    sessions = set()
    for variant, skill in variants.items():
        audit_attempt(root, pack["cases"]["dev"], variant, report["dev"][variant], skill, report["model"], sessions)
    selected = selection(report["dev"])
    require(report["selection"] == selected and (report["holdout"] is not None) == (selected is not None),
            "Shared-root holdout opened without a development qualification")
    if selected is None:
        require(not (root / "replay/holdout").exists(), "Unqualified replay contains holdout artifacts")
    else:
        audit_attempt(root, pack["cases"]["holdout"], selected, report["holdout"], variants[selected], report["model"], sessions)
    outcome = decision(selected, report["holdout"])
    require(report["decision"] == outcome, "Shared-root Skill decision changed")
    return {"status": "passed" if outcome in {"candidate_qualified", "retain_baseline"} else "pending",
            "mode": report["mode"], "decision": outcome, "scorer_version": pack["scorer_version"],
            "captures": len(SPLITS), "sessions": len(sessions)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="Fresh directory for capture and Hermes replay")
    group.add_argument("--audit", type=Path, help="Recheck a completed run without modifying its evidence")
    parser.add_argument("--live", action="store_true", help="Use the configured GLM model instead of the local fixture")
    args = parser.parse_args()
    if args.audit is not None:
        if args.live:
            parser.error("--live applies to --out")
        print(json.dumps(audit(args.audit), ensure_ascii=False, indent=2))
        return
    ensure_loopback_no_proxy()
    if args.live:
        from patrol import model_connection

        connection = model_connection(False)
    else:
        connection = nullcontext("")
    with connection as url:
        options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if args.live else "local-fixture",
                   "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if args.live else FIXTURE_MODEL}
        report = run(args.out, options, args.live)
    print(f"{args.out.resolve() / 'report.json'} ({report['decision']})")


if __name__ == "__main__":
    main()
