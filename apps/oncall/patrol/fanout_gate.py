"""Score two downstream impacts against one shared upstream and a healthy control."""

import json
import re

from enterprise_case_gate import (
    OUTPUT_INSTRUCTION, OUTPUT_KEYS, SELECTOR_KEYS, nonempty, selectors, string_set, unique_object,
)
from enterprise_pack import iso
from environment import classify
from export_workset import scoped_snapshot


SCORER_VERSION = "oncall-shared-root-fanout-v1"
SCENARIO = "shared_root_fanout"


def validate_case(case):
    """Validate the bounded snapshot, causal links and independent annotation."""
    from enterprise_replay import lesson_module, task_scope

    monitor = lesson_module("monitor")
    snapshot = case["snapshot"]
    incident = snapshot["incident_id"]
    coverage = monitor.scope(snapshot)
    prompt = case["input"]["prompt"]
    scope = task_scope(prompt)
    incident_header = prompt.partition("\n")[2].partition("\n")[0]
    cluster, start, end = (scope[key] for key in ("cluster", "start", "end"))
    begin, finish = monitor.timestamp(start), monitor.timestamp(end)
    if (case["scenario"] != SCENARIO or not nonempty(incident) or {cluster} != set(coverage["clusters"])
            or (start, end) != (coverage["start"], coverage["end"])
            or incident_header != f"CAPSTONE_INCIDENT {incident}"):
        raise ValueError("Fanout task scope does not match its captured incident")

    annotation = case["annotation"]
    expected = annotation["expected_output"]
    if not isinstance(expected, dict) or set(expected) != OUTPUT_KEYS or expected["needed_evidence"] != []:
        raise ValueError("Fanout case requires a confirmed result and no missing evidence")
    root_qualified = expected["root_cause"]
    affected = string_set(expected["affected_services"])
    excluded = string_set(expected["excluded_services"])
    prefix = f"{cluster}/"
    if (not nonempty(root_qualified) or len(affected) != 2 or len(excluded) != 1
            or affected & excluded or root_qualified in affected | excluded
            or f"{cluster}/{scope['service']}" not in affected
            or any(not name.startswith(prefix) or not nonempty(name.removeprefix(prefix))
                   for name in affected | excluded | {root_qualified})):
        raise ValueError("Fanout requires one root, two downstreams and one separate control")
    root = root_qualified.removeprefix(prefix)
    downstreams = {name.removeprefix(prefix) for name in affected}
    control = next(iter(excluded)).removeprefix(prefix)

    rows = {}
    for row in snapshot["rows"]:
        identifier = row["id"]
        if (not nonempty(identifier) or identifier in rows or not nonempty(row["incident_id"])
                or not nonempty(row["service"])
                or not begin <= monitor.timestamp(row["time"]) <= finish):
            raise ValueError("Fanout snapshot contains duplicate rows or rows outside its window")
        rows[identifier] = row
    required = string_set(annotation["required_evidence"])
    control_ids = string_set(annotation["control_evidence"])
    foreign = {identifier for identifier, row in rows.items() if row["incident_id"] != incident}
    if len(control_ids) != 1 or control_ids != foreign or not required <= rows.keys() - foreign:
        raise ValueError("Healthy control evidence must be separate from target incident evidence")

    proof = {}
    for identifier in required | control_ids:
        row = rows[identifier]
        proof.setdefault((row["kind"], row["service"]), []).append(row)
    scopes = {(kind, downstream) for downstream in downstreams for kind in ("alerts", "traces", "dependencies")}
    scopes.update({("metrics", root), ("logs", root), ("metrics", control)})
    queries = selectors(annotation["required_queries"], (*SELECTOR_KEYS, "status"))
    if (proof.keys() != scopes or queries != {(kind, cluster, service, "covered") for kind, service in scopes}
            or any(len(records) != (2 if key == ("logs", root) else 1) for key, records in proof.items())
            or proof["metrics", control][0]["id"] not in control_ids):
        raise ValueError("Fanout lacks exact evidence and query scopes for both downstreams, root and control")

    control_row = proof["metrics", control][0]
    environment_id = control_row["value"]["environment_id"]
    if not nonempty(environment_id):
        raise ValueError("Fanout control has no environment identity")
    healthy = checked_metrics(control_row, environment_id)
    if classify(healthy) != "healthy" or healthy["error_rate"] != 0:
        raise ValueError("The independent control was not healthy")
    if checked_metrics(proof["metrics", root][0], environment_id)["error_rate"] <= 0:
        raise ValueError("The shared upstream has no observed failures")

    logs = {row["value"]["trace_id"]: row for row in proof["logs", root]}
    used_trace_ids = set()
    for service in downstreams:
        alert = checked_metrics(proof["alerts", service][0], environment_id)
        span_row = proof["traces", service][0]
        span, topology = span_row["value"], proof["dependencies", service][0]["value"]
        trace_id = span["trace_id"]
        if (classify(alert) != "abnormal" or alert["error_rate"] <= 0
                or not re.fullmatch(r"[0-9a-f]{32}", trace_id) or trace_id in used_trace_ids or trace_id not in logs
                or (span["service"], span["upstream_service"], span["upstream"], span["status"], span["upstream_status"])
                != (service, root, root_qualified, 503, 503)
                or topology["upstream"] != [root_qualified] or topology.get("trace_id", trace_id) != trace_id):
            raise ValueError("Affected downstream lacks a distinct failing trace to the shared root")
        log_row = logs[trace_id]
        log = log_row["value"]
        if ((log["service"], log["caller_service"], log["status"], log.get("fault_scope", root))
                != (root, service, 503, root) or not span["started_at"] <= log["time"] <= span["time"]
                or not healthy["window_start"] <= log["time"] <= healthy["window_end"]
                or span_row["time"] != iso(span["time"]) or log_row["time"] != iso(log["time"])):
            raise ValueError("Upstream log did not match its downstream trace and healthy control window")
        used_trace_ids.add(trace_id)
    return monitor, {row["id"] for row in logs.values()}


def checked_metrics(row, environment_id):
    metrics = scoped_snapshot(row["value"], row["service"], environment_id)
    if row["time"] != iso(metrics["window_end"]):
        raise ValueError("Fanout metric timestamp does not match its observation window")
    return metrics


def grade(case, execution):
    from enterprise_replay import monitor_lookup

    review = {"scorer_version": SCORER_VERSION, "explanation_semantics": "not_evaluated"}
    unscored = {**review, "status": "unscored", "passed": False, "score": None}
    if not isinstance(execution, dict) or execution.get("status") != "completed":
        return {**unscored, "errors": ["Hermes 执行未正常完成"]}
    try:
        monitor, root_logs = validate_case(case)
        snapshot, annotation = case["snapshot"], case["annotation"]
        required = set(annotation["required_evidence"])
        control = set(annotation["control_evidence"])
        queries = selectors(annotation["required_queries"], (*SELECTOR_KEYS, "status"))
        if not isinstance(execution["receipts"], list) or not isinstance(execution["final_message"], str):
            raise ValueError("Invalid execution record")
        observed, exact_rows, queried = set(), set(), set()
        both_root_logs = False
        for receipt in execution["receipts"]:
            actual = monitor_lookup(monitor.query, snapshot, receipt["query"])
            if actual != receipt:
                raise ValueError("Tool receipt disagrees with the frozen snapshot")
            if actual.get("status") != "covered":
                continue
            identifiers = {row["id"] for row in actual["rows"]}
            observed.update(identifiers)
            query_key = tuple(actual["query"][key] for key in SELECTOR_KEYS) + ("covered",)
            if query_key in queries:
                queried.add(query_key)
                exact_rows.update(identifiers)
                both_root_logs |= root_logs <= identifiers
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError) as exc:
        return {**unscored, "errors": [f"现场标注或工具回执无法核验：{exc}"]}

    errors = []
    if not queries <= queried or not (required | control) <= exact_rows or not both_root_logs:
        errors.append("未取得两个下游、共享上游及健康对照的精确查询回执")
    try:
        answer = json.loads(execution["final_message"], object_pairs_hook=unique_object)
        if not isinstance(answer, dict) or set(answer) != OUTPUT_KEYS | {"evidence_ids", "explanation"}:
            raise ValueError("Invalid final answer fields")
        expected = annotation["expected_output"]
        if answer["root_cause"] != expected["root_cause"]:
            errors.append("root_cause 不符")
        for field in ("affected_services", "excluded_services"):
            if string_set(answer[field]) != set(expected[field]):
                errors.append(f"{field} 不符")
        if answer["needed_evidence"] != []:
            errors.append("needed_evidence 应为空")
        cited = string_set(answer["evidence_ids"])
        if not required <= cited <= observed or cited & control:
            errors.append("引用应包含两个下游和上游已查到的证据，并与健康对照分开")
        if not nonempty(answer["explanation"]):
            errors.append("缺少判断说明")
    except (KeyError, ValueError, TypeError):
        errors.append("回答格式无效")
    return {**review, "status": "scored", "passed": not errors, "score": int(not errors), "errors": errors}
