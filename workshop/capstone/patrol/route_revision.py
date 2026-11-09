#!/usr/bin/env python3
"""Propose a diagnostic route replacement through native Hermes background review."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

from environment import DIAGNOSTIC_SOURCE, DIAGNOSTIC_V1, DIAGNOSTIC_V2, SERVICES
from fact_review import MEMORY_FILE, native_review, review_connection
from hermes_runtime import ensure_loopback_no_proxy
from patrol import decode_report
from route_task import ROUTE_PREFIX


EVIDENCE_PREFIX = "CAPSTONE_ROUTE_REPAIR_JSON "
PROMPT = (
    "Review the saved diagnostic repair evidence for a stable Memory correction. "
    "The JSON below contains the old_entry and two ordered HTTP receipts. "
    "Only replace the entry if both receipts are from its service and environment, "
    "the old route actually returned 410 with a replacement, and that exact replacement returned 200. "
    "Use one native memory call with action replace, target memory, and the exact old_entry as old_text. "
    "The new content must use the same CAPSTONE_DIAGNOSTIC_ROUTE_V1 prefix followed by compact JSON "
    "with keys environment_id, path, service in that order. Change only the path to the verified one. "
    "After a successful receipt reply with JSON {\"status\":\"proposed\"}. "
    "If you choose to leave Memory unchanged, make no call and reply with JSON {\"status\":\"keep\"}.\n"
)


def memory_entry(scope, path):
    return ROUTE_PREFIX + json.dumps({**scope, "path": path}, sort_keys=True, separators=(",", ":"))


def replacement_args(evidence):
    scope = evidence["scope"]
    if (not isinstance(scope, dict) or scope.keys() != {"service", "environment_id"}
            or scope["service"] not in SERVICES or not isinstance(scope["environment_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", scope["environment_id"])):
        raise ValueError("Diagnostic repair has an invalid scope")
    old = memory_entry(scope, DIAGNOSTIC_V1)
    if evidence["old_entry"] != old:
        raise ValueError("The saved Memory does not hold the old route for this scope")
    receipts = evidence["receipts"]
    if not isinstance(receipts, list) or len(receipts) != 2:
        raise ValueError("Diagnostic revision needs both scoped HTTP receipts")
    retired, current = receipts
    base = {"source": DIAGNOSTIC_SOURCE, **scope}
    if (retired != {**base, "path": DIAGNOSTIC_V1, "http_status": 410,
                    "active": False, "replacement": DIAGNOSTIC_V2}
            or current != {**base, "path": retired["replacement"], "http_status": 200,
                           "active": True, "replacement": None}
            or type(retired["http_status"]) is not int or type(current["http_status"]) is not int
            or retired["active"] is not False or current["active"] is not True):
        raise ValueError("Repair receipts do not verify a scoped retired and current route")
    return {"action": "replace", "target": "memory", "old_text": old,
            "content": memory_entry(scope, current["path"])}


def repair_evidence(raw, initial_memory):
    repair = json.loads(raw)
    old_entry = initial_memory.decode("utf-8").removesuffix("\n")
    if (not isinstance(repair, dict) or repair["native_memory_loaded"] is not True
            or repair["loaded_memory_entry"] != old_entry
            or repair["memory_sha256"] != sha256(initial_memory).hexdigest()):
        raise ValueError("Repair did not load the supplied old Memory snapshot")
    items = repair["tool_receipts"]
    if not isinstance(items, list) or len(items) != 2:
        raise ValueError("Diagnostic revision needs exactly two ordered tool receipts")
    item_ids = [repair["prompt_item_id"], *(item["item_id"] for item in items), repair["report_item_id"]]
    call_ids = [item["call_id"] for item in items]
    if (not all(type(item) is int and item > 0 for item in item_ids)
            or not all(a < b for a, b in zip(item_ids, item_ids[1:]))
            or not all(isinstance(call, str) and call for call in call_ids) or call_ids[0] == call_ids[1]
            or any(item["name"] != "diagnostic_lookup" for item in items)
            or not isinstance(repair["session_id"], str) or not repair["session_id"]):
        raise ValueError("Diagnostic repair lacks valid Session and ordered tool Items")
    evidence = {"scope": repair["scope"], "old_entry": old_entry,
                "receipts": [item["receipt"] for item in items]}
    args = replacement_args(evidence)
    if repair["report"] != {**evidence["scope"], "path": evidence["receipts"][1]["path"]}:
        raise ValueError("Diagnostic repair report does not match the confirmed route")
    return repair, evidence, args


def scripted_revision(messages):
    line = next((line for message in messages if message["role"] == "user"
                 for line in message["content"].splitlines() if line.startswith(EVIDENCE_PREFIX)), None)
    if line is None:
        raise ValueError("Scripted revision requires recorded repair evidence")
    args = replacement_args(json.loads(line.removeprefix(EVIDENCE_PREFIX)))
    receipt = next((json.loads(message["content"]) for message in reversed(messages)
                    if message["role"] == "tool"), None)
    if receipt is None:
        return "memory", args
    return None, {"status": "proposed" if receipt.get("success") is True else "keep"}


def verified_revision(state, initial_memory, expected, captured):
    session_id, worker_thread, result = captured
    if (not result.get("completed") or not result.get("final_response")
            or any(result.get(key) for key in ("failed", "error", "interrupted", "partial"))):
        raise RuntimeError("Native route revision did not complete its turn")
    decision = decode_report(result["final_response"])
    if not isinstance(decision, dict) or decision.get("status") not in ("proposed", "keep"):
        raise ValueError("Native route revision did not return a proposal decision")
    messages = result["messages"]
    calls = [call for message in messages for call in message.get("tool_calls") or []]
    readback = (state / MEMORY_FILE).read_bytes()
    status = decision["status"]
    trace = {"completed": True, "memory_write_verified": False, "memory_item": MEMORY_FILE.as_posix(),
             "memory_before_sha256": sha256(initial_memory).hexdigest(), "memory_sha256": sha256(readback).hexdigest(),
             "session_id": session_id, "worker_thread": worker_thread, "memory_tool_calls": []}
    if status == "keep":
        if calls or readback != initial_memory:
            raise RuntimeError("Keep unexpectedly attempted a Memory write")
        return {"status": status, "native_review": trace}

    if len(calls) != 1 or calls[0]["function"]["name"] != "memory":
        raise RuntimeError("A route correction requires exactly one native Memory call")
    call = calls[0]
    arguments = call["function"]["arguments"]
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    receipt = next((json.loads(message["content"]) for message in messages
                    if message.get("role") == "tool" and message.get("tool_call_id") == call["id"]), {})
    if (args != expected or receipt.get("success") is not True or receipt.get("target") != "memory"
            or readback != expected["content"].encode("utf-8")):
        raise RuntimeError("Native replacement receipt or Memory read-back differs from the verified route")
    trace.update(memory_write_verified=True, memory_tool_calls=[{"id": call["id"], "name": "memory",
                                                                 **args, "success": True}])
    return {"status": status, "native_review": trace}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--repair", type=Path, required=True)
    parser.add_argument("--memory-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Use the local scripted model")
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        parser.error("--out must be a new file")
    raw = args.repair.read_bytes()
    initial = args.memory_file.read_bytes()
    repair, evidence, expected = repair_evidence(raw, initial)
    entry = EVIDENCE_PREFIX + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not args.offline and not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("Set GLM_API_KEY or use --offline")
    ensure_loopback_no_proxy()
    with review_connection(args.offline, scripted_revision) as url:
        options = {"base_url": url, "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
                   "model": "route-review-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2"}
        state = args.state.resolve()
        captured = native_review(state, entry, options, review_prompt=PROMPT, initial_memory=initial)
        proposal = verified_revision(state, initial, expected, captured)
    if args.memory_file.read_bytes() != initial or args.repair.read_bytes() != raw:
        raise RuntimeError("The source repair artifact or old Memory changed during review")
    proposal.update(source_sha256=sha256(raw).hexdigest(), repair_session_id=repair["session_id"],
                    repair_report_item_id=repair["report_item_id"], mode="local-script-model" if args.offline else "live")
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(proposal, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ImportError) as exc:
        sys.exit(f"Native route revision failed: {exc}")
