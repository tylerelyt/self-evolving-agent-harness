"""Local model for the shared-root Skill comparison through real Hermes tools."""

from enterprise_case_fixture import monitor_receipts, respond as baseline_respond
from enterprise_replay import failed_trace_ids, task_scope


FANOUT_RULE = (
    "同一工单出现多个下游告警时，逐个服务核对告警、依赖和失败调用链。"
    "每条失败链都要用自己的关联号匹配同一上游的错误日志，再报告完整影响集合。"
    "检查同集群健康服务的指标作为对照，单独列出该服务，只引用当前工单证据。"
)


def respond(messages):
    """Use the Skill rule to choose an investigation path, without receiving the answer."""
    if not any(message["role"] == "system" and FANOUT_RULE in message["content"] for message in messages):
        return baseline_respond(messages)

    prompt = next(message["content"] for message in messages if message["role"] == "user")
    scope = task_scope(prompt)
    incident = prompt.split("\n", 2)[1].removeprefix("CAPSTONE_INCIDENT ")
    cluster = scope["cluster"]
    interval = {"start": scope["start"], "end": scope["end"]}
    receipts = {(item["query"]["kind"], item["query"]["service"]): item for item in monitor_receipts(messages)}

    def request(kind, service=None):
        arguments = {**interval, "kind": kind, "cluster": cluster}
        if service is not None:
            arguments["service"] = service
        return "monitor_query", arguments

    def target_rows(kind, service):
        return [item for item in receipts[kind, service].get("rows", []) if item["incident_id"] == incident]

    if ("alerts", None) not in receipts:
        return request("alerts")
    services = sorted({item["service"] for item in target_rows("alerts", None)
                       if (item["value"].get("error_rate") or 0) > 0})
    queries = [(kind, service) for service in services for kind in ("alerts", "dependencies", "traces")]
    for kind, service in queries:
        if (kind, service) not in receipts:
            return request(kind, service)

    upstreams = [{upstream for item in target_rows("dependencies", service) for upstream in item["value"]["upstream"]
                  if upstream.startswith(cluster + "/") and failed_trace_ids(target_rows("traces", service), upstream)}
                 for service in services]
    common = set.intersection(*upstreams) if upstreams else set()
    root = None
    if len(common) == 1:
        qualified = next(iter(common))
        root_service = qualified.split("/", 1)[1]
        for kind in ("metrics", "logs"):
            if (kind, root_service) not in receipts:
                return request(kind, root_service)
            queries.append((kind, root_service))
        logs = target_rows("logs", root_service)
        linked = [{item["value"]["trace_id"] for item in target_rows("traces", service)
                   if item["value"]["trace_id"] in failed_trace_ids(target_rows("traces", service), qualified)
                   and any(log["value"]["trace_id"] == item["value"]["trace_id"]
                           and log["value"]["caller_service"] == service
                           and log["value"]["status"] == item["value"]["status"] for log in logs)}
                  for service in services]
        if (len(linked) >= 2 and all(linked) and len(set.union(*linked)) == sum(map(len, linked))
                and any((item["value"].get("error_rate") or 0) > 0 for item in target_rows("metrics", root_service))):
            root = qualified
    if ("metrics", None) not in receipts:
        return request("metrics")
    controls = {item["service"] for item in receipts["metrics", None].get("rows", [])
                if item["incident_id"] != incident and item["service"] not in services
                and item["value"].get("error_rate") == 0 and item["value"].get("requests", 0) >= 10}
    excluded = []
    for service in sorted(controls):
        if ("metrics", service) not in receipts:
            return request("metrics", service)
        if any(item["incident_id"] != incident and item["value"].get("error_rate") == 0
               for item in receipts["metrics", service].get("rows", [])):
            excluded.append(f"{cluster}/{service}")
    evidence = list(dict.fromkeys(item["id"] for query in queries for item in target_rows(*query)))
    explanation = ("各下游的失败调用用不同关联号连接到同一上游的错误日志，控制服务窗口无错误。"
                   if root else "现有链路证据不足以确认一个共同的上游根因。")
    return None, {"root_cause": root, "affected_services": [f"{cluster}/{service}" for service in services],
                  "excluded_services": excluded, "needed_evidence": [], "evidence_ids": evidence,
                  "explanation": explanation}
