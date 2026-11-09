#!/usr/bin/env python3
"""Evaluate or adopt the scoped recovery-window Skill in fresh Hermes sessions."""

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from environment import SERVICES
from hermes_runtime import ensure_loopback_no_proxy, install_oncall_skill, open_sessions, read_oncall_skill, run_turn
from patrol import decode_report, model_connection


SKILL = "oncall-recovery-window-check"
RULES = (
    "recovery_windows", "service and environment_id", "exactly two", "window_start > cleared_at",
    "first.window_end < second.window_start", "requests >= 10", "error_rate == 0", "p95_ms < 500",
)
WINDOW_FIELDS = {"source", "service", "environment_id", "window_start", "window_end", "requests", "error_rate", "p95_ms"}
EVIDENCE_FIELDS = {"incident_id", "scope", "source_session_id", "source_report_item_id", "cleared_at", "recovery_windows"}


def recovery_proved(evidence):
    windows = evidence["recovery_windows"]
    if len(windows) != 2:
        return False
    first, second = windows
    scope = evidence["scope"]
    return (evidence["cleared_at"] < first["window_start"] < first["window_end"]
            < second["window_start"] < second["window_end"]
            and all(window["service"] == scope["service"] and window["environment_id"] == scope["environment_id"]
                    and window["requests"] >= 10 and window["error_rate"] == 0 and window["p95_ms"] < 500
                    for window in windows))


def scripted_model(messages):
    receipts = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    if not receipts:
        return "skill_view", {"name": SKILL}
    if len(receipts) == 1:
        return "recovery_windows", {}
    skill, evidence = receipts
    rules_loaded = all(rule in skill["content"] for rule in RULES)
    decision = "recovered" if rules_loaded and recovery_proved(evidence) else "unverified"
    return None, {"incident_id": evidence["incident_id"], **evidence["scope"], "decision": decision}


def connection(offline):
    if not offline:
        return model_connection(False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from local_model import serve
    return serve(scripted_model)


def gate_cases():
    scope = {"service": "payments", "environment_id": "a" * 32}
    first = {**scope, "source": "constructed_recovery_gate", "window_start": 110,
             "window_end": 140, "requests": 20, "error_rate": 0.0, "p95_ms": 80}
    second = {**first, "window_start": 150, "window_end": 180}
    cases = (
        ("disjoint-healthy", [first, second], "recovered"),
        ("overlapping-healthy", [first, {**second, "window_start": 135}], "unverified"),
        ("touching-boundary", [first, {**second, "window_start": 140}], "unverified"),
        ("nonzero-5xx", [first, {**second, "error_rate": 0.01}], "unverified"),
        ("sparse-window", [first, {**second, "requests": 9}], "unverified"),
        ("different-environment", [first, {**second, "environment_id": "b" * 32}], "unverified"),
        ("different-service", [first, {**second, "service": "orders"}], "unverified"),
        ("before-clearance", [{**first, "window_start": 95}, second], "unverified"),
        ("slow-p95", [first, {**second, "p95_ms": 500}], "unverified"),
        ("missing-window", [first], "unverified"),
    )
    for index, (case_id, windows, expected) in enumerate(cases):
        yield case_id, {"incident_id": f"gate-payments-{index + 1}", "scope": scope,
                        "source_session_id": f"constructed-case-{index + 1}", "source_report_item_id": 1,
                        "cleared_at": 100, "recovery_windows": windows}, expected


def live_evidence(raw):
    evidence = json.loads(raw)
    if not isinstance(evidence, dict) or evidence.keys() != EVIDENCE_FIELDS:
        raise ValueError("Expected the recorded recovery evidence of a new incident")
    scope = evidence["scope"]
    if (not isinstance(scope, dict) or scope.keys() != {"service", "environment_id"}
            or scope["service"] not in SERVICES or not isinstance(scope["environment_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", scope["environment_id"])
            or not isinstance(evidence["incident_id"], str)
            or not re.fullmatch(rf"lab-{scope['service']}-[a-f0-9]{{12}}", evidence["incident_id"])
            or not isinstance(evidence["source_session_id"], str)
            or not re.fullmatch(r"patrol-fork-[a-f0-9]{32}", evidence["source_session_id"])
            or type(evidence["source_report_item_id"]) is not int or evidence["source_report_item_id"] <= 0
            or type(evidence["cleared_at"]) not in (int, float) or not math.isfinite(evidence["cleared_at"])
            or evidence["cleared_at"] <= 0):
        raise ValueError("Live recovery evidence has no valid incident scope or SessionDB source")
    windows = evidence["recovery_windows"]
    if not isinstance(windows, list) or any(
        not isinstance(window, dict) or window.keys() != WINDOW_FIELDS
        or window["source"] != "live_lab_http_requests" or type(window["requests"]) is not int
        or any(type(window[key]) not in (int, float) or not math.isfinite(window[key])
               for key in ("window_start", "window_end", "error_rate", "p95_ms"))
        or window["p95_ms"] < 0 for window in windows
    ) or not recovery_proved(evidence):
        raise ValueError("Live recovery evidence must contain the two scoped verified HTTP windows")
    return evidence


def read_evidence(rows, prompt, evidence, skill_sha):
    user_items = [row for row in rows if row["role"] == "user"]
    if len(user_items) != 1 or user_items[0]["content"] != prompt:
        raise RuntimeError("The verification session must begin with just the assigned incident scope")
    calls = [(row["id"], call["id"], call["function"]["name"])
             for row in rows for call in row.get("tool_calls") or []]
    items = [row for row in rows if row["role"] == "tool"]
    if ([name for _, _, name in calls] != ["skill_view", "recovery_windows"] or len(items) != 2
            or rows[-1]["role"] != "assistant"
            or not user_items[0]["id"] < calls[0][0] < items[0]["id"] < calls[1][0] < items[1]["id"] < rows[-1]["id"]
            or any(item["tool_call_id"] != call_id for item, (_, call_id, _) in zip(items, calls))):
        raise RuntimeError("Verification must preserve an ordered Skill read, recovery receipt and final report")
    skill, windows = [json.loads(row["content"]) for row in items]
    if (skill.get("success") is not True or skill.get("name") != SKILL or skill.get("sha256") != skill_sha
            or sha256(skill["content"].encode("utf-8")).hexdigest() != skill_sha or windows != evidence):
        raise RuntimeError("Recovery verification receipts do not match the pinned Skill and assigned evidence")
    try:
        report = decode_report(rows[-1]["content"])
    except (AttributeError, TypeError, ValueError):
        report = None
    return {"prompt_item_id": user_items[0]["id"], "report_item_id": rows[-1]["id"], "report": report,
            "tool_receipts": [{"name": name, "call_id": call_id, "item_id": item["id"], "receipt": receipt}
                              for item, (_, call_id, name), receipt in zip(items, calls, (skill, windows))]}


def run_case(state, skill, digest, case_id, evidence, expected, options):
    state.mkdir()
    session_id = "patrol-recovery-skill-" + uuid.uuid4().hex
    scope = {"incident_id": evidence["incident_id"], **evidence["scope"]}
    prompt = (
        "按恢复检查 Skill 核验已记录的处置证据。范围为 " + json.dumps(scope, ensure_ascii=False) + "。"
        f"先调用 skill_view 读取 {SKILL}，再调用 recovery_windows 获取证据。"
        "只作只读核验，返回含 incident_id、service、environment_id、decision 的 JSON 对象。"
        "符合 Skill 的恢复规则时 decision 填 recovered，否则填 unverified。"
    )
    with open_sessions(state, fresh=True) as sessions:
        if install_oncall_skill(state, skill, name=SKILL) != digest:
            raise RuntimeError("The recovery Skill changed while it was being pinned")
        rows, _ = run_turn(sessions, session_id, prompt, {
            "skill_view": lambda: read_oncall_skill(digest, name=SKILL), "recovery_windows": lambda: evidence,
        }, options, skill_name=SKILL)
        result = read_evidence(rows, prompt, evidence, digest)
    return {"case_id": case_id, "incident_id": scope["incident_id"], "session_id": session_id,
            "passed": result["report"] == {**scope, "decision": expected}, "expected_decision": expected, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--offline", action="store_true", help="Drive the actual Hermes tools using a local scripted model")
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        parser.error("The verification output must be a new file")
    source = args.evidence.read_bytes() if args.evidence is not None else None
    adoption = live_evidence(source) if source is not None else None
    skill = args.skill.resolve(strict=True)
    digest = sha256(skill.read_bytes()).hexdigest()
    args.state.mkdir(parents=True, exist_ok=True)
    ensure_loopback_no_proxy()
    with connection(args.offline) as url:
        options = {"base_url": url, "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
                   "model": "recovery-skill-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2"}
        if adoption is None:
            cases = [run_case(args.state / case_id, skill, digest, case_id, evidence, expected, options)
                     for case_id, evidence, expected in gate_cases()]
            report = {"status": "passed" if all(case["passed"] for case in cases) else "rejected", "cases": cases}
        else:
            case = run_case(args.state / "adopted-incident", skill, digest, "adopted-incident", adoption, "recovered", options)
            report = {"status": "adopted" if case["passed"] else "rejected", "case": case,
                      "source_sha256": sha256(source).hexdigest()}
    if sha256(skill.read_bytes()).hexdigest() != digest or (source is not None and args.evidence.read_bytes() != source):
        raise RuntimeError("An input changed during recovery Skill verification")
    mode = "local-script-model" if args.offline else "live"
    with args.out.open("x", encoding="utf-8") as output:
        json.dump({**report, "skill_sha256": digest, "mode": mode}, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, sqlite3.Error, ValueError, KeyError, TypeError, ImportError) as exc:
        sys.exit(str(exc))
