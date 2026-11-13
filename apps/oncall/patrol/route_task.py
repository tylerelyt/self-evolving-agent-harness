#!/usr/bin/env python3
"""Use native Hermes Memory to locate and verify one scoped diagnostic route."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from environment import DIAGNOSTIC_SOURCE, DIAGNOSTIC_V1, DIAGNOSTIC_V2, SERVICES, probe_diagnostic
from hermes_runtime import ensure_loopback_no_proxy, open_sessions, run_turn
from patrol import decode_report, model_connection


ROUTE_PREFIX = "CAPSTONE_DIAGNOSTIC_ROUTE_V1 "


def route_entries(text):
    return [line for line in text.splitlines() if line.startswith(ROUTE_PREFIX)]


def scripted_model(messages):
    """Choose from Hermes' actual system prompt and live tool receipts."""
    receipts = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    if not receipts:
        entries = [line for message in messages if message["role"] == "system"
                   for line in route_entries(message["content"])]
        if len(entries) != 1:
            raise ValueError("The native system prompt must supply one diagnostic Memory entry")
        remembered = json.loads(entries[0].removeprefix(ROUTE_PREFIX))
        return "diagnostic_lookup", {"path": remembered["path"]}
    latest = receipts[-1]
    if len(receipts) == 1 and latest.get("http_status") == 410 and latest.get("replacement") == DIAGNOSTIC_V2:
        return "diagnostic_lookup", {"path": latest["replacement"]}
    if latest.get("http_status") == 200 and latest.get("active") is True:
        return None, {key: latest[key] for key in ("service", "environment_id", "path")}
    return None, {"error": "No active diagnostic route was verified"}


def connection(offline):
    if not offline:
        return model_connection(False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from local_model import serve
    return serve(scripted_model)


def read_evidence(rows, prompt, scope, remembered_path):
    user_items = [row for row in rows if row["role"] == "user"]
    if len(user_items) != 1 or user_items[0]["content"] != prompt:
        raise RuntimeError("The diagnostic session must begin with just the requested scope")
    calls = [(row["id"], call["id"], call["function"])
             for row in rows for call in row.get("tool_calls") or []]
    items = [row for row in rows if row["role"] == "tool"]
    final = rows[-1]
    if not 1 <= len(calls) == len(items) <= 2 or final["role"] != "assistant":
        raise RuntimeError("Expected one or two diagnostic HTTP calls before the final report")

    evidence = []
    previous_item = user_items[0]["id"]
    expected_path = remembered_path
    for (call_item, call_id, function), item in zip(calls, items):
        arguments = function["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if (function["name"] != "diagnostic_lookup" or expected_path is None
                or arguments != {"path": expected_path} or item["tool_call_id"] != call_id
                or not previous_item < call_item < item["id"] < final["id"]):
            raise RuntimeError("Diagnostic calls must follow Memory first and then any observed replacement")

        receipt = json.loads(item["content"])
        status = receipt.get("http_status")
        replacement = DIAGNOSTIC_V2 if status == 410 and expected_path == DIAGNOSTIC_V1 else None
        expected = {**scope, "source": DIAGNOSTIC_SOURCE, "path": expected_path,
                    "active": status == 200, "replacement": replacement, "http_status": status}
        if (type(status) is not int or not (status == 200 or replacement is not None)
                or type(receipt.get("active")) is not bool or receipt != expected):
            raise RuntimeError("Diagnostic tool returned an unexpected route or scope")
        evidence.append({"name": "diagnostic_lookup", "call_id": call_id, "item_id": item["id"], "receipt": receipt})
        previous_item = item["id"]
        expected_path = replacement

    last_receipt = evidence[-1]["receipt"]
    report = decode_report(final["content"])
    if last_receipt["http_status"] != 200 or report != {**scope, "path": last_receipt["path"]}:
        raise RuntimeError("The report must identify the route verified by an HTTP 200 receipt")
    return {"prompt_item_id": user_items[0]["id"], "report_item_id": final["id"],
            "tool_receipts": evidence, "report": report}


def route_task(state, raw_memory, environment_state, scope, options):
    memory_text = raw_memory.decode("utf-8")
    entries = route_entries(memory_text)
    if len(entries) != 1 or memory_text.splitlines() != entries:
        raise ValueError("Expected exactly one scoped diagnostic Memory entry")
    entry = entries[0]
    remembered = json.loads(entry.removeprefix(ROUTE_PREFIX))
    if not isinstance(remembered, dict) or remembered.get("path") not in (DIAGNOSTIC_V1, DIAGNOSTIC_V2):
        raise ValueError("Invalid diagnostic Memory entry")
    if remembered != {**scope, "path": remembered["path"]}:
        raise ValueError("Diagnostic Memory is outside the requested scope")

    session_id = "patrol-route-" + uuid.uuid4().hex
    prompt = (
        "核实指定服务的诊断入口。范围为 " + json.dumps(scope, ensure_ascii=False) + "。"
        "先从当前会话的 Memory 读取该范围记录的路径，再用 diagnostic_lookup 请求它。"
        "如果实际回执提供替代路径，再用工具核实它。只有确认入口可用时，"
        "才输出含 service、environment_id、path 的 JSON 对象。"
    )
    with open_sessions(state, fresh=True, memory_text=memory_text) as sessions:
        rows, _ = run_turn(sessions, session_id, prompt, {
            "diagnostic_lookup": lambda path: probe_diagnostic(environment_state, scope["service"], path,
                                                              scope["environment_id"]),
        }, options, load_memory=True)
        saved = sessions.get_session(session_id)
        system_prompt = saved.get("system_prompt") if saved else None
        if not isinstance(system_prompt, str) or route_entries(system_prompt) != [entry]:
            raise RuntimeError("The native SessionDB system prompt did not load the expected Memory entry")

    local_memory = state / "hermes" / "memories" / "MEMORY.md"
    if local_memory.read_bytes() != raw_memory:
        raise RuntimeError("The read-only diagnostic task changed Memory")
    return {
        "session_id": session_id, "scope": scope, "memory_sha256": sha256(raw_memory).hexdigest(),
        "native_memory_loaded": True, "loaded_memory_entry": entry,
        "system_prompt_sha256": sha256(system_prompt.encode("utf-8")).hexdigest(),
        **read_evidence(rows, prompt, scope, remembered["path"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--memory-file", type=Path, required=True)
    parser.add_argument("--environment-state", type=Path, required=True)
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Drive the actual Hermes tools using a local scripted model")
    args = parser.parse_args()
    home = args.state / "hermes"
    if any(path.exists() or path.is_symlink() for path in (home, args.out)):
        parser.error("The diagnostic task requires a new Hermes Home and a new output file")
    memory_path = args.memory_file.resolve()
    if args.out.resolve() == memory_path or memory_path.is_relative_to(home.resolve()):
        parser.error("The diagnostic output and Home must be separate from the supplied Memory")
    raw_memory = memory_path.read_bytes()
    args.state.mkdir(parents=True, exist_ok=True)
    ensure_loopback_no_proxy()
    scope = {"service": args.service, "environment_id": args.environment_id}
    with connection(args.offline) as url:
        options = {
            "base_url": url, "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
            "model": "route-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2",
        }
        result = route_task(args.state, raw_memory, args.environment_state, scope, options)
    if memory_path.read_bytes() != raw_memory:
        raise RuntimeError("The supplied Memory changed during the diagnostic task")
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, sqlite3.Error, ValueError, KeyError, TypeError, ImportError) as exc:
        sys.exit(str(exc))
