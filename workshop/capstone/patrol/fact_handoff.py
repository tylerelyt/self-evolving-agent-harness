#!/usr/bin/env python3
"""Check a peer handoff in a fresh Hermes session using historical and live receipts."""

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from environment import SERVICES, classify, get_snapshot
from hermes_runtime import ensure_loopback_no_proxy, open_sessions, run_turn
from patrol import decode_report, model_connection
from shared_facts import make_memory_lookup, validate_fact


def handoff_report(fact, snapshot, *, memory=None):
    report = {
        "fact_id": fact["incident_id"] if fact is not None else None,
        "source_session_id": fact["source_session_id"] if fact is not None else None,
        "source_report_item_id": fact["source_report_item_id"] if fact is not None else None,
        "current_status": classify(snapshot),
    }
    if memory is not None and memory.get("backend") == "openviking":
        report.update(fact_uri=memory["fact_uri"], runbook_uri=memory["runbook_uri"])
    return report


def scripted_model(messages):
    """Drive the actual tools without knowing the historical fact or current state."""
    receipts = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
    if not receipts:
        return "memory_lookup", {}
    if len(receipts) == 1:
        return "service_snapshot", {}
    memory, snapshot = receipts
    if "error" in memory:
        return None, {"memory_error": memory["error"]}
    return None, handoff_report(memory["fact"], snapshot, memory=memory)


def connection(offline):
    if not offline:
        return model_connection(False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from local_model import serve
    return serve(scripted_model)


def check_snapshot(snapshot, scope):
    if (snapshot.get("source") != "live_lab_http_requests" or snapshot.get("service") != scope["service"]
            or snapshot.get("environment_id") != scope["environment_id"]
            or not math.isfinite(snapshot["window_end"])):
        raise RuntimeError("Current snapshot is outside the requested HTTP scope")
    return snapshot


def check_openviking_memory(memory, namespace, receiver, scope):
    from openviking_handoff import fact_body, fact_uri, runbook_uri

    fields = {"fact", "historical_only", "backend", "fact_uri", "fact_sha256",
              "runbook_uri", "runbook_sha256", "runbook"}
    if set(memory) != fields or memory["backend"] != "openviking" or memory["historical_only"] is not True:
        raise RuntimeError("Memory did not return a scoped OpenViking lookup receipt")
    runbook = memory["runbook"]
    if (memory["runbook_uri"] != runbook_uri(namespace, scope["service"])
            or not isinstance(runbook, str) or not runbook.strip()
            or hashlib.sha256(runbook.encode("utf-8")).hexdigest() != memory["runbook_sha256"]):
        raise RuntimeError("OpenViking did not return the scoped runbook and content digest")
    fact = memory["fact"]
    if fact is None:
        if memory["fact_uri"] is not None or memory["fact_sha256"] is not None:
            raise RuntimeError("A missing OpenViking fact cannot have a citation")
        return
    validate_fact(fact)
    expected_uri = fact_uri(namespace, scope["incident_id"], scope["environment_id"])
    if (fact["instance"] == receiver or memory["fact_uri"] != expected_uri
            or hashlib.sha256(fact_body(fact).encode("utf-8")).hexdigest() != memory["fact_sha256"]):
        raise RuntimeError("OpenViking fact must cite a distinct peer resource in the run namespace")


def read_evidence(rows, prompt, scope, session_id, *, openviking_namespace=None, receiver=None):
    user_items = [row for row in rows if row["role"] == "user"]
    if len(user_items) != 1 or user_items[0]["content"] != prompt:
        raise RuntimeError("Handoff did not start with just the requested scope")
    calls = [(row["id"], call["id"], call["function"]["name"])
             for row in rows for call in row.get("tool_calls") or []]
    if [name for _, _, name in calls] != ["memory_lookup", "service_snapshot"]:
        raise RuntimeError("Handoff must request memory and then a fresh HTTP snapshot")
    items = [row for row in rows if row["role"] == "tool"]
    if (len(items) != len(calls)
            or not user_items[0]["id"] < calls[0][0] < items[0]["id"] < calls[1][0] < items[1]["id"] < rows[-1]["id"]
            or rows[-1]["role"] != "assistant"
            or any(item["tool_call_id"] != call_id for item, (_, call_id, _) in zip(items, calls))):
        raise RuntimeError("Handoff must preserve both tool receipts before the final report")

    memory, snapshot = [json.loads(row["content"]) for row in items]
    if "error" in memory:
        raise RuntimeError(f"Historical memory lookup failed: {memory['error']}")
    if openviking_namespace is not None:
        check_openviking_memory(memory, openviking_namespace, receiver, scope)
    elif set(memory) != {"fact", "historical_only"} or memory["historical_only"] is not True:
        raise RuntimeError("Memory did not return a historical lookup receipt")
    check_snapshot(snapshot, scope)
    fact = memory["fact"]
    if fact is not None and (any(fact.get(key) != value for key, value in scope.items())
                             or session_id in (fact["source_session_id"], fact["investigation_session_id"])
                             or snapshot["window_end"] <= fact["recovery_end"]):
        raise RuntimeError("Handoff must use a peer fact and an independent later snapshot")

    report = decode_report(rows[-1]["content"])
    if report != handoff_report(fact, snapshot, memory=memory):
        raise RuntimeError("Handoff report does not match the memory citation and current HTTP receipt")
    return {
        "status": "memory_missing" if fact is None else "recalled", "scope": scope, "session_id": session_id,
        "prompt_item_id": user_items[0]["id"], "report_item_id": rows[-1]["id"],
        "fact": fact, "memory_receipt": memory, "current_receipt": snapshot,
        "current_status": report["current_status"], "report": report,
        "tool_receipts": [{"name": name, "call_id": call_id, "item_id": item["id"], "receipt": receipt}
                          for item, (_, call_id, name), receipt in zip(items, calls, (memory, snapshot))],
    }


def handoff(state, memory, environment_state, scope, options, *, openviking_namespace=None, receiver=None):
    options = dict(options)
    context = options.pop("memory_context", None)
    if context is not None:
        if openviking_namespace is not None or receiver is not None:
            raise ValueError("Choose one source of external-memory configuration")
        openviking_namespace, receiver = context["openviking_namespace"], context["receiver"]
    session_id = "patrol-handoff-" + uuid.uuid4().hex
    prompt = (
        "请做一次同伴事故交接核对。范围为 " + json.dumps(scope, ensure_ascii=False) + "。"
        "先用 memory_lookup 查询历史事故。等回执返回后，再用 service_snapshot 查询现场。"
        "历史记忆不能用来判断当前健康。只按现场快照判断 current_status。"
        "请求数不足 10 时填 insufficient，否则错误率至少 0.05 或 P95 至少 500ms 时填 abnormal，其余填 healthy。"
        "只返回 JSON，字段为 fact_id、source_session_id、source_report_item_id、current_status。"
        "fact_id 使用命中的 incident_id，另两个来源字段照抄命中的事实。未命中时，三个来源字段都填 null。"
    )
    if openviking_namespace is not None:
        if memory is not None or receiver not in {"instance-a", "instance-b"}:
            raise ValueError("OpenViking handoff requires an assigned receiver and no local memory store")
        prompt += (
            "本轮 memory_lookup 还会查找对应服务的值班手册。"
            "JSON 另含 fact_uri 和 runbook_uri，逐字引用实际回执中的两个来源。"
            "历史事故未命中时 fact_uri 填 null。值班手册提供排查顺序，当前状态仍按现场 HTTP 回执判断。"
        )
    with open_sessions(state, fresh=True) as sessions:
        if openviking_namespace is None:
            lookup = make_memory_lookup(memory, scope["incident_id"], scope["service"], scope["environment_id"],
                                        session_id=session_id)
        else:
            from openviking_handoff import make_openviking_lookup

            lookup = make_openviking_lookup(
                openviking_namespace, scope["incident_id"], scope["service"], scope["environment_id"],
                receiver=receiver, session_id=session_id,
            )
        rows, _ = run_turn(sessions, session_id, prompt, {
            "memory_lookup": lookup,
            "service_snapshot": lambda: check_snapshot(get_snapshot(environment_state, scope["service"]), scope),
        }, options)
    return read_evidence(rows, prompt, scope, session_id,
                         openviking_namespace=openviking_namespace, receiver=receiver)


def verify_saved_handoff(state, scope, result, *, openviking_namespace=None, receiver=None):
    """Verify a selected implementation's return against its native SessionDB."""
    if not isinstance(result, dict) or not isinstance(result.get("session_id"), str):
        raise ValueError("Memory handoff must return a persisted Hermes session")
    database = Path(state) / "hermes" / "state.db"
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (result["session_id"],))]
    if not rows:
        raise ValueError("Memory handoff did not leave a persisted Hermes session")
    for row in rows:
        row["tool_calls"] = json.loads(row["tool_calls"] or "[]")
    verified = read_evidence(rows, rows[0]["content"], scope, result["session_id"],
                             openviking_namespace=openviking_namespace, receiver=receiver)
    if result != verified:
        raise ValueError("Memory handoff differs from its persisted Hermes tool receipts")
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--memory", type=Path)
    source.add_argument("--openviking", action="store_true", help="Use the assigned external OpenViking resources")
    parser.add_argument("--openviking-namespace")
    parser.add_argument("--receiver", choices=("instance-a", "instance-b"))
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--environment-state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Drive the actual Hermes tools using a local scripted model")
    parser.add_argument("--implementation", type=Path, help="Use the selected implementation's memory hook")
    args = parser.parse_args()
    home = args.state / "hermes"
    if any(path.exists() or path.is_symlink() for path in (home, args.out)):
        parser.error("Handoff requires a new Hermes Home and a new output file")
    memory_path = None
    if args.openviking:
        if not args.openviking_namespace or args.receiver is None:
            parser.error("--openviking requires --openviking-namespace and --receiver")
    else:
        if args.openviking_namespace is not None or args.receiver is not None:
            parser.error("--openviking-namespace and --receiver require --openviking")
        memory_path = args.memory.resolve()
        if args.out.resolve() == memory_path or memory_path.is_relative_to(home.resolve()):
            parser.error("Handoff output and Home must be separate from the memory store")
    args.state.mkdir(parents=True, exist_ok=True)
    ensure_loopback_no_proxy()
    scope = {"incident_id": args.incident_id, "service": args.service, "environment_id": args.environment_id}
    with connection(args.offline) as url:
        options = {
            "base_url": url, "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
            "model": "handoff-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2",
        }
        if args.implementation is None:
            result = handoff(args.state, memory_path, args.environment_state, scope, options,
                             openviking_namespace=args.openviking_namespace, receiver=args.receiver)
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from implementation import load_implementation

            if args.openviking:
                options["memory_context"] = {"openviking_namespace": args.openviking_namespace,
                                             "receiver": args.receiver}
            implementation = load_implementation(args.implementation)
            result = implementation.recall_memory(args.state, memory_path, args.environment_state, scope, options)
            result = verify_saved_handoff(args.state, scope, result,
                                          openviking_namespace=args.openviking_namespace, receiver=args.receiver)
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, sqlite3.Error, ValueError, KeyError, TypeError, ImportError) as exc:
        sys.exit(str(exc))
