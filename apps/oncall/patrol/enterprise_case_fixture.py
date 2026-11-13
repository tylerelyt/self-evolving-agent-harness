"""Deterministic model for wiring tests of the capstone enterprise case protocol."""

import json


INCIDENT_PREFIX = "CAPSTONE_INCIDENT "


def monitor_receipts(messages):
    return [json.loads(message["content"]) for message in messages if message["role"] == "tool"]


def respond(messages):
    """Request real monitor lookups and decide from the task and returned rows."""
    from enterprise_replay import failed_trace_ids, task_scope

    prompt = next(message["content"] for message in messages if message["role"] == "user")
    scope = task_scope(prompt)
    incident_header = prompt.partition("\n")[2].partition("\n")[0]
    incident = incident_header.removeprefix(INCIDENT_PREFIX)
    if incident == incident_header or not incident or any(character.isspace() for character in incident):
        raise ValueError("Expected a capstone incident ID on the second line")
    cluster, service = scope["cluster"], scope["service"]
    interval = {key: scope[key] for key in ("start", "end")}
    receipts = {}
    for receipt in monitor_receipts(messages):
        query = receipt["query"]
        if query["cluster"] == cluster and all(query[key] == value for key, value in interval.items()):
            receipts[query["kind"], query.get("service")] = receipt

    def request(kind, target=None):
        # Lesson 19 exposes an optional string service. Omitting it queries the whole cluster.
        arguments = {**interval, "kind": kind, "cluster": cluster}
        if target is not None:
            arguments["service"] = target
        return "monitor_query", arguments

    def rows(kind, target, event=incident):
        receipt = receipts[kind, target]
        if receipt["status"] != "covered":
            return []
        return [row for row in receipt["rows"] if row["incident_id"] == event]

    target_queries = [(kind, service) for kind in ("alerts", "traces", "dependencies")]
    for query in target_queries:
        if query not in receipts:
            return request(*query)

    traces = rows("traces", service)
    dependencies = rows("dependencies", service)
    upstreams = {upstream for row in dependencies for upstream in row["value"]["upstream"]
                 if upstream.startswith(cluster + "/")}
    if len(upstreams) > 1:
        upstreams = {upstream for upstream in upstreams if failed_trace_ids(traces, upstream)}
    upstream = next(iter(upstreams)) if len(upstreams) == 1 else None
    linked_failures = failed_trace_ids(traces, upstream) if upstream else set()
    confirmed = False
    if upstream:
        root_service = upstream.split("/", 1)[1]
        for kind in ("metrics", "logs"):
            query = kind, root_service
            if query not in receipts:
                return request(*query)
            target_queries.append(query)
        failure_statuses = {row["value"]["trace_id"]: row["value"]["status"] for row in traces
                            if row["value"].get("trace_id") in linked_failures}
        confirmed = (any((row["value"].get("error_rate") or 0) > 0 for row in rows("metrics", root_service))
                     and any(log["value"].get("trace_id") in failure_statuses
                             and log["value"]["status"] == failure_statuses[log["value"]["trace_id"]]
                             and log["value"].get("caller_service") == service for log in rows("logs", root_service)))
    if ("alerts", None) not in receipts:
        return request("alerts")
    active_alerts = [row for row in receipts["alerts", None].get("rows", [])
                     if (row["value"].get("error_rate") or 0) > 0]
    affected = {row["service"] for row in active_alerts if row["incident_id"] == incident}
    distractors = sorted({(row["service"], row["incident_id"]) for row in active_alerts
                          if row["incident_id"] != incident and row["service"] not in affected
                          and f"{cluster}/{row['service']}" != upstream})
    excluded = set()
    for other_service, other_incident in distractors:
        for kind in ("alerts", "logs"):
            if (kind, other_service) not in receipts:
                return request(kind, other_service)
        if (any((row["value"].get("error_rate") or 0) > 0 for row in rows("alerts", other_service, other_incident))
                and any(row["value"].get("status", 0) >= 500 and row["value"].get("caller_service") is None
                        and row["value"].get("trace_id") not in linked_failures
                        for row in rows("logs", other_service, other_incident))):
            excluded.add(other_service)

    missing = [{"kind": kind, "cluster": cluster, "service": target} for kind, target in target_queries
               if receipts[kind, target]["status"] == "not_covered"]
    root = upstream if confirmed and not missing and service in affected else None
    evidence = list(dict.fromkeys(row["id"] for query in target_queries for row in rows(*query)))
    if root:
        explanation = "目标工单的失败调用链与上游错误日志具有相同关联号，上游指标也记录了失败。"
    elif missing:
        explanation = "现有回执无法核实根因，需要补充未覆盖的调用链或日志。"
    else:
        explanation = "现有回执不足以把失败归因到某个上游服务。"
    if excluded:
        explanation += "同集群的其他告警有不同的工单号，精确查询的日志显示是独立的直接请求故障。"
    return None, {"root_cause": root, "affected_services": [f"{cluster}/{name}" for name in sorted(affected)],
                  "excluded_services": [f"{cluster}/{name}" for name in sorted(excluded)],
                  "needed_evidence": missing, "evidence_ids": evidence, "explanation": explanation}
