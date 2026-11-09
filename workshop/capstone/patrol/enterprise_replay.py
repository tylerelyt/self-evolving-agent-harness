"""Replay versioned enterprise cases in fresh, read-only Hermes investigations."""

import argparse
from contextlib import closing, nullcontext
from hashlib import sha256
from importlib import import_module
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from uuid import uuid4

from environment import write_json
from hermes_runtime import ensure_loopback_no_proxy, install_oncall_skill, open_sessions


HERE = Path(__file__).resolve()
LESSON = HERE.parents[3] / "examples" / "19-enterprise-dataset"
SKILL_NAME = "oncall-investigation"
SEED = LESSON / "skills" / SKILL_NAME / "SKILL.md"
PEER_CHECK = "排除同名服务前，必须查询非目标集群中该服务的告警和日志，并引用实际查到的两类证据。"
MAX_TURNS = 12


def lesson_module(name):
    sys.path.insert(0, str(LESSON))
    try:
        module = import_module(name)
        if Path(module.__file__).resolve().parent != LESSON:
            raise RuntimeError(f"Unexpected Lesson 19 module: {name}")
        return module
    finally:
        sys.path.pop(0)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def monitor_lookup(query, snapshot, arguments):
    try:
        return query(snapshot, **arguments)
    except (TypeError, KeyError, ValueError) as exc:
        return {"error": str(exc), "query": arguments}


def task_scope(prompt):
    header = prompt.partition("\n")[0]
    prefix = "CAPSTONE_SCOPE "
    if not header.startswith(prefix):
        raise ValueError("Expected a capstone enterprise scope")
    scope = json.loads(header.removeprefix(prefix))
    if (not isinstance(scope, dict) or set(scope) != {"cluster", "service", "start", "end"}
            or any(not isinstance(value, str) or not value for value in scope.values())):
        raise ValueError("Invalid capstone enterprise scope")
    return scope


def failed_trace_ids(traces, upstream):
    trace_ids = set()
    for row in traces:
        value = row["value"]
        status = value.get("status", 0)
        if (value.get("upstream") == upstream and status >= 500 and status == value.get("upstream_status")
                and value.get("trace_id")):
            trace_ids.add(value["trace_id"])
    return trace_ids


def respond(messages):
    """Drive actual monitor queries using only the model's visible evidence."""
    prompt = next(message["content"] for message in messages if message["role"] == "user")
    scope = task_scope(prompt)
    cluster, service = scope["cluster"], scope["service"]
    interval = {"start": scope["start"], "end": scope["end"]}
    receipts = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    by_key = {(receipt["query"]["cluster"], receipt["query"]["kind"]): receipt for receipt in receipts}

    def request(kind, region, target):
        return "monitor_query", {**interval, "kind": kind, "cluster": region, "service": target}

    for kind in ("alerts", "traces", "dependencies"):
        if (cluster, kind) not in by_key:
            return request(kind, cluster, service)
    traces = by_key[cluster, "traces"].get("rows", [])
    dependencies = by_key[cluster, "dependencies"].get("rows", [])
    upstreams = {row["value"].get("upstream") for row in traces}
    declared = {upstream for row in dependencies for upstream in row["value"].get("upstream", [])}
    roots = upstreams & declared
    root = next(iter(roots)) if len(roots) == 1 else None
    linked_failures = failed_trace_ids(traces, root) if root else set()
    if linked_failures and root.startswith(cluster + "/"):
        root_service = root.split("/", 1)[1]
        for kind in ("metrics", "logs"):
            if (cluster, kind) not in by_key:
                return request(kind, cluster, root_service)
        root_metrics = by_key[cluster, "metrics"].get("rows", [])
        root_logs = by_key[cluster, "logs"].get("rows", [])
        if (not any((row["value"].get("error_rate") or 0) > 0 for row in root_metrics)
                or not any(row["value"].get("trace_id") in linked_failures and row["value"].get("status", 0) >= 500
                           and row["value"].get("caller_service") == service for row in root_logs)):
            root = None
    else:
        root = None
    peers = set(by_key[cluster, "alerts"]["scope"]["clusters"]) - {cluster}
    excluded = None
    if len(peers) == 1 and any(message["role"] == "system" and PEER_CHECK in message["content"]
                               for message in messages):
        peer = next(iter(peers))
        for kind in ("alerts", "logs"):
            if (peer, kind) not in by_key:
                return request(kind, peer, service)
        peer_alerts = by_key[peer, "alerts"].get("rows", [])
        peer_logs = by_key[peer, "logs"].get("rows", [])
        if (any((row["value"].get("error_rate") or 0) > 0 for row in peer_alerts)
                and any(row["value"].get("status", 0) >= 500 and not row["value"].get("caller_service")
                        and row["value"].get("trace_id") and row["value"]["trace_id"] not in linked_failures
                        for row in peer_logs)):
            excluded = peer
    return None, {
        "root_cause": root,
        "affected_service": f"{cluster}/{service}",
        "excluded_cluster": excluded,
        "evidence_ids": list(dict.fromkeys(row["id"] for receipt in receipts for row in receipt.get("rows", []))),
        "explanation": (
            "目标服务的调用链和上游错误日志共享关联号，上游指标也记录了请求失败。"
            if root else "目标集群的证据不足以确认上游根因。"
        ) + ("另一区域的同名服务有自己的告警和日志。" if excluded else ""),
    }


def instruction(case_protocol):
    if case_protocol:
        from enterprise_case_gate import OUTPUT_INSTRUCTION

        return OUTPUT_INSTRUCTION
    return lesson_module("worker").OUTPUT_INSTRUCTION


def run_actor(state, *, case_protocol=False):
    """The child receives a public task, frozen snapshot, Skill and model endpoint."""
    monitor = lesson_module("monitor")
    protocol = lesson_module("worker")
    task = read_json(state / "task.json")
    snapshot = read_json(state / "snapshot.json")
    output_instruction = instruction(case_protocol)
    prompt = task["prompt"] + "\n\n" + output_instruction
    skill = state / "SKILL.md"
    session_id = "patrol-enterprise-" + uuid4().hex
    with open_sessions(state, fresh=True) as db:
        from agent.skill_commands import build_preloaded_skills_prompt
        from hermes_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist
        from run_agent import AIAgent
        from tools.registry import registry

        skill_sha = install_oncall_skill(state, skill, name=SKILL_NAME)
        preloaded, names, missing = build_preloaded_skills_prompt([SKILL_NAME])
        if missing or names != [SKILL_NAME] or skill.read_bytes().decode().strip() not in preloaded:
            raise RuntimeError("Hermes did not preload the pinned enterprise Skill")

        def lookup(arguments, **_kwargs):
            return json.dumps(monitor_lookup(monitor.query, snapshot, arguments), ensure_ascii=False)

        registry.register(name="monitor_query", toolset="incident_snapshot", schema=monitor.tool_schema(snapshot),
                          handler=lookup, check_fn=lambda: True)
        agent = AIAgent(
            model=os.environ["COURSE_ENTERPRISE_MODEL"], base_url=os.environ["COURSE_ENTERPRISE_URL"],
            api_key=os.environ["COURSE_ENTERPRISE_KEY"], provider="custom", api_mode="chat_completions",
            session_id=session_id, session_db=db, enabled_toolsets=["incident_snapshot"],
            max_iterations=MAX_TURNS, max_tokens=4096, request_overrides=protocol.request_options(),
            quiet_mode=True, skip_context_files=True, skip_memory=True, load_soul_identity=False,
            save_trajectories=False,
        )
        try:
            if [tool["function"]["name"] for tool in agent.tools] != ["monitor_query"]:
                raise RuntimeError("Enterprise replay may expose only monitor_query")
            set_thread_tool_whitelist({"monitor_query"})
            result = agent.run_conversation(
                prompt, system_message=preloaded + "\n\n" + output_instruction, conversation_history=[],
            )
        finally:
            clear_thread_tool_whitelist()
            agent.close()
    write_json(state / "actor-result.json", {
        "status": "completed" if protocol.completed(result) else "execution_failed",
        "session_id": session_id, "skill_sha256": skill_sha, "final_message": result.get("final_response") or "",
        "api_calls": result.get("api_calls"), "exit_reason": result.get("turn_exit_reason"),
    })


def read_session(state, result, prompt, snapshot, skill_content, query):
    """Verify recorded queries and tool output outside the actor before scoring."""
    database = state / "hermes/state.db"
    session_id = result["session_id"]
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        session = db.execute(
            "SELECT s.parent_session_id, COALESCE(p.prompt, s.system_prompt) AS prompt "
            "FROM sessions s LEFT JOIN system_prompts p ON p.hash=s.system_prompt_hash WHERE s.id=?", (session_id,),
        ).fetchone()
        messages = list(db.execute(
            "SELECT id, role, content, tool_call_id, tool_calls FROM messages WHERE session_id=? ORDER BY id", (session_id,),
        ))
    users = [message for message in messages if message["role"] == "user"]
    final = messages[-1] if messages else None
    if (session is None or session["parent_session_id"] is not None
            or not session["prompt"] or skill_content.decode().strip() not in session["prompt"]
            or sha256(skill_content).hexdigest() != result["skill_sha256"]
            or len(users) != 1 or users[0]["content"] != prompt or final is None
            or final["role"] != "assistant" or final["content"] != result["final_message"]):
        raise RuntimeError("Enterprise SessionDB does not prove a fresh replay with the pinned Skill")
    pending, seen, receipts = {}, set(), []
    for message in messages:
        for call in json.loads(message["tool_calls"] or "[]"):
            if (message["role"] != "assistant" or call["function"]["name"] != "monitor_query"
                    or call["id"] in seen or not users[0]["id"] < message["id"] < final["id"]):
                raise RuntimeError("Unexpected enterprise tool call in SessionDB")
            seen.add(call["id"])
            pending[call["id"]] = message["id"], json.loads(call["function"]["arguments"])
        if message["role"] == "tool":
            call_id, arguments = pending.pop(message["tool_call_id"])
            receipt = json.loads(message["content"])
            if (not call_id < message["id"] < final["id"]
                    or receipt != monitor_lookup(query, snapshot, arguments)):
                raise RuntimeError("Enterprise tool receipt disagrees with the frozen snapshot")
            receipts.append({"call_item_id": call_id, "item_id": message["id"], "receipt": receipt})
    if pending:
        raise RuntimeError("Enterprise SessionDB tool calls lack results")
    return {"prompt_item_id": users[0]["id"], "report_item_id": final["id"],
            "system_prompt_sha256": sha256(session["prompt"].encode()).hexdigest(), "tool_items": receipts,
            "receipts": [item["receipt"] for item in receipts]}


def replay(root, case, skill_content, options, query, *, case_protocol=False):
    root.mkdir(parents=True)
    write_json(root / "task.json", case["input"])
    write_json(root / "snapshot.json", case["snapshot"])
    (root / "SKILL.md").write_bytes(skill_content)
    home = root / "actor-home"
    home.mkdir()
    env = {key: os.environ[key] for key in (
        "PATH", "LANG", "LC_ALL", "TZ", "NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy",
    ) if key in os.environ}
    env.update(HOME=str(home), TMPDIR=str(home), COURSE_ENTERPRISE_MODEL=options["model"],
               COURSE_ENTERPRISE_URL=options["base_url"], COURSE_ENTERPRISE_KEY=options["api_key"])
    try:
        with (root / "actor.log").open("w") as log:
            actor = "--case-actor" if case_protocol else "--actor"
            process = subprocess.run([sys.executable, "-B", str(HERE), actor, str(root)],
                                     cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600)
    except subprocess.TimeoutExpired:
        return {"status": "infrastructure_error", "error": "Hermes actor exceeded its time limit"}
    if process.returncode:
        return {"status": "infrastructure_error", "error": f"Hermes actor exited with {process.returncode}"}
    result = read_json(root / "actor-result.json")
    if result["status"] == "completed":
        if (read_json(root / "task.json") != case["input"] or read_json(root / "snapshot.json") != case["snapshot"]
                or (root / "SKILL.md").read_bytes() != skill_content):
            raise RuntimeError("Enterprise replay inputs changed after they were pinned")
        result.update(read_session(root, result, case["input"]["prompt"] + "\n\n" + instruction(case_protocol),
                                   case["snapshot"], skill_content, query))
    return result


def evaluate(root, pack, options, live):
    """Compare one generic evidence rule on dev, then open holdout after selection."""
    root = root.resolve()
    root.mkdir(parents=True)
    dataset = lesson_module("dataset")
    monitor = lesson_module("monitor")
    grader = lesson_module("grader")
    dataset.verify(pack)
    cases = pack["ready"]
    seen = set()
    for case in cases:
        identity = case["id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+@[a-zA-Z0-9_-]+", identity) or identity in seen:
            raise ValueError("Duplicate or invalid enterprise case identity")
        seen.add(identity)
        if set(case["input"]) != {"prompt"} or case["split"] not in ("dev", "holdout"):
            raise ValueError("Enterprise case must have a public task and a fixed split")
        task = task_scope(case["input"]["prompt"])
        coverage = monitor.scope(case["snapshot"])
        if (task["cluster"] != identity.split("@")[1] or task["cluster"] not in coverage["clusters"]
                or (task["start"], task["end"]) != (coverage["start"], coverage["end"])):
            raise ValueError("Enterprise task scope does not match its frozen snapshot")
    dev = [case for case in cases if case["split"] == "dev"]
    holdout = [case for case in cases if case["split"] == "holdout"]
    baseline = SEED.read_bytes()
    variants = {"baseline": baseline, "candidate": baseline + ("\n\n" + PEER_CHECK + "\n").encode()}
    report = {
        "status": "running", "mode": "live" if live else "local-script-model",
        "model": options["model"] if live else "enterprise-fixture",
        "pack_sha256": pack["pack_sha256"], "scorer_version": pack["versions"]["scorer_version"],
        "skill_sha256": {key: sha256(value).hexdigest() for key, value in variants.items()},
        "selection": None, "dev": [], "holdout": [], "holdout_passed": None,
    }
    report_path = root / "report.json"
    write_json(report_path, report)
    if not live:
        sys.path.insert(0, str(HERE.parents[1]))
        from local_model import serve

    connection = nullcontext(options["base_url"]) if live else serve(respond)

    def paired(split, selected_cases, runtime_options):
        for case in selected_cases:
            pair = {"case": case["id"]}
            for variant, content in variants.items():
                directory = root / split / case["id"] / variant
                result = replay(directory, case, content, runtime_options, monitor.query)
                result["grade"] = grader.grade(case, result)
                write_json(directory / "evaluation.json", result)
                pair[variant] = {"file": str((directory / "evaluation.json").relative_to(root)),
                                 "grade": result["grade"], "session_id": result.get("session_id")}
            report[split].append(pair)
            write_json(report_path, report)

    try:
        if not dev or not holdout:
            report["status"] = "insufficient_ready_cases"
            return report
        with connection as url:
            runtime_options = {**options, "base_url": url, "model": report["model"]}
            paired("dev", dev, runtime_options)
            if any(pair[key]["grade"]["status"] != "scored" for pair in report["dev"] for key in variants):
                report["status"] = "unscored"
                return report
            improved = (all(pair["candidate"]["grade"]["passed"] for pair in report["dev"])
                        and any(not pair["baseline"]["grade"]["passed"] for pair in report["dev"]))
            report["selection"] = "candidate" if improved else "baseline"
            write_json(report_path, report)
            if improved:
                paired("holdout", holdout, runtime_options)
                if any(pair[key]["grade"]["status"] != "scored" for pair in report["holdout"] for key in variants):
                    report["status"] = "unscored"
                    return report
                report["holdout_passed"] = all(pair["candidate"]["grade"]["passed"] for pair in report["holdout"])
            report["status"] = "completed"
            return report
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        write_json(report_path, report)


def main():
    if len(sys.argv) == 3 and sys.argv[1] in {"--actor", "--case-actor"}:
        run_actor(Path(sys.argv[2]).resolve(), case_protocol=sys.argv[1] == "--case-actor")
        return

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True, help="Versioned enterprise pack from fleet.py")
    parser.add_argument("--out", type=Path, required=True, help="Fresh directory for replay results")
    parser.add_argument("--live", action="store_true", help="Use the configured GLM endpoint")
    args = parser.parse_args()
    ensure_loopback_no_proxy()
    if args.live:
        from patrol import model_connection

        connection = model_connection(False)
    else:
        connection = nullcontext("")
    with connection as url:
        options = {
            "base_url": url,
            "api_key": os.environ["GLM_API_KEY"] if args.live else "local-fixture",
            "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if args.live else "enterprise-fixture",
        }
        report = evaluate(args.out, read_json(args.pack), options, args.live)
    print(f"{args.out.resolve() / 'report.json'} ({report['status']})")
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
