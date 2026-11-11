"""Frozen lesson-23 tasks and an evidence-based candidate gate."""

from __future__ import annotations

import hashlib
import json

SKILL = "timeout-triage"
SCOPE = {"service": "payment-api", "environment": "lab", "region": "local"}
TOOLS = ("query_service_health", "query_upstream_latency", "query_pool_config", "query_pool_effective")
CASES = ("drift", "upstream")
METHOD = "配置文件与实例生效值"


def snapshots(case):
    if case not in CASES:
        raise ValueError("Unknown frozen lesson-23 case")
    return {
        "query_service_health": {"healthy": True},
        "query_upstream_latency": {"p95_ms": 1600 if case == "upstream" else 120},
        "query_pool_config": {"max_connections": 120},
        "query_pool_effective": {"max_connections": 60, "active": 60, "waiting_requests": 24},
    }


def prompt(role, case):
    snapshots(case)
    if role not in {"source", "receiver"}:
        raise ValueError("Unknown task role")
    task = {"scope": SCOPE, "task": "完成一轮只读超时初查；先读取 timeout-triage，按已返回的回执选择分支。",
            "extra_investigation": role == "source"}
    if role == "source":
        task["supplement"] = "如果健康和上游都正常，本次额外查询配置文件与实例生效的连接池上限，比较实际结果。这个显式补查用于给后台提供方法缺口的材料。"
    return "LESSON23_TASK " + json.dumps(task, ensure_ascii=False, sort_keys=True)


def report_for(receipts):
    upstream = receipts.get("query_upstream_latency", {})
    configured, effective = receipts.get("query_pool_config"), receipts.get("query_pool_effective")
    assessment = "insufficient_evidence"
    if upstream.get("p95_ms", 0) > 1000:
        assessment = "upstream_latency_observed"
    elif configured and effective and configured["max_connections"] != effective["max_connections"]:
        assessment = "configuration_drift_observed"
    return {"assessment": assessment, "root_cause": "unknown", "observations": receipts}


def validate_task(messages, calls, case, expected_sha):
    from skillclaw_lab.observations import canonical, tool_events

    events = tool_events(messages)
    query = [event for event in events if event["tool"] in TOOLS]
    fixture = snapshots(case)
    expected_calls = [{"tool": event["tool"], "arguments": event["arguments"], "result": event["result"]}
                      for event in query]
    if sorted(map(canonical, expected_calls)) != sorted(map(canonical, calls)):
        raise ValueError("Native messages differ from actual tool-handler receipts")
    if not query or any(event["arguments"] != SCOPE or event["result"] != fixture[event["tool"]] for event in query):
        raise ValueError("Tool receipt differs from the frozen scoped fixture")
    if any(event["tool"] not in {*TOOLS, "skill_view", "skills_list"} for event in events):
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
    final = messages[-1] if messages else {}
    if final.get("role") != "assistant" or final.get("tool_calls"):
        raise ValueError("Task has no final persisted report")
    report = json.loads(final.get("content", ""))
    receipts = {e["tool"]: e["result"] for e in query}
    if report != report_for(receipts):
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
            "conditional_queries_ordered": ordered, "report": report}


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
    if len(set(sessions)) != len(sessions):
        return {"decision": "REJECT", "reason": "shared_evaluation_session"}
    if any(row[arm]["score"]["status"] not in {"pass", "fail"}
           for row in rows for arm in ("baseline", "candidate")):
        return {"decision": "REJECT", "reason": "unjudgeable_cases"}
    changed = baseline_sha != candidate_sha
    regression = any(row["baseline"]["score"]["passed"] and not row["candidate"]["score"]["passed"] for row in rows)
    improved = [row["case"] for row in rows if not row["baseline"]["score"]["passed"] and row["candidate"]["score"]["passed"]]
    accepted = changed and not regression and all(row["candidate"]["score"]["passed"] for row in rows) and bool(improved)
    return {"decision": "APPROVE" if accepted else "REJECT", "reason": "verified_improvement" if accepted else "no_verified_gain",
            "improved": improved, "regression": regression}
