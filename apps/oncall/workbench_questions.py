"""Answer an engineer's question in a new Hermes branch of a saved incident."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import URLError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "patrol"))

from environment import SERVICES, classify, get_snapshot, load_json, write_json
from hermes_runtime import TOOL_DESCRIPTIONS, checkpoint_rows, ensure_loopback_no_proxy, fork, open_sessions, run_turn
from local_model import serve as scripted_model
from patrol import connect, decode_report, incidents, model_connection, writer_lock

QUESTION_PREFIX = "ONCALL_ENGINEER_QUESTION "


def find_incident(state, incident_id):
    with closing(connect(state, readonly=True)) as db:
        for service in SERVICES:
            record = next((row for row in incidents(db, service) if row["incident_id"] == incident_id), None)
            if record is not None:
                return service, record
    raise ValueError("The selected incident is not recorded in this worker state")


def recorded_artifact(state, incident_id, filename):
    for directory in (state / "pages" / incident_id, state):
        path = directory / filename
        if path.is_file():
            artifact = load_json(path)
            if artifact.get("incident_id") == incident_id:
                return {"path": str(path), "content": artifact}
    return None


def incident_evidence(state, service, record, initial_report):
    """Read saved observations separately from the current HTTP state."""
    with closing(connect(state, readonly=True)) as db:
        current = next(row for row in incidents(db, service) if row["incident_id"] == record["incident_id"])
        recovered = current["recovered_inspection_id"]
        recovery = db.execute("SELECT * FROM inspections WHERE id = ?", (recovered,)).fetchone() if recovered else None
    observation = json.loads(record["observation"])
    result = {
        "kind": "recorded_incident_evidence", "read_at": time.time(), "incident_id": record["incident_id"],
        "scope": {"service": service, "environment_id": observation["environment_id"]},
        "initial": {"session_id": record["session_id"], "report_item_id": record["report_item_id"],
                    "evidence_item_id": record["evidence_item_id"], "skill_sha256": record["skill_sha256"],
                    "observation": observation, "report": initial_report},
        "recorded_incident_status": "recovered" if recovered else "open",
        "followup": recorded_artifact(state, record["incident_id"], "investigation.json"),
        "page": recorded_artifact(state, record["incident_id"], "page.json"),
        "recovery": dict(recovery) if recovery is not None else None,
        "current_http_state": "Query service_snapshot to check the present state separately",
    }
    if result["recovery"] is not None:
        result["recovery"]["observation"] = json.loads(result["recovery"]["observation"])
    return result


def scripted_reply(messages):
    results = {}
    calls = {}
    question = ""
    for message in messages:
        if message["role"] == "user":
            results.clear()
            calls.clear()
            question = message["content"]
        elif message["role"] == "assistant":
            calls.update({call["id"]: call["function"]["name"] for call in message.get("tool_calls") or []})
        elif message["role"] == "tool" and message.get("tool_call_id") in calls:
            results[calls[message["tool_call_id"]]] = json.loads(message["content"])
    if "incident_evidence" not in results:
        return "incident_evidence", {}
    if "service_snapshot" not in results:
        return "service_snapshot", {}
    header = question.partition("\n")[0]
    task = json.loads(header.removeprefix(QUESTION_PREFIX))
    saved = results["incident_evidence"]
    snapshot = results["service_snapshot"]
    initial = saved["initial"]
    statuses = initial["report"].get("probe_http_statuses", [])
    parts = [f"关于「{task['question']}」，我回查了事故 {saved['incident_id']}，并重新查询了当前指标。",
             f"历史初判中的探测结果为 {statuses}，来源是会话 {initial['session_id']} 的报告 Item {initial['report_item_id']}。"]
    if saved["followup"] is not None:
        followup = saved["followup"]["content"]
        parts.append(f"补查记录中的探测结果为 {followup['report'].get('probe_http_statuses', [])}。"
                     f"{followup['report'].get('analysis', '未记录补查分析。')}来源是分支会话 {followup['session_id']}，"
                     f"报告 Item {followup['report_item_id']}。")
    parts.append("事故记录已关联恢复检查点。" if saved["recovery"] is not None else "事故记录暂未关联恢复检查点。")
    if snapshot.get("error"):
        parts.append(f"当前查询未取得同环境的有效指标，原因是{snapshot['error']}。目前只能核对历史记录，当前服务状态仍待确认。")
    else:
        metrics = snapshot["observation"]
        status = {"abnormal": "仍有异常", "healthy": "当前窗口健康", "insufficient": "当前有效请求不足"}[snapshot["status"]]
        parts.append(f"这次查询的指标窗口为 {metrics['window_start']:.3f} 至 {metrics['window_end']:.3f}，"
                     f"有 {metrics['requests']} 个请求，5xx 比例为 {metrics['error_rate']}，{status}。"
                     "这条指标属于本次查询，历史探测结果保留在原事故里。")
    return None, "\n\n".join(parts)


def ask(state, environment_state, incident_id, question, *, live=False):
    ensure_loopback_no_proxy()
    service, record = find_incident(state, incident_id)
    original_scope = json.loads(record["observation"])
    TOOL_DESCRIPTIONS["incident_evidence"] = "Read this incident's recorded initial report, follow-up, page and recovery provenance."
    evidence_read = False

    with open_sessions(state) as sessions:
        with writer_lock(state):
            prefix = checkpoint_rows(sessions, record)
            initial_report = decode_report(prefix[-1]["content"])
            child, copies = fork(sessions, record)

        def history():
            nonlocal evidence_read
            result = incident_evidence(state, service, record, initial_report)
            evidence_read = True
            return result

        def current():
            if not evidence_read:
                return {"error": "Read incident_evidence before querying the current state"}
            try:
                observation = get_snapshot(environment_state, service)
                if observation["environment_id"] != original_scope["environment_id"]:
                    return {"error": "The running environment differs from the selected incident's environment",
                            "expected_environment_id": original_scope["environment_id"],
                            "observed_environment_id": observation["environment_id"]}
                return {"kind": "current_http_state", "queried_at": time.time(),
                        "status": classify(observation), "observation": observation}
            except (OSError, URLError, ValueError, KeyError, RuntimeError) as exc:
                return {"error": str(exc), "queried_at": time.time()}

        task = {"incident_id": incident_id, "service": service, "environment_id": original_scope["environment_id"],
                "question": question}
        prompt = QUESTION_PREFIX + json.dumps(task, ensure_ascii=False) + "\n" + (
            "工程师正在追问这起事故。先调用 incident_evidence 核对原始初判、补查、交接与恢复记录，"
            "再调用 service_snapshot 查询同一环境的当前指标。用自然语言直接回答问题，并引用来源 Session 和 Item。"
            "明确区分历史观察和当前指标，证据不足时说明还需要核实什么。工具只读。"
        )
        connection = model_connection(False) if live else scripted_model(scripted_reply)
        with connection as url:
            options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if live else "local-fixture",
                       "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if live else "oncall-question-script"}
            rows, _ = run_turn(sessions, child, prompt,
                               {"incident_evidence": history, "service_snapshot": current}, options, max_iterations=6)
        if checkpoint_rows(sessions, record) != prefix:
            raise RuntimeError("The source incident report changed during the question")
        tool_rows = {row.get("tool_name"): row for row in rows if row["role"] == "tool"}
        if not {"incident_evidence", "service_snapshot"} <= tool_rows.keys():
            raise RuntimeError("The answer did not query both incident history and the current state")
        final = rows[-1]
        if final["role"] != "assistant" or not final.get("content"):
            raise RuntimeError("Hermes did not leave an answer in the question session")
        answer = final["content"]
        if not live:
            answer = json.loads(answer)
        return {"status": "completed", "mode": "live" if live else "local-script-model", "model": options["model"],
                "incident_id": incident_id, "question": question, "answer": answer,
                "source_session_id": record["session_id"], "source_report_item_id": record["report_item_id"],
                "source_report": initial_report, "session_id": child, "child_session_id": child,
                "copied_report_item_id": copies[record["report_item_id"]]["id"],
                "report_item_id": final["id"], "created_at": time.time(),
                "tools": {name: {"item_id": row["id"], "receipt": json.loads(row["content"])}
                          for name, row in tool_rows.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--environment-state", type=Path, required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.question.strip():
        parser.error("Provide a nonempty engineer question")
    state = args.state.resolve()
    output = args.out.resolve()
    if not (state / "patrol.sqlite").is_file() or not (state / "hermes/state.db").is_file():
        parser.error("Use a worker state containing the selected incident and its Hermes session")
    if output.exists():
        parser.error("Use a new result path for each question")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = ask(state, args.environment_state.resolve(), args.incident_id, args.question.strip(), live=args.live)
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
        write_json(output, {"status": "failed", "incident_id": args.incident_id, "question": args.question, "error": str(exc)})
        print(str(exc), file=sys.stderr)
        return 1
    write_json(output, result)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
