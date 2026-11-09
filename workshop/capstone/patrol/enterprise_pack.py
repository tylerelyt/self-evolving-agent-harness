"""Build Lesson 19 cases from scoped live captures and independent lab recovery."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import environment


LESSON19 = Path(__file__).resolve().parents[3] / "examples" / "19-enterprise-dataset"
sys.path.insert(1, str(LESSON19))
from dataset import digest, file_digest, read, verify, write_pack
from monitor import KINDS

SNAPSHOT_VERSION = "capstone-http-telemetry-v1"


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sealed(path, data):
    if path.exists():
        raise FileExistsError(path)
    environment.write_json(path, {**data, "sha256": digest(data)})


def read_sealed(path):
    data = read(path)
    checksum = data.pop("sha256")
    if checksum != digest(data):
        raise ValueError(f"Evidence changed after capture: {path}")
    return data, checksum


def fleet_round(directory, index):
    return directory.parents[1] / f"round-{index}"


def check_scope(receipt, service, environment_id, source):
    if (receipt["service"], receipt["environment_id"], receipt["source"]) != (service, environment_id, source):
        raise ValueError(f"Receipt does not belong to {environment_id}/{service}")
    if not receipt["window_start"] < receipt["window_end"]:
        raise ValueError(f"Invalid observation window for {environment_id}/{service}")


def check_metrics(receipt, service, environment_id):
    check_scope(receipt, service, environment_id, "live_lab_http_requests")


def telemetry(state, service, environment_id):
    data = environment.get_telemetry(state, service)
    check_scope(data, service, environment_id, "live_lab_http_telemetry")
    for item in data["logs"] + data["traces"]:
        if item["service"] != service or not data["window_start"] <= item["time"] <= data["window_end"]:
            raise ValueError("Telemetry is outside the service or time coverage")
    return data


def row(identifier, kind, alias, service, when, value):
    return {"id": identifier, "kind": kind, "cluster": alias, "service": service, "time": iso(when), "value": value}


def make_snapshot(record, alias, target, peer_target, states, branch):
    peer = target["excluded_cluster"]
    service, root = target["service"], target["root_service"]
    env_id, peer_env = target["environment_id"], peer_target["environment_id"]
    observation = json.loads(record["observation"])
    check_metrics(observation, service, env_id)
    if (observation["window_end"] < target["injected_at"]
            or environment.classify(observation) != "abnormal"):
        raise ValueError("Patrol alert is outside the declared fault")

    front = telemetry(states[alias], service, env_id)
    upstream = telemetry(states[alias], root, env_id)
    decoy = telemetry(states[peer], service, peer_env)
    root_metrics = environment.get_snapshot(states[alias], root)
    decoy_metrics = environment.get_snapshot(states[peer], service)
    check_metrics(root_metrics, root, env_id)
    check_metrics(decoy_metrics, service, peer_env)
    if front["upstream_service"] != root or decoy["upstream_service"] is not None:
        raise ValueError("Observed HTTP dependency does not match the declared topology")

    spans = {event["trace_id"]: event for event in front["traces"]}
    scoped_logs = [event for event in upstream["logs"] if event.get("caller_service") == service]
    root_logs = {event["trace_id"]: event for event in scoped_logs}
    if len(spans) != len(front["traces"]) or len(root_logs) != len(scoped_logs):
        raise ValueError("Duplicate correlation ID in a service telemetry stream")
    matched = None
    for probe in branch["probes"]:
        if probe["http_status"] < 500 or probe.get("trace_id") not in spans:
            continue
        span = spans[probe["trace_id"]]
        if (probe["environment_id"] != env_id or probe["service"] != service
                or not branch["skill_item_id"] < probe["item_id"] < branch["report_item_id"]
                or span["upstream_service"] != root
                or span["status"] != span["upstream_status"] or span["status"] != probe["http_status"]
                or not target["injected_at"] <= span["started_at"] <= span["time"] <= probe["checked_at"]):
            raise ValueError("Fork probe does not match its scoped downstream span")
        log = root_logs.get(span["trace_id"])
        if log is None:
            continue
        if (log["status"] != span["upstream_status"] or log["fault_mode"] != target["mode"]
                or not span["started_at"] <= log["time"] <= span["time"]):
            raise ValueError("Root log does not match the probe and downstream response")
        matched = probe, span, log
        break

    decoy_failure = next((log for log in reversed(decoy["logs"])
                          if not log.get("caller_service") and log["status"] >= 500
                          and peer_target["decoy_injected_at"] <= log["time"]), None)
    if (matched is None or decoy_failure is None
            or not root_metrics["error_rate"] or not decoy_metrics["error_rate"]):
        return None, None, "缺少绑定分支探测的依赖证据，或缺少同名服务的独立故障证据。"
    if (root_metrics["window_end"] < target["injected_at"]
            or decoy_metrics["window_end"] < peer_target["decoy_injected_at"]):
        raise ValueError("Dependency or decoy metrics predate the declared faults")

    probe, span, log = matched
    qualified_root = f"{alias}/{root}"
    records = [
        row("target-alert", "alerts", alias, service, observation["window_end"], observation),
        row("root-metrics", "metrics", alias, root, root_metrics["window_end"], root_metrics),
        row("root-log", "logs", alias, root, log["time"], log),
        row("target-span", "traces", alias, service, span["time"], {**span, "upstream": qualified_root}),
        row("target-dependency", "dependencies", alias, service, span["time"],
            {"upstream": [qualified_root], "trace_id": span["trace_id"]}),
        row("peer-alert", "alerts", peer, service, decoy_metrics["window_end"], decoy_metrics),
        row("peer-log", "logs", peer, service, decoy_failure["time"], decoy_failure),
    ]
    times = [record["time"] for record in records]
    coverage = {"start": min(times), "end": max(times), "complete": True}
    snapshot = {"incident_id": record["incident_id"], "snapshot_id": record["incident_id"],
                "version": SNAPSHOT_VERSION, "coverage": coverage, "rows": records,
                "manifest": {"clusters": {alias: list(KINDS), peer: ["alerts", "logs"]}}}
    return snapshot, {"session_id": branch["session_id"], "item_id": probe["item_id"],
                      "trace_id": span["trace_id"]}, None


def capture(directory, plan, environments, proofs):
    """Capture bounded evidence while both faults still exist."""
    plan_path = directory / "fault-plan.json"
    index = plan["round"]
    source = fleet_round(directory, index) / "collection/patrol-evidence.json"
    targets = plan["targets"]
    if (read(plan_path) != plan or read(source) != proofs or index not in (1, 2)
            or targets.keys() != environments.keys() or targets.keys() != proofs.keys() or len(targets) != 2):
        raise ValueError("Enterprise capture disagrees with the frozen fault plan or source receipts")
    cases = {}
    for alias, target in targets.items():
        peer = target["excluded_cluster"]
        peer_target = targets[peer]
        service, env_id = target["service"], target["environment_id"]
        if (peer == alias or env_id == peer_target["environment_id"]
                or service != peer_target["decoy_service"]
                or environment.TRACE_DEPENDENCIES[service] != target["root_service"]):
            raise ValueError("Fault plan does not separate target and decoy scopes")
        proof = proofs[alias]
        record, branch = proof["incident"], proof["investigation"]
        if (branch["session_id"] == record["session_id"] or branch["source_session_id"] != record["session_id"]
                or branch["source_report_item_id"] != record["report_item_id"]
                or branch["skill_sha256"] != record["skill_sha256"]
                or (branch["incident_id"], branch["service"], branch["environment_id"])
                != (record["incident_id"], service, env_id)):
            raise ValueError("The investigation branch belongs to another incident")
        snapshot, linked_probe, pending = make_snapshot(record, alias, target, peer_target, environments, branch)
        cases[alias] = {"incident_id": record["incident_id"], "snapshot": snapshot,
                        "linked_probe": linked_probe, "pending": pending}
    sealed(directory / "capture.json", {"plan_sha256": file_digest(plan_path),
                                       "source_sha256": file_digest(source), "cases": cases})


def finish_round(directory, lifecycle, decoy_after):
    """Confirm the separately recorded repair and both recovery windows."""
    plan_path = directory / "fault-plan.json"
    plan = read(plan_path)
    run = fleet_round(directory, plan["round"])
    source = run / "incident-lifecycle.json"
    patrol_path = run / "collection/patrol-evidence.json"
    captured, capture_sha = read_sealed(directory / "capture.json")
    if (captured["plan_sha256"] != file_digest(plan_path)
            or captured["source_sha256"] != file_digest(patrol_path) or read(source) != lifecycle):
        raise ValueError("The independent recovery record or fault plan does not match its source")
    proofs = read(patrol_path)
    entries = {entry["id"]: entry for entry in lifecycle["incidents"]}
    completed = {}
    for alias, target in plan["targets"].items():
        incident = captured["cases"][alias]["incident_id"]
        service, env_id = target["service"], target["environment_id"]
        scope = f"{alias}/{env_id}/{service}"
        events = {event["key"]: event for event in lifecycle["events"] if event["incident"] == incident}
        entry = entries[incident]
        trigger = events[incident + "-trigger"]
        if (trigger["kind"] != "observation" or trigger["body"]["scope"] != scope
                or trigger["body"]["quality"] != "unhealthy"
                or trigger["body"]["evidence"] != json.loads(proofs[alias]["incident"]["observation"])):
            raise ValueError("Lifecycle trigger does not match the captured patrol alert")
        approval, execution = events[incident + "-approved"], events[incident + "-execute"]
        approved, applied = approval["body"], execution["body"]
        effect = applied["effect"]
        if (entry["scope"] != scope or entry["state"] != "CLOSED"
                or entry["healthy_windows"] != 2 or not approved["actor"]
                or approval["kind"] != "action_approved" or execution["kind"] != "action_executed"
                or approved["scope"] != scope or applied["scope"] != scope
                or approved["operation"] != applied["operation"] or applied["status"] != "applied"
                or applied["action"] != "clear_lab_fault" or effect["fault"] != f"{service}.fault"
                or effect["already_clear"] or effect["cleared_at"] <= target["injected_at"]):
            raise ValueError("The target incident lacks a matching independently approved repair")
        snapshot = captured["cases"][alias]["snapshot"]
        if snapshot and snapshot["coverage"]["end"] > iso(effect["cleared_at"]):
            raise ValueError("The capture includes observations made after the repair")
        boundary = effect["cleared_at"]
        for index in (1, 2):
            window = events[f"{incident}-recovery-{index}"]["body"]
            metrics = window["evidence"]
            check_metrics(metrics, service, env_id)
            if (window["scope"] != scope or window["source"] != "verification"
                    or window["quality"] != "healthy" or metrics["error_rate"] != 0
                    or environment.classify(metrics) != "healthy" or metrics["window_start"] <= boundary):
                raise ValueError("Recovery windows are not distinct and healthy in the target scope")
            boundary = metrics["window_end"]
        peer = target["excluded_cluster"]
        decoy_metrics = decoy_after[peer]
        check_metrics(decoy_metrics, service, plan["targets"][peer]["environment_id"])
        if decoy_metrics["window_end"] < boundary or decoy_metrics["window_start"] <= effect["cleared_at"]:
            raise ValueError("The independent decoy was not observed after target recovery")
        completed[alias] = {"approval": approval["key"], "execution": execution["key"],
                            "recovery_end": boundary, "decoy_after": decoy_metrics}
    sealed(directory / "recovery-evidence.json", {"capture_sha256": capture_sha,
                                                 "source_sha256": file_digest(source), "completed": completed})


def build_pack(root):
    """Materialize the two rounds with the same oncall-pack-v3 builder as Lesson 19."""
    source_path, annotations_path = root / "rollouts.json", root / "annotations.json"
    snapshots_dir = root / "snapshots"
    if any(path.exists() for path in (source_path, annotations_path, snapshots_dir, root / "prepared")):
        raise FileExistsError("The enterprise case pack already exists")
    source = {"origin": {"kind": "capstone_live_lab",
                         "description": "本机实验 HTTP 证据和 Hermes SessionDB 回执。"}, "traces": []}
    annotations = {}
    snapshots = {}
    incidents_seen = set()
    previous = {}
    for index, split in ((1, "dev"), (2, "holdout")):
        directory = root / f"round-{index}"
        run = fleet_round(directory, index)
        plan_path = directory / "fault-plan.json"
        proofs_path = run / "collection/patrol-evidence.json"
        lifecycle_path = run / "incident-lifecycle.json"
        plan, proofs = read(plan_path), read(proofs_path)
        capture, capture_sha = read_sealed(directory / "capture.json")
        recovery, _ = read_sealed(directory / "recovery-evidence.json")
        if (plan["round"] != index or capture["plan_sha256"] != file_digest(plan_path)
                or capture["source_sha256"] != file_digest(proofs_path)
                or recovery["source_sha256"] != file_digest(lifecycle_path)
                or recovery["capture_sha256"] != capture_sha):
            raise ValueError("An enterprise source changed after collection")
        for alias, case in capture["cases"].items():
            target = plan["targets"][alias]
            if min(target["injected_at"], target["decoy_injected_at"]) <= previous.get(alias, 0):
                raise ValueError("Later incident overlaps the earlier recovery window")
            independent = recovery["completed"][alias]
            previous[alias] = independent["recovery_end"]
            incident, snapshot = case["incident_id"], case["snapshot"]
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", incident) or incident in incidents_seen:
                raise ValueError("Invalid or reused incident across evaluation splits")
            incidents_seen.add(incident)
            record, branch = proofs[alias]["incident"], proofs[alias]["investigation"]
            observation = json.loads(record["observation"])
            coverage = snapshot["coverage"] if snapshot else {
                "start": iso(observation["window_start"]), "end": iso(observation["window_end"]),
            }
            scope = {"cluster": alias, "service": target["service"], "start": coverage["start"], "end": coverage["end"]}
            prompt = "CAPSTONE_SCOPE " + json.dumps(scope, ensure_ascii=False, sort_keys=True) + "\n" + (
                "调查范围内的服务告警，查证根因、影响服务及其他集群中同名服务的独立故障。"
                "用 monitor_query 查询并引用告警、指标、日志、调用链、依赖和排除证据。"
                "仅调查。以 JSON 回答 root_cause、affected_service、excluded_cluster、evidence_ids 和 explanation。"
                "前两项使用 集群/服务，excluded_cluster 使用集群名。"
            )
            metadata = {"incident_id": incident, "cluster": alias, "snapshot_id": incident, "split": split,
                        "scenario": "live-cross-cluster-dependency", "skill_version": record["skill_sha256"],
                        "probe_link": case["linked_probe"], "pending_reason": case["pending"],
                        "receipt": f"round-{index}/collection/patrol-evidence.json"}
            for receipt, output in ((record, proofs[alias]["report"]), (branch, branch["report"])):
                source["traces"].append({
                    "id": f"session:{receipt['session_id']}:item:{receipt['report_item_id']}",
                    "input": {"prompt": prompt}, "output": output, "metadata": metadata,
                })
            if snapshot is None:
                continue
            snapshots[incident] = snapshot
            decoy_after = independent["decoy_after"]
            if environment.classify(decoy_after) != "abnormal" or not decoy_after["error_rate"]:
                continue
            annotations[f"{incident}@{alias}"] = {
                "status": "reviewed", "reviewer": "lab-control-plane",
                "snapshot_id": incident, "snapshot_version": SNAPSHOT_VERSION,
                "basis": [
                    {"kind": "incident_timeline", "ref": f"enterprise/round-{index}/fault-plan.json",
                     "text": "故障注入计划在调查前冻结，目标依赖和同名服务各有独立开关。"},
                    {"kind": "repair_record", "ref": f"round-{index}/incident-lifecycle.json#{independent['execution']}",
                     "text": "批准后清除目标故障，随后取得两个独立健康窗口；另一个集群同名服务仍异常。"},
                ],
                "expected_output": {"root_cause": f"{alias}/{target['root_service']}",
                                    "affected_service": f"{alias}/{target['service']}",
                                    "excluded_cluster": target["excluded_cluster"]},
                "required_evidence": ["target-alert", "root-metrics", "root-log", "target-span", "target-dependency"],
                "required_exclusion_evidence": ["peer-alert", "peer-log"],
            }
    snapshots_dir.mkdir()
    for incident, snapshot in snapshots.items():
        environment.write_json(snapshots_dir / f"{incident}.json", snapshot)
    environment.write_json(source_path, source)
    environment.write_json(annotations_path, annotations)
    pack = write_pack(source_path, annotations_path, snapshots_dir, root / "prepared")
    verify(pack)
    return pack
