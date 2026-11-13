#!/usr/bin/env python3
"""Verify native fleet traces and optionally round-trip a new Langfuse dataset."""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from uuid import uuid4

from export_workset import build_workset, require, run_file, session_rows
from hermes_runtime import check_langfuse, ensure_loopback_no_proxy

LESSON19 = Path(__file__).resolve().parents[1] / "integrations/dataset"
sys.path.insert(1, str(LESSON19))
from dataset import digest, file_digest, prepare, read, save, verify, write_pack
from langfuse_io import client, collect, export, map_tasks, publish, write_restored_pack
from pipeline import decoded, plugin_projection


def read_turn(db, session_id, report_item, lower_bound):
    user_item, = db.execute(
        "SELECT max(id) FROM messages WHERE session_id=? AND role='user' AND id>? AND id<?",
        (session_id, lower_bound, report_item),
    ).fetchone()
    require(user_item is not None, "Native Trace has no corresponding user Item")
    rows = session_rows(db, session_id, user_item, report_item)
    assistant = [row for row in rows.values() if row["role"] == "assistant"]
    require(assistant and assistant[-1]["id"] == report_item, "Native Trace has no corresponding report Item")
    calls = {call["id"]: call["function"] for row in assistant for call in json.loads(row["tool_calls"] or "[]")}
    tools = {}
    for row in rows.values():
        if row["role"] != "tool":
            continue
        call = calls[row["tool_call_id"]]
        require(row["tool_call_id"] not in tools and row["tool_name"] == call["name"],
                "SessionDB tool Item does not match its assistant call")
        tools[row["tool_call_id"]] = {
            "name": call["name"], "input": decoded(call["arguments"]), "output": decoded(row["content"]),
        }
    require(tools and tools.keys() == calls.keys(), "SessionDB turn has incomplete tool calls")
    return {"user_item_id": user_item, "prompt": rows[user_item]["content"],
            "report": assistant[-1]["content"], "generation_count": len(assistant), "tools": tools}


def local_sources(run):
    run = Path(run).resolve(strict=True)
    report = read(run_file(run, "report.json"))
    flag = report.get("langfuse") or {}
    require(report["status"] == "complete" and flag.get("plugin") == "observability/langfuse"
            and flag.get("scope") == "patrol_and_followup" and flag.get("status") == "awaiting_remote_verification"
            and flag.get("expected_turns") == 8, "Use a completed fleet run with --enterprise --langfuse")
    require(build_workset(run, report) == read(run_file(run, report["http_workset"])),
            "The fleet workset no longer matches its SessionDB evidence")
    pack = read(run_file(run, "enterprise/prepared/pack.json"))
    verify(pack)
    source_path = run_file(run, "enterprise/rollouts.json")
    annotation_path = run_file(run, "enterprise/annotations.json")
    source, annotations = read(source_path), read(annotation_path)
    require(source["origin"]["kind"] == "capstone_live_lab"
            and pack["versions"]["source_sha256"] == file_digest(source_path)
            and pack["versions"]["annotations_sha256"] == file_digest(annotation_path),
            "The fleet source or independent annotation changed")
    rollouts = {record["id"]: record for record in source["traces"]}
    require(len(rollouts) == len(source["traces"]) == flag["expected_turns"],
            "The fleet has missing or duplicate source turns")
    turns, snapshots = {}, {}
    for round_data in report["rounds"]:
        index = round_data["round"]
        proofs_path = run_file(run, round_data["patrol_evidence"])
        capture = read(run_file(run, f"enterprise/round-{index}/capture.json"))
        require(capture.pop("sha256") == digest(capture) and capture["source_sha256"] == file_digest(proofs_path),
                "Frozen enterprise capture changed after collection")
        for alias, proof in read(proofs_path).items():
            incident, branch = proof["incident"], proof["investigation"]
            incident_id = incident["incident_id"]
            frozen = capture["cases"][alias]
            require(frozen["incident_id"] == incident_id and re.fullmatch(r"[A-Za-z0-9_-]+", incident_id),
                    "Frozen snapshot belongs to a different incident")
            if frozen["snapshot"] is not None:
                snapshot = read(run_file(run, f"enterprise/snapshots/{incident_id}.json"))
                require(snapshot == frozen["snapshot"], "Frozen enterprise snapshot changed after capture")
                snapshots[incident_id] = snapshot
            db_path = run_file(run, f"round-{index}/collection/{alias}/hermes/state.db")
            with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                db.row_factory = sqlite3.Row
                for role, entry, task_id, start in (
                    ("patrol", incident, incident["turn_id"], 0),
                    ("followup", branch, f"followup-{incident['turn_id']}", branch["copied_report_item_id"]),
                ):
                    session_id, report_item = entry["session_id"], entry["report_item_id"]
                    key = f"session:{session_id}:item:{report_item}"
                    task = rollouts[key]
                    require(key not in turns and task["metadata"]["incident_id"] == incident_id
                            and task["metadata"]["cluster"] == alias and task["metadata"]["snapshot_id"] == incident_id
                            and task["metadata"]["split"] == {1: "dev", 2: "holdout"}[index]
                            and task["metadata"]["skill_version"] == incident["skill_sha256"],
                            "A rollout belongs to a different fleet case or split")
                    turns[key] = {**read_turn(db, session_id, report_item, start),
                                  "session_id": session_id, "task_id": task_id, "report_item_id": report_item,
                                  "role": role, "round": index, "instance": alias, "incident_id": incident_id}
    original = prepare(source, annotations, snapshots)
    require(turns.keys() == rollouts.keys() and all(original[field] == pack[field] for field in ("ready", "pending")),
            "The frozen local pack no longer matches its original inputs")
    return pack, rollouts, turns, annotations, snapshots


def confirm_trace(trace, turn):
    metadata = trace["metadata"]
    observations = trace["raw_trace"]["observations"]
    roots = trace["root_observation_ids"]
    root = next((row for row in observations if row["id"] in roots), {})
    require(len(roots) == 1 and root.get("name") == "Hermes turn" and metadata.get("source") == "hermes"
            and metadata.get("task_id") == turn["task_id"]
            and metadata.get("session_id") in (None, turn["session_id"])
            and root.get("sessionId") in (None, turn["session_id"])
            and decoded(trace["input"]) == {"role": "user", "content": turn["prompt"]},
            "Remote root does not match the native Hermes task, Session and user Item")
    require(len({row["id"] for row in observations}) == len(observations)
            and all(row.get("endTime") and (row["id"] in roots or row.get("parentObservationId") in roots)
                    for row in observations),
            "Remote Trace observations are incomplete or attached to a different root")
    report = decoded(trace["output"])
    require(isinstance(report, dict) and report.get("content") == turn["report"],
            "Remote Trace report does not match the SessionDB report Item")
    tools = [row for row in observations if row.get("type") == "TOOL"]
    generations = [row for row in observations if row.get("type") == "GENERATION"]
    require(len(observations) == 1 + len(tools) + len(generations) and len(generations) == turn["generation_count"],
            "Remote model calls do not match the SessionDB turn")
    actual_calls = []
    finals = []
    user_input_seen = False
    for row in generations:
        model_input, model_output = decoded(row.get("input")), decoded(row.get("output"))
        require(isinstance(model_input, list) and model_input and isinstance(model_output, dict)
                and (model_output.get("content") or model_output.get("tool_calls")),
                "Remote generation lacks its model request or response")
        user_input_seen |= {"role": "user", "content": turn["prompt"]} in model_input
        calls = model_output.get("tool_calls") or []
        if not calls:
            finals.append(model_output.get("content"))
        for call in calls:
            key = call["id"]
            function = call["function"]
            expected_call = turn["tools"].get(key, {})
            require(function["name"] == expected_call.get("name")
                    and decoded(function["arguments"]) == expected_call.get("input"),
                    "Remote generation contains a different tool name or arguments")
            actual_calls.append(key)
    require(user_input_seen and finals == [turn["report"]]
            and Counter(actual_calls) == Counter(turn["tools"].keys()),
            "Remote generations contain a different user prompt, tool call set or final report")
    expected_tools = Counter()
    truncated = []
    for key, tool in turn["tools"].items():
        output, omitted = plugin_projection(tool["output"])
        expected_tools[digest({
            "id": key, "name": f"Tool: {tool['name']}", "input": tool["input"], "output": output,
        })] += 1
        if omitted:
            truncated.append({"tool_call_id": key, "paths": omitted})
    actual_tools = Counter(digest({
        "id": (row.get("metadata") or {}).get("tool_call_id"),
        "name": row["name"],
        "input": decoded(row.get("input")),
        "output": decoded(row.get("output")),
    }) for row in tools)
    require(actual_tools == expected_tools, "Remote tool observations do not match SessionDB calls and receipts")
    return {"generations": len(generations), "tools": len(tools), "projected_tool_fields": truncated}


def wait_for_traces(lf, expected, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            source = collect(lf, expected.keys())
            require({trace["id"] for trace in source["traces"]} == expected.keys(), "Remote Trace set differs from the fleet")
            return source, {trace["id"]: confirm_trace(trace, expected[trace["id"]]) for trace in source["traces"]}
        except (LookupError, ValueError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Native Trace verification did not complete: {exc}") from exc
            time.sleep(min(2, remaining))


def comparable_cases(pack):
    return {case["id"]: {key: case[key] for key in ("input", "snapshot", "annotation", "split", "scenario")}
            for case in pack["ready"]}


def run(output, local, lf, create_trace_id, *, dataset_prefix=None, timeout=90):
    pack, rollouts, turns, annotations, snapshots = local
    output.mkdir(parents=True, exist_ok=False)
    expected = {create_trace_id(seed=f"{turn['session_id']}::{turn['task_id']}"): {**turn, "source": key}
                for key, turn in turns.items()}
    require(len(expected) == len(turns), "Native Trace IDs collided")
    source, checks = wait_for_traces(lf, expected, timeout)
    tasks = {trace_id: {field: rollouts[turn["source"]][field] for field in ("input", "metadata")}
             for trace_id, turn in expected.items()}
    mapped = map_tasks(source, tasks)
    index = {trace_id: {field: turn[field] for field in (
        "source", "session_id", "task_id", "user_item_id", "report_item_id", "role", "round", "instance", "incident_id",
    )} | checks[trace_id] for trace_id, turn in expected.items()}
    save(output / "trace-index.json", index)
    save(output / "collected.json", source)
    save(output / "mapped.json", mapped)
    save(output / "annotations.json", annotations)
    for snapshot_id, snapshot in snapshots.items():
        save(output / "snapshots" / f"{snapshot_id}.json", snapshot)
    native = write_pack(output / "mapped.json", output / "annotations.json", output / "snapshots", output / "prepared")
    verify(native)
    require(comparable_cases(native) == comparable_cases(pack)
            and [(row["case_id"], row["reasons"]) for row in native["pending"]]
            == [(row["case_id"], row["reasons"]) for row in pack["pending"]],
            "Trace mapping changed the independently labelled cases")
    report = {"status": "verified", "native_traces": len(index), "ready": len(native["ready"]),
              "pending": len(native["pending"]), "pack_sha256": native["pack_sha256"]}
    save(output / "report.json", report)
    if dataset_prefix is None:
        return report

    from langfuse.api.commons.errors.not_found_error import NotFoundError

    require(native["ready"], "The verified pack contains no publishable cases")
    # Langfuse upserts datasets by name. An independent name per invocation
    # keeps concurrent experiments with the same prefix in separate datasets.
    dataset = f"{dataset_prefix}-{uuid4().hex}"
    try:
        lf.api.datasets.get(dataset_name=dataset)
    except NotFoundError:
        pass
    else:
        raise ValueError("The generated dataset name already exists; retry with a new output directory")
    report.update(status="publishing", dataset=dataset, dataset_prefix=dataset_prefix)
    save(output / "report.json", report)
    try:
        receipt = publish(lf, dataset, native)
        save(output / "published.json", receipt)
        version = receipt["export_version_hint"]
        report.update(status="reading_back", dataset_version=version)
        save(output / "report.json", report)
        deadline = time.monotonic() + timeout
        while True:
            exported = export(lf, dataset, version)
            if len(exported["items"]) >= len(receipt["items"]) or time.monotonic() >= deadline:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        save(output / "exported.json", exported)
        items = {row["id"]: row for row in receipt["items"]}
        fetched = {row["id"]: row for row in exported["items"]}
        require(len(fetched) == len(exported["items"]) and items == fetched, "Dataset version differs from the publish receipt")
        restored = write_restored_pack(exported, snapshots, output / "restored")
        verify(restored)
        require(not restored["pending"] and comparable_cases(restored) == comparable_cases(native),
                "The restored dataset changed the snapshots, independent labels or split")
        source_traces = {row["metadata"]["case_id"]: row["source_trace_id"] for row in receipt["items"]}
        require(all(case["provenance"]["source_trace_id"] == source_traces[case["id"]]
                    and source_traces[case["id"]] in case["provenance"]["trace_ids"]
                    and set(case["provenance"]["trace_ids"]) <= index.keys() for case in restored["ready"]),
                "The restored dataset lost its native Trace links")
    except (Exception, KeyboardInterrupt):
        report.update(status="failed", failed_phase=report["status"])
        save(output / "report.json", report)
        raise
    report["status"] = "roundtrip_complete"
    save(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="Completed fleet --enterprise --langfuse run")
    parser.add_argument("--out", type=Path, required=True, help="New directory for Trace verification evidence")
    parser.add_argument("--dataset", help="Prefix for a uniquely named dataset; required with --apply")
    parser.add_argument("--apply", action="store_true", help="Publish and read back the verified dataset")
    parser.add_argument("--timeout", type=int, default=90, help="Seconds to wait for eventual consistency")
    args = parser.parse_args()
    if args.apply != (args.dataset is not None):
        parser.error("--dataset and --apply must be supplied together")
    if args.dataset is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,86}", args.dataset):
        parser.error("Use a dataset prefix with up to 87 letters, numbers, underscores or hyphens")
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300")
    local = local_sources(args.run.resolve(strict=True))
    check_langfuse()
    from langfuse import Langfuse

    ensure_loopback_no_proxy()
    lf = client()
    try:
        result = run(args.out.resolve(), local, lf, Langfuse.create_trace_id,
                     dataset_prefix=args.dataset, timeout=args.timeout)
    finally:
        lf.shutdown()
    print(f"{args.out.resolve() / 'report.json'} ({result['status']})")


if __name__ == "__main__":
    main()
