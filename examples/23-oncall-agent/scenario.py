"""Frozen lesson-23 tasks and an evidence-based candidate gate."""

from __future__ import annotations

import hashlib
import json

SKILL = "timeout-triage"
SCOPE = {"service": "payment-api", "environment": "lab", "region": "local"}
TOOLS = ("query_service_health", "query_upstream_latency", "query_pool_config", "query_pool_effective")
CASES = ("drift", "upstream")
METHOD = "配置文件与实例生效值"
CONTEXT_TOOL = "read_external_context"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_scope(scope):
    if (not isinstance(scope, dict) or set(scope) != set(SCOPE)
            or any(not isinstance(value, str) or not value.strip() or len(value) > 120 for value in scope.values())):
        raise ValueError("Use an explicit service/environment/region scope")
    return dict(scope)


def snapshots(case, frozen=None):
    if case not in CASES:
        raise ValueError("Unknown frozen lesson-23 case")
    default = {
        "query_service_health": {"healthy": True},
        "query_upstream_latency": {"p95_ms": 1600 if case == "upstream" else 120},
        "query_pool_config": {"max_connections": 120},
        "query_pool_effective": {"max_connections": 60, "active": 60, "waiting_requests": 24},
    }
    if frozen is None:
        return default
    if not isinstance(frozen, dict) or set(frozen) != set(TOOLS):
        raise ValueError("Frozen task must provide exactly the lesson's four scene tools")
    if any(set(frozen[name]) != set(default[name]) for name in TOOLS):
        raise ValueError("Frozen task fields differ from the scene-tool schema")
    if (type(frozen["query_service_health"]["healthy"]) is not bool
            or any(type(value) not in (int, float) or value < 0
                   for name in TOOLS[1:] for value in frozen[name].values())):
        raise ValueError("Frozen task contains invalid telemetry")
    return json.loads(json.dumps(frozen))


def prompt(role, case, *, scope=None, task_id=None, context_required=False):
    snapshots(case)
    if role not in {"source", "receiver"}:
        raise ValueError("Unknown task role")
    task = {"scope": validate_scope(scope or SCOPE), "task": "完成一轮只读超时初查；先读取 timeout-triage，按已返回的回执选择分支。",
            "extra_investigation": role == "source", "context_required": context_required}
    if task_id:
        task["task_id"] = task_id
    if context_required:
        task["context_instruction"] = "读取Skill后，按服务范围检索外部值班手册，再查询当前现场。外部手册不是现场状态。"
    if role == "source":
        task["supplement"] = "如果健康和上游都正常，本次额外查询配置文件与实例生效的连接池上限，比较实际结果。这个显式补查用于给后台提供方法缺口的材料。"
    return "LESSON23_TASK " + json.dumps(task, ensure_ascii=False, sort_keys=True)


def report_for(receipts, context=None):
    upstream = receipts.get("query_upstream_latency", {})
    configured, effective = receipts.get("query_pool_config"), receipts.get("query_pool_effective")
    assessment = "insufficient_evidence"
    if upstream.get("p95_ms", 0) > 1000:
        assessment = "upstream_latency_observed"
    elif configured and effective and configured["max_connections"] != effective["max_connections"]:
        assessment = "configuration_drift_observed"
    report = {"assessment": assessment, "root_cause": "unknown", "observations": receipts}
    if context is not None:
        report["context_citation"] = {"bundle_sha256": context["bundle_sha256"], "receipt_sha256": digest(context["context"])}
    return report


def validate_task(messages, calls, case, expected_sha, *, scope=None, frozen_snapshot=None, expected_context=None):
    from skillclaw_lab.observations import canonical, tool_events

    events = tool_events(messages)
    query = [event for event in events if event["tool"] in TOOLS]
    scope = validate_scope(scope or SCOPE)
    fixture = snapshots(case, frozen_snapshot)
    expected_calls = [{"tool": event["tool"], "arguments": event["arguments"], "result": event["result"]}
                      for event in query]
    if sorted(map(canonical, expected_calls)) != sorted(map(canonical, calls)):
        raise ValueError("Native messages differ from actual tool-handler receipts")
    if not query or any(event["arguments"] != scope or event["result"] != fixture[event["tool"]] for event in query):
        raise ValueError("Tool receipt differs from the frozen scoped fixture")
    if any(event["tool"] not in {*TOOLS, CONTEXT_TOOL, "skill_view", "skills_list"} for event in events):
        raise ValueError("Unexpected tool was used")
    loaded = []
    for event in events:
        if event["tool"] != "skill_view":
            continue
        value = event["result"]
        body = value.get("raw_content", value.get("content")) if isinstance(value, dict) else None
        if event["arguments"].get("name") == SKILL and value.get("success") is True and isinstance(body, str):
            loaded.append(hashlib.sha256(body.encode()).hexdigest())
    if loaded != [expected_sha]:
        raise ValueError("Task must actually read exactly its pinned Skill content")
    skills = [e for e in events if e["tool"] == "skill_view"]
    if any(e["decision_index"] <= skills[0]["result_index"] for e in query):
        raise ValueError("Queries ran before the pinned Skill was read")
    context_events = [event for event in events if event["tool"] == CONTEXT_TOOL]
    if expected_context is None:
        if context_events:
            raise ValueError("Unexpected external-context receipt")
    elif (len(context_events) != 1 or context_events[0]["arguments"] != scope
          or context_events[0]["result"] != expected_context
          or context_events[0]["decision_index"] <= skills[0]["result_index"]
          or any(event["decision_index"] <= context_events[0]["result_index"] for event in query)):
        raise ValueError("External context must match the frozen OpenViking receipt before scene queries")
    final = messages[-1] if messages else {}
    if final.get("role") != "assistant" or final.get("tool_calls"):
        raise ValueError("Task has no final persisted report")
    report = json.loads(final.get("content", ""))
    receipts = {e["tool"]: e["result"] for e in query}
    if report != report_for(receipts, expected_context):
        raise ValueError("Final report differs from observed tool receipts")
    health = [e for e in query if e["tool"] == "query_service_health"]
    upstream = [e for e in query if e["tool"] == "query_upstream_latency"]
    config = [e for e in query if e["tool"] in {"query_pool_config", "query_pool_effective"}]
    ordered = bool(health and upstream and all(
        health[-1]["result_index"] < e["decision_index"] and upstream[-1]["result_index"] < e["decision_index"]
        for e in config))
    passed = (len(query) == 4 and set(receipts) == set(TOOLS) and ordered) if case == "drift" else (
        len(query) == 2 and set(receipts) == {"query_service_health", "query_upstream_latency"})
    return {"valid_trace": True, "passed": passed, "assessment": report["assessment"],
            "queries": [e["tool"] for e in query], "loaded_sha256": expected_sha,
            "conditional_queries_ordered": ordered, "report": report,
            "snapshot_sha256": digest(fixture), "context_sha256": digest(expected_context) if expected_context is not None else None,
            "scope": scope}


def score(trace, *, unavailable=False):
    if not trace.get("valid_trace"):
        return {"status": "execution_error", "passed": None}
    if unavailable:
        return {"status": "score_error", "passed": None,
                "reason": "Intentionally unavailable assessment exercises the publication gate; this is not a business failure."}
    return {"status": "pass" if trace["passed"] else "fail", "passed": trace["passed"]}


def decide(rows, baseline_sha, candidate_sha, *, source_sessions=()):
    if len(rows) != len(CASES) or {row["case"] for row in rows} != set(CASES):
        return {"decision": "REJECT", "reason": "incomplete_cases"}
    sessions = []
    for row in rows:
        for arm, expected in (("baseline", baseline_sha), ("candidate", candidate_sha)):
            result = row[arm]
            session = result.get("session_id")
            trace = result.get("trace", {})
            if (not isinstance(session, str) or not session or session in source_sessions
                    or trace.get("valid_trace") is not True or result.get("skill_sha256") != expected
                    or trace.get("loaded_sha256") != expected):
                return {"decision": "REJECT", "reason": "invalid_execution_evidence"}
            sessions.append(session)
        baseline_trace, candidate_trace = row["baseline"]["trace"], row["candidate"]["trace"]
        if (not baseline_trace.get("snapshot_sha256") or not baseline_trace.get("context_sha256")
                or baseline_trace.get("scope") != candidate_trace.get("scope")
                or baseline_trace.get("snapshot_sha256") != candidate_trace.get("snapshot_sha256")
                or baseline_trace.get("context_sha256") != candidate_trace.get("context_sha256")):
            return {"decision": "REJECT", "reason": "incomparable_frozen_inputs"}
    if len(set(sessions)) != len(sessions):
        return {"decision": "REJECT", "reason": "shared_evaluation_session"}
    if any(row[arm]["score"]["status"] not in {"pass", "fail"}
           for row in rows for arm in ("baseline", "candidate")):
        return {"decision": "REJECT", "reason": "unjudgeable_cases"}
    if any(type(row[arm]["trace"].get("passed")) is not bool
           or row[arm]["score"]["passed"] is not row[arm]["trace"]["passed"]
           or (row[arm]["score"]["status"] == "pass") is not row[arm]["score"]["passed"]
           for row in rows for arm in ("baseline", "candidate")):
        return {"decision": "REJECT", "reason": "inconsistent_business_score"}
    changed = baseline_sha != candidate_sha
    regression = any(row["baseline"]["score"]["passed"] and not row["candidate"]["score"]["passed"] for row in rows)
    improved = [row["case"] for row in rows if not row["baseline"]["score"]["passed"] and row["candidate"]["score"]["passed"]]
    accepted = changed and not regression and all(row["candidate"]["score"]["passed"] for row in rows) and bool(improved)
    return {"decision": "APPROVE" if accepted else "REJECT", "reason": "verified_improvement" if accepted else "no_verified_gain",
            "improved": improved, "regression": regression}
