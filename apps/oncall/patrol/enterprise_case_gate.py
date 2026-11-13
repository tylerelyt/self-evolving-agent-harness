"""Score the capstone's business boundaries against a frozen monitor snapshot."""

import json


SCORER_VERSION = "oncall-business-boundaries-v1"
OUTPUT_INSTRUCTION = (
    "仅输出一个 JSON 对象，字段为 root_cause、affected_services、excluded_services、"
    "needed_evidence、evidence_ids 和 explanation。根因明确时用 集群/服务，"
    "证据不足时 root_cause 为 null。affected_services 和 excluded_services 为 集群/服务 数组。"
    "needed_evidence 为待补查的 {kind, cluster, service} 对象数组，其中 service 只填服务名。"
    "evidence_ids 只引用实际查到且属于目标事故的记录。explanation 用简短文字说明判断。"
)
OUTPUT_KEYS = {"root_cause", "affected_services", "excluded_services", "needed_evidence"}
SELECTOR_KEYS = ("kind", "cluster", "service")


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def string_set(values):
    if not isinstance(values, list) or any(not nonempty(value) for value in values):
        raise ValueError("Expected an array of nonempty strings")
    result = set(values)
    if len(result) != len(values):
        raise ValueError("Duplicate string in array")
    return result


def selectors(values, keys=SELECTOR_KEYS):
    if not isinstance(values, list):
        raise ValueError("Expected an array of query selectors")
    result = set()
    for value in values:
        if (not isinstance(value, dict) or set(value) != set(keys)
                or any(not nonempty(value[key]) for key in keys)):
            raise ValueError("Invalid query selector")
        result.add(tuple(value[key] for key in keys))
    if len(result) != len(values):
        raise ValueError("Duplicate query selector")
    return result


def validate_case(case):
    """Validate independent annotation and return the monitor used by the replay."""
    from enterprise_replay import lesson_module, task_scope

    monitor = lesson_module("monitor")
    snapshot = case["snapshot"]
    coverage = monitor.scope(snapshot)
    prompt = case["input"]["prompt"]
    scope = task_scope(prompt)
    incident_header = prompt.partition("\n")[2].partition("\n")[0]
    cluster, service = scope["cluster"], scope["service"]
    start, end = scope["start"], scope["end"]
    begin, finish = monitor.timestamp(start), monitor.timestamp(end)
    if ({cluster} != set(coverage["clusters"]) or start != coverage["start"] or end != coverage["end"]
            or not nonempty(snapshot["incident_id"]) or incident_header != f"CAPSTONE_INCIDENT {snapshot['incident_id']}"):
        raise ValueError("Business task scope does not match its frozen incident")

    rows = {}
    for row in snapshot["rows"]:
        identifier = row["id"]
        if (not nonempty(identifier) or identifier in rows or not nonempty(row["incident_id"])
                or not nonempty(row["service"]) or not begin <= monitor.timestamp(row["time"]) <= finish):
            raise ValueError("Invalid incident row or row outside the capture window")
        rows[identifier] = row
    foreign = {identifier for identifier, row in rows.items() if row["incident_id"] != snapshot["incident_id"]}

    annotation = case["annotation"]
    expected = annotation["expected_output"]
    if not isinstance(expected, dict) or set(expected) != OUTPUT_KEYS:
        raise ValueError("Invalid business output annotation")
    prefix = f"{cluster}/"
    root = expected["root_cause"]
    affected = string_set(expected["affected_services"])
    excluded = string_set(expected["excluded_services"])
    required = string_set(annotation["required_evidence"])
    forbidden = string_set(annotation["forbidden_evidence"])
    if (f"{cluster}/{service}" not in affected or affected & excluded
            or any(not item.startswith(prefix) or not nonempty(item.removeprefix(prefix)) for item in affected)
            or forbidden != foreign or not required or not required <= rows.keys() - foreign):
        raise ValueError("Annotation mixes the target and foreign incident evidence")

    needed = selectors(expected["needed_evidence"])
    queries = selectors(annotation["required_queries"], (*SELECTOR_KEYS, "status"))
    unavailable = {(kind, region, target) for kind, region, target, status in queries if status == "not_covered"}
    covered_ids = set()
    for kind, region, target, status in queries:
        if region != cluster:
            raise ValueError("Required query is outside the target cluster")
        receipt = monitor.query(snapshot, kind, region, start, end, target)
        if receipt["status"] != status:
            raise ValueError("Required query status does not match the snapshot")
        covered_ids.update(row["id"] for row in receipt.get("rows", []))
    if not (required | forbidden) <= covered_ids or needed != unavailable:
        raise ValueError("Annotations are unsupported by the required queries")

    target_scopes = {(rows[key]["kind"], cluster, rows[key]["service"], "covered") for key in required}
    if case["scenario"] == "concurrent_incident":
        if (not nonempty(root) or not root.startswith(prefix) or not nonempty(root.removeprefix(prefix))
                or needed or not foreign or excluded != {f"{cluster}/{rows[key]['service']}" for key in foreign}
                or root in excluded):
            raise ValueError("Concurrent case must separate a confirmed root from the independent incident")
        root_service = root.removeprefix(prefix)
        causal_scopes = {(kind, cluster, service, "covered") for kind in ("alerts", "traces", "dependencies")}
        causal_scopes.update((kind, cluster, root_service, "covered") for kind in ("metrics", "logs"))
        if not causal_scopes <= target_scopes & queries:
            raise ValueError("Concurrent case lacks scoped evidence for a confirmed root")
        for qualified in excluded:
            decoy = qualified.removeprefix(prefix)
            decoy_kinds = {rows[key]["kind"] for key in foreign if rows[key]["service"] == decoy}
            if any(kind not in decoy_kinds or (kind, cluster, decoy, "covered") not in queries
                   for kind in ("alerts", "logs")):
                raise ValueError("Independent incident lacks a captured alert and log or their scoped queries")
    elif case["scenario"] == "insufficient_coverage":
        known_scopes = {(kind, cluster, service, "covered") for kind in ("alerts", "dependencies")}
        if (root is not None or excluded or foreign or ("traces", cluster, service) not in needed
                or not known_scopes <= target_scopes & queries
                or not any(kind == "logs" and ("metrics", cluster, target, "covered") in target_scopes & queries
                           for kind, _, target in needed)):
            raise ValueError("Abstention case must have uncovered causal evidence and an unknown root")
    else:
        raise ValueError("Unknown business scenario")
    return monitor


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON object field")
    return result


def grade(case, execution):
    from enterprise_replay import monitor_lookup

    review = {"scorer_version": SCORER_VERSION, "explanation_semantics": "not_evaluated"}
    unscored = {**review, "status": "unscored", "passed": False, "score": None}
    if not isinstance(execution, dict) or execution.get("status") != "completed":
        return {**unscored, "errors": ["Hermes 执行未正常完成"]}
    try:
        monitor = validate_case(case)
        snapshot = case["snapshot"]
        coverage = snapshot["coverage"]
        begin, finish = monitor.timestamp(coverage["start"]), monitor.timestamp(coverage["end"])
        annotation = case["annotation"]
        if not isinstance(execution["receipts"], list) or not isinstance(execution["final_message"], str):
            raise ValueError("Invalid execution record")
        required_queries = selectors(annotation["required_queries"], (*SELECTOR_KEYS, "status"))
        observed, scoped_rows, queried = set(), set(), set()
        for receipt in execution["receipts"]:
            request = receipt["query"]
            actual = monitor_lookup(monitor.query, snapshot, request)
            if actual != receipt:
                raise ValueError("Tool receipt disagrees with the frozen snapshot")
            if "status" not in actual:
                continue
            identifiers = {row["id"] for row in actual.get("rows", [])}
            observed.update(identifiers)
            query_key = tuple(actual["query"][key] for key in SELECTOR_KEYS) + (actual["status"],)
            if (query_key in required_queries and begin <= monitor.timestamp(request["start"])
                    <= monitor.timestamp(request["end"]) <= finish):
                queried.add(query_key)
                scoped_rows.update(identifiers)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        return {**unscored, "errors": [f"现场标注或工具回执无法核验：{exc}"]}

    errors = []
    if not required_queries <= queried:
        errors.append("缺少所需范围内的真实查询回执")
    if not set(annotation["forbidden_evidence"]) <= scoped_rows:
        errors.append("同集群独立事故的告警或日志尚未查到")
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
        if selectors(answer["needed_evidence"]) != selectors(expected["needed_evidence"]):
            errors.append("needed_evidence 未列出待补查的范围")
        cited = string_set(answer["evidence_ids"])
        if not (set(annotation["required_evidence"]) <= cited & scoped_rows and cited <= observed):
            errors.append("缺少已查到的必要引用，或引用了未查到的证据")
        if cited & set(annotation["forbidden_evidence"]):
            errors.append("引用混入其他事故的证据")
        if not nonempty(answer["explanation"]):
            errors.append("缺少判断说明")
    except (KeyError, ValueError, TypeError):
        errors.append("回答格式无效")
    return {**review, "status": "scored", "passed": not errors, "score": int(not errors), "errors": errors}
