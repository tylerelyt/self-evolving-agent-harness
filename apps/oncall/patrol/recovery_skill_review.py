#!/usr/bin/env python3
"""Create a recovery-window Skill candidate with native Hermes Background Review."""

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sys

from export_workset import scoped_snapshot
from fact_review import native_review, review_connection
from hermes_runtime import ONCALL_SKILL, ensure_loopback_no_proxy
from patrol import decode_report
from shared_facts import validate_scope


SKILL = "oncall-recovery-window-check"
EVIDENCE_PREFIX = "CAPSTONE_RECOVERY_EVIDENCE_JSON "
PROMPT = (
    "Review a completed incident to decide whether it supports a reusable recovery-check procedure. "
    "Make exactly one tool call per assistant response. First call skills_list and wait for "
    "its successful receipt. Only in the next response call skill_view on oncall-alert-triage "
    "and wait for its successful receipt before deciding. Never parallelize these calls. "
    "Keep the existing Skill unchanged. If a distinct procedure is justified, use exactly one "
    "skill_manage create to propose oncall-recovery-window-check as a standalone SKILL.md. "
    "The create arguments must contain only action, name and content: action=create, "
    "name=oncall-recovery-window-check, content=the entire Skill. Omit category and all other "
    "optional arguments so the candidate lives directly under the skills root. The content "
    "must have YAML frontmatter with this exact name and a description of at most 60 characters, "
    "one short trigger-first sentence ending in a period; put all details in the body. "
    "Use recovery_windows and teach how to verify exact scope, separate post-clearance windows, "
    "request volume, errors, and latency. Never include the actual incident ID, Session ID, or "
    "environment ID in the reusable method. Do not execute remediation. Reply with JSON status "
    "proposed after a successful create, or keep only if no Skill should be created and no "
    "creation was attempted. If any tool fails, stop and reply with JSON status failed; do "
    "not retry creation or claim keep. Do not call skill_view again after creation: the host "
    "independently verifies the candidate's actual bytes.\n"
)
SCRIPTED_SKILL = """---
name: oncall-recovery-window-check
description: Verify mitigation with scoped HTTP recovery windows.
---

# Check HTTP recovery evidence

1. Call `recovery_windows` and compare every receipt with the incident's exact `service and environment_id` scope.
2. Require exactly two non-overlapping windows strictly after cleared_at. Check `window_start > cleared_at` for both and `first.window_end < second.window_start` in chronological order.
3. Both windows must show at least 10 requests (`requests >= 10`), zero 5xx (`error_rate == 0`) and `p95_ms < 500`.
4. Report recovery verified only when both windows meet these checks. Missing or conflicting evidence is unverified. This check only reads evidence and performs no remediation.
"""


def verified_evidence(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or data.keys() != {
        "incident_id", "scope", "source_session_id", "source_report_item_id", "cleared_at", "recovery_windows",
    }:
        raise ValueError("Expected the closed incident's exact recovery evidence fields")
    scope = data["scope"]
    if not isinstance(scope, dict) or scope.keys() != {"service", "environment_id"}:
        raise ValueError("Expected a service and environment scope")
    service, environment = scope["service"], scope["environment_id"]
    validate_scope(data["incident_id"], service, environment)
    if (not data["incident_id"].startswith(f"lab-{service}-")
            or not isinstance(data["source_session_id"], str)
            or not re.fullmatch(r"patrol-fork-[0-9a-f]{32}", data["source_session_id"])
            or type(data["source_report_item_id"]) is not int or data["source_report_item_id"] < 1):
        raise ValueError("Recovery evidence must identify the scoped investigation Session and Item")

    cleared_at = data["cleared_at"]
    windows = data["recovery_windows"]
    if (type(cleared_at) not in (int, float) or not math.isfinite(cleared_at) or cleared_at <= 0
            or not isinstance(windows, list) or len(windows) != 2):
        raise ValueError("Recovery evidence must include clearance and two observations")
    lower_bound = cleared_at
    for window in windows:
        if (window != scoped_snapshot(window, service, environment)
                or window["window_start"] <= lower_bound
                or window["error_rate"] != 0 or window["p95_ms"] >= 500):
            raise ValueError("Recovery observations must be healthy, scoped, post-clearance and nonoverlapping")
        lower_bound = window["window_end"]
    return data


def scripted_review(messages):
    entry = next((line for message in messages if message["role"] == "user"
                  for line in message["content"].splitlines() if line.startswith(EVIDENCE_PREFIX)), None)
    if entry is None:
        raise ValueError("Scripted review requires persisted recovery evidence")
    verified_evidence(entry.removeprefix(EVIDENCE_PREFIX))
    receipts = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    if not receipts:
        return "skills_list", {}
    if any(receipt.get("success") is not True for receipt in receipts):
        return None, {"status": "keep"}
    if len(receipts) == 1:
        names = {item["name"] for item in receipts[0]["skills"]}
        if ONCALL_SKILL not in names or SKILL in names:
            return None, {"status": "keep"}
        return "skill_view", {"name": ONCALL_SKILL}
    if len(receipts) == 2:
        if receipts[-1].get("name") != ONCALL_SKILL or not receipts[-1].get("content"):
            return None, {"status": "keep"}
        return "skill_manage", {"action": "create", "name": SKILL, "content": SCRIPTED_SKILL}
    return None, {"status": "proposed"}


def verified_result(state, evidence, formal, captured):
    from tools import skill_usage

    session_id, worker_thread, result = captured
    if (not result.get("completed") or not result.get("final_response")
            or any(result.get(key) for key in ("failed", "error", "interrupted", "partial"))):
        raise RuntimeError("Native Hermes Skill review did not complete")
    decision = decode_report(result["final_response"])
    if not isinstance(decision, dict) or decision.get("status") not in ("proposed", "keep"):
        raise ValueError("Native Skill review did not return a proposal decision")
    status = decision["status"]
    messages = result["messages"]
    ordered = [(position, call) for position, message in enumerate(messages)
               for call in message.get("tool_calls") or []]
    calls = [call for _, call in ordered]
    replies = {message["tool_call_id"]: (position, json.loads(message["content"]))
               for position, message in enumerate(messages) if message.get("role") == "tool"}
    expected = ["skills_list", "skill_view"] + (["skill_manage"] if status == "proposed" else [])
    if ([call["function"]["name"] for call in calls] != expected
            or len(replies) != len(calls) or len({call["id"] for call in calls}) != len(calls)
            or any(call["id"] not in replies for call in calls)):
        raise RuntimeError("Expected successful native inventory and read-back before any single candidate creation")
    previous = -1
    receipts = {}
    for position, call in ordered:
        replied_at, receipt = replies[call["id"]]
        if not previous < position < replied_at or receipt.get("success") is not True:
            raise RuntimeError("Native Skill review calls must follow the preceding successful receipt")
        previous = replied_at
        receipts[call["id"]] = receipt
    inventory, viewed = (receipts[call["id"]] for call in calls[:2])
    if ({item["name"] for item in inventory["skills"]} != {ONCALL_SKILL}
            or viewed.get("name") != ONCALL_SKILL or viewed["content"].encode("utf-8") != formal):
        raise RuntimeError("Native review did not inspect the exact protected Skill before deciding")

    root = state / "hermes" / "skills"
    protected = root / ONCALL_SKILL / "SKILL.md"
    proposed = root / SKILL / "SKILL.md"
    allowed = {protected, root / ".usage.json", root / ".usage.json.lock"}
    if status == "proposed":
        allowed.add(proposed)
    if (protected.read_bytes() != formal or skill_usage.get_record(ONCALL_SKILL).get("pinned") is not True
            or any(path not in allowed for path in root.rglob("*") if path.is_file() or path.is_symlink())):
        raise RuntimeError("Native Skill review changed the protected library or produced unexpected files")

    trace = {"completed": True, "session_id": session_id, "worker_thread": worker_thread,
             "protected_skill_pinned": True, "inventory": sorted(item["name"] for item in inventory["skills"]),
             "tool_calls": [{"id": call["id"], "name": call["function"]["name"], "success": True} for call in calls]}
    result = {"status": status, "formal_sha256": sha256(formal).hexdigest(),
              "candidate": None, "candidate_sha256": None, "native_review": trace}
    if status == "proposed":
        call = calls[-1]
        arguments = call["function"]["arguments"]
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        content = proposed.read_text(encoding="utf-8")
        if (args != {"action": "create", "name": SKILL, "content": content}
                or Path(receipts[call["id"]]["skill_md"]).resolve() != proposed
                or skill_usage.get_record(SKILL).get("created_by") != "agent"
                or any(str(evidence[key]) in content for key in ("incident_id", "source_session_id"))
                or evidence["scope"]["environment_id"] in content):
            raise RuntimeError("Native creation or candidate read-back does not match the scoped proposal")
        trace["tool_calls"][-1].update(action="create", skill=SKILL)
        result.update(candidate=proposed.relative_to(state).as_posix(), candidate_sha256=sha256(proposed.read_bytes()).hexdigest())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--formal-skill", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Use the local scripted model")
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        parser.error("--out must be a new file")
    raw = args.evidence.read_bytes()
    evidence = verified_evidence(raw)
    formal = args.formal_skill.read_bytes()
    if not args.offline and not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("Set GLM_API_KEY or use --offline")
    entry = EVIDENCE_PREFIX + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ensure_loopback_no_proxy()
    with review_connection(args.offline, scripted_review) as url:
        options = {"base_url": url, "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
                   "model": "recovery-skill-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2"}
        state = args.state.resolve()
        captured = native_review(state, entry, options, review_prompt=PROMPT, initial_skill=formal)
        proposal = verified_result(state, evidence, formal, captured)
    if args.formal_skill.read_bytes() != formal or args.evidence.read_bytes() != raw:
        raise RuntimeError("Native Skill review changed its formal input or evidence")
    proposal.update(source_sha256=sha256(raw).hexdigest(), mode="local-script-model" if args.offline else "live")
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(proposal, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ImportError) as exc:
        sys.exit(f"Native recovery Skill review failed: {exc}")
