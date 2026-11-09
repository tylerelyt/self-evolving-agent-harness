#!/usr/bin/env python3
"""Stage 1: recurring read-only service patrol with durable checkpoints."""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from urllib.error import URLError
import uuid
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from environment import DEFAULT_STATE, SERVICES, classify, get_snapshot, probe_service
from fixture import respond
from hermes_runtime import (ONCALL_SKILL, checkpoint_rows, ensure_loopback_no_proxy, fork,
                            install_oncall_skill, open_sessions, read_oncall_skill, run_turn)

ENVIRONMENT = "lab"
REGION = "local"
STEER_PREFIX = "ONCALL_ALERT "


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def decode_report(content):
    text = content.strip()
    if text.startswith("```") and text.endswith("```") and "\n" in text:
        text = text.split("\n", 1)[1][:-3].strip()
    return json.loads(text)

def connect(state):
    db = sqlite3.connect(state / "patrol.sqlite")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS inspections (
            id INTEGER PRIMARY KEY,
            service TEXT NOT NULL,
            environment TEXT NOT NULL,
            region TEXT NOT NULL,
            checked_at REAL NOT NULL,
            status TEXT NOT NULL,
            observation TEXT NOT NULL,
            summary TEXT NOT NULL,
            history_id INTEGER REFERENCES inspections(id),
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL UNIQUE,
            evidence_item_id INTEGER NOT NULL,
            report_item_id INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS inspections_by_scope
            ON inspections(service, environment, region, id DESC);
        CREATE TABLE IF NOT EXISTS checkpoints (
            service TEXT NOT NULL,
            environment TEXT NOT NULL,
            region TEXT NOT NULL,
            inspection_id INTEGER NOT NULL REFERENCES inspections(id),
            PRIMARY KEY (service, environment, region)
        );
        CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            inspection_id INTEGER NOT NULL REFERENCES inspections(id),
            steer_item_id INTEGER NOT NULL,
            steer_delivery TEXT NOT NULL,
            probe_item_id INTEGER NOT NULL,
            recovered_inspection_id INTEGER REFERENCES inspections(id)
        );
        CREATE TABLE IF NOT EXISTS incident_skills (
            incident_id TEXT PRIMARY KEY REFERENCES incidents(incident_id),
            skill_name TEXT NOT NULL,
            skill_sha256 TEXT NOT NULL,
            skill_item_id INTEGER NOT NULL
        );
    """)
    return db


def scope(service):
    return service, ENVIRONMENT, REGION


def history(db, service, limit=5):
    rows = db.execute("""
        SELECT * FROM inspections
        WHERE service = ? AND environment = ? AND region = ?
        ORDER BY id DESC LIMIT ?
    """, (*scope(service), limit))
    return [dict(row) for row in rows]


def checkpoint(db, service):
    row = db.execute("""
        SELECT i.* FROM checkpoints c JOIN inspections i ON i.id = c.inspection_id
        WHERE c.service = ? AND c.environment = ? AND c.region = ?
    """, scope(service)).fetchone()
    return dict(row) if row else None


def incidents(db, service):
    rows = db.execute("""
        SELECT n.*, i.session_id, i.turn_id, i.evidence_item_id, i.report_item_id,
               i.observation, i.summary, s.skill_name, s.skill_sha256, s.skill_item_id
        FROM incidents n JOIN inspections i ON i.id = n.inspection_id
        LEFT JOIN incident_skills s ON s.incident_id = n.incident_id
        WHERE i.service = ? AND i.environment = ? AND i.region = ?
        ORDER BY n.inspection_id DESC
    """, scope(service))
    return [dict(row) for row in rows]


def publish(db, service, observation, report, session_id, turn_id, evidence, message, incident, active):
    with db:
        record_id = db.execute("""
            INSERT INTO inspections (
                service, environment, region, checked_at, status, observation, summary,
                history_id, session_id, turn_id, evidence_item_id, report_item_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (*scope(service), observation["window_end"], report["status"], dump(observation),
              report["summary"], report["history_id"], session_id, turn_id,
              evidence["id"], message["id"])).lastrowid
        db.execute("""
            INSERT INTO checkpoints VALUES (?, ?, ?, ?)
            ON CONFLICT(service, environment, region) DO UPDATE SET inspection_id = excluded.inspection_id
        """, (*scope(service), record_id))
        if incident:
            db.execute("""
                INSERT INTO incidents (incident_id, inspection_id, steer_item_id, steer_delivery, probe_item_id)
                VALUES (?, ?, ?, ?, ?)
            """, (incident["incident_id"], record_id, incident["steer_item_id"],
                  incident["steer_delivery"], incident["probe_item_id"]))
            db.execute("INSERT INTO incident_skills VALUES (?, ?, ?, ?)",
                       (incident["incident_id"], incident["skill_name"],
                        incident["skill_sha256"], incident["skill_item_id"]))
        elif active and report["status"] == "healthy" and observation["error_rate"] == 0:
            db.execute("UPDATE incidents SET recovered_inspection_id = ? WHERE incident_id = ?",
                       (record_id, active["incident_id"]))
    record = checkpoint(db, service)
    if incident:
        record.update(incident)
    return record


def inspect_service(sessions, db, service, state, options, skill_sha, *,
                    snapshot=get_snapshot, probe_request=probe_service, capture_session=False, langfuse=False):
    last = checkpoint(db, service)
    session_id = last["session_id"] if last else "patrol-" + service + "-" + uuid.uuid4().hex
    turn_id = uuid.uuid4().hex
    active = next((row for row in incidents(db, service) if row["recovered_inspection_id"] is None), None)
    alert_id = None
    skill_read = False

    def check_alert(steer, name, receipt):
        nonlocal alert_id
        if name != "service_snapshot" or active or alert_id:
            return
        metrics = json.loads(receipt)
        if metrics.get("error") or classify(metrics) != "abnormal":
            return
        candidate = f"lab-{service}-{turn_id[:12]}"
        alert = dump({"incident_id": candidate, "service": service,
                      "error_rate": metrics["error_rate"], "p95_ms": metrics["p95_ms"]})
        instruction = (
            f"{STEER_PREFIX}{alert}\n"
            f"切换到 On-call 调查。先用 skill_view 读取 {ONCALL_SKILL} 并遵循其中步骤。"
            "仍按巡检格式输出，增加 incident_id 与 probe_http_status。"
            "在 summary 写明已确认的影响、尚不确定的原因和建议处置，保留历史对照。"
            "只做调查和交接，环境开关由学员操作。"
        )
        if steer(instruction):
            alert_id = candidate

    def load_skill():
        nonlocal skill_read
        if not alert_id:
            return {"error": "No On-call steer was accepted for this turn"}
        receipt = read_oncall_skill(skill_sha)
        skill_read = True
        return receipt

    def probe():
        if not skill_read:
            return {"error": "Read the assigned On-call Skill before probing"}
        try:
            return probe_request(state, service)
        except (URLError, OSError, ValueError, KeyError) as exc:
            return {"error": f"Cannot probe {service}: {exc}"}

    def current():
        try:
            return dict(snapshot(state, service), history_available=last is not None)
        except (URLError, OSError, ValueError, KeyError, RuntimeError) as exc:
            return {"error": f"Cannot read {service} metrics: {exc}"}

    def previous():
        return [{key: row[key] for key in ("id", "checked_at", "status", "summary")}
                for row in history(db, service, 3)]

    prompt = (
        f"巡检 {service}/{ENVIRONMENT}/{REGION}，巡检编号 {turn_id}。"
        "先调用 service_snapshot 获取当前窗口。有历史可对照时调用 search_inspections。"
        "读取失败则报告 unavailable。低于 10 个请求判 insufficient。"
        "否则 5xx 达到 5% 或 P95 达到 500ms 判 abnormal，其余 healthy。"
        "只输出 JSON 对象，包含 status、history_id（引用的历史记录整数 ID，未引用为 null）"
        "和中文 summary（写明请求数、5xx 比例、P95 及判断依据）。"
    )
    tools = {"service_snapshot": current, "search_inspections": previous}
    if skill_sha:
        tools.update(skill_view=load_skill, oncall_probe=probe)
        prompt += "只有收到本轮 Hermes steer 的 ONCALL_ALERT 才进入 On-call 调查。"
    rows, transcript = run_turn(
        sessions, session_id, prompt, tools, options, check_alert if skill_sha else None,
        capture_session=capture_session, task_id=turn_id if langfuse else None,
    )
    calls = {call["id"]: call["function"]["name"] for row in rows for call in row.get("tool_calls") or []}
    evidence = next((row for row in reversed(rows)
                     if row["role"] == "tool" and calls.get(row["tool_call_id"]) == "service_snapshot"), None)
    message = rows[-1] if rows and rows[-1]["role"] == "assistant" else None
    if not evidence or not message or not message["content"]:
        raise RuntimeError(f"Session {session_id} lacks a persisted metrics receipt or report")
    observation = json.loads(evidence["content"])
    if observation.get("error"):
        raise RuntimeError(observation["error"])
    report = decode_report(message["content"])
    retrieved = {item["id"] for row in rows if row["role"] == "tool"
                 and calls.get(row["tool_call_id"]) == "search_inspections"
                 for item in json.loads(row["content"]) if isinstance(item, dict)}
    if (report["status"] != classify(observation) or not isinstance(report["summary"], str)
            or not report["summary"].strip()
            or (report["history_id"] is not None
                and (type(report["history_id"]) is not int or report["history_id"] not in retrieved))):
        raise RuntimeError(f"Turn {turn_id} did not produce a report supported by the evidence")
    incident = None
    if alert_id:
        from agent.prompt_builder import STEER_MARKER_OPEN
        marker = f"{STEER_MARKER_OPEN}\n{STEER_PREFIX}"
        steered = next((msg for msg in transcript if msg["role"] == "tool"
                        and marker in msg["content"] and alert_id in msg["content"]), None)
        delivery = next((row for row in rows if row["role"] == "tool" and steered
                         and row["tool_call_id"] == steered["tool_call_id"]), None)
        skill_item = next((row for row in rows if row["role"] == "tool" and delivery
                           and row["id"] > delivery["id"]
                           and calls.get(row["tool_call_id"]) == "skill_view"
                           and json.loads(row["content"]).get("sha256") == skill_sha), None)
        probe_item = next((row for row in rows if row["role"] == "tool" and skill_item
                           and row["id"] > skill_item["id"]
                           and calls.get(row["tool_call_id"]) == "oncall_probe"
                           and json.loads(row["content"]).get("http_status") == report.get("probe_http_status")
                           and "http_status" in json.loads(row["content"])), None)
        probe_status = json.loads(probe_item["content"]).get("http_status") if probe_item else None
        if (not skill_item or not probe_status or report.get("incident_id") != alert_id
                or report.get("probe_http_status") != probe_status):
            raise RuntimeError(f"{alert_id}: On-call report lacks a verified Skill read and a matching HTTP probe")
        incident = {
            "incident_id": alert_id, "steer_item_id": delivery["id"],
            "steer_delivery": steered["content"][steered["content"].index(marker):],
            "probe_item_id": probe_item["id"],
            "skill_name": ONCALL_SKILL, "skill_sha256": skill_sha, "skill_item_id": skill_item["id"],
        }
    observation.pop("history_available", None)
    return publish(db, service, observation, report, session_id, turn_id, evidence, message, incident, active)


def show_checkpoint(record, session_id, items):
    show({"session_id": session_id, "turn_id": record["turn_id"],
          "source_session_id": record["session_id"],
          "evidence_item": items[record["evidence_item_id"]],
          "report_item": items[record["report_item_id"]]})


def show(value):
    print(dump(value), flush=True)


def run(state, db, sessions, options, interval, sample, rounds, oncall, *,
        environment_state=None, skill_file=None, capture_session=False, services=SERVICES, langfuse=False):
    skill_sha = install_oncall_skill(state, skill_file) if oncall else None
    order = sorted(dict.fromkeys(services), key=lambda service: (checkpoint(db, service) or {}).get("id", 0))
    round_number = 0
    while rounds is None or round_number < rounds:
        selected = order[:sample]
        with writer_lock(state):
            for service in selected:
                try:
                    show(inspect_service(
                        sessions, db, service, environment_state or state, options, skill_sha,
                        capture_session=capture_session, langfuse=langfuse,
                    ))
                except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
                    if rounds is not None:
                        raise RuntimeError(f"{service}: {exc}") from exc
                    print(f"{service}: {exc}", file=sys.stderr, flush=True)
        order = order[sample:] + selected
        round_number += 1
        if rounds is None or round_number < rounds:
            time.sleep(interval)


@contextmanager
def writer_lock(state, database="writer.sqlite", timeout=30):
    # An SQLite transaction on a separate lock database allows read-only history
    # commands to proceed while one patrol/fork/resume owns the runtime.
    with closing(sqlite3.connect(state / database, timeout=timeout)) as lock:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("This state is already in use by a patrol runtime command") from exc
        yield


def model_connection(offline):
    if offline:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from local_model import serve
        return serve(respond)
    if not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("Set GLM_API_KEY for model inference, or pass run --offline for a wiring test")
    return nullcontext(os.environ.get("GLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--interval", type=float, default=10)
    runner.add_argument("--sample", type=int, choices=range(1, len(SERVICES) + 1), default=2)
    runner.add_argument("--service", dest="services", choices=SERVICES, action="append",
                        help="Limit patrol to an assigned service; repeat for multiple services")
    runner.add_argument("--rounds", type=int, help="Exit after this many rounds for a smoke test")
    runner.add_argument("--offline", action="store_true", help="Use a local scripted model for wiring tests")
    runner.add_argument("--oncall", action="store_true", help="Steer the active agent into On-call on abnormal metrics")
    runner.add_argument("--environment-state", type=Path, help="Use a separately started lab environment")
    runner.add_argument("--skill-file", type=Path, help="Use a pinned On-call Skill for this patrol run")
    runner.add_argument("--capture-url", help="SkillClaw Client Proxy endpoint for this instance")
    runner.add_argument("--langfuse", action="store_true", help="Send native traces to the configured Langfuse project")
    for command in ("history", "resume", "fork", "incidents"):
        commands.add_parser(command).add_argument("service", choices=SERVICES)
    args = parser.parse_args()
    if args.command == "run" and (
        not math.isfinite(args.interval) or args.interval < 0
        or (args.rounds is None and args.interval <= 0)
        or (args.rounds is not None and args.rounds < 1)
    ):
        parser.error("Use a positive interval for continuous patrol and a positive round count for tests")
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    with closing(connect(state)) as db:
        if args.command in ("history", "incidents"):
            query = history if args.command == "history" else incidents
            for row in query(db, args.service):
                show(row)
            return
        if args.command == "run":
            ensure_loopback_no_proxy()
            environment_state = args.environment_state or state
            if not (environment_state / "environment.json").exists():
                raise RuntimeError("Start environment.py serve before running patrol")
            if not args.offline and not os.environ.get("GLM_API_KEY", "").strip():
                raise RuntimeError("Set GLM_API_KEY or use --offline")
            connection = nullcontext(args.capture_url) if args.capture_url else model_connection(args.offline)
            with (writer_lock(state, "runner.sqlite", timeout=0),
                  connection as url, open_sessions(state, langfuse=args.langfuse) as sessions):
                options = {
                    "base_url": url,
                    "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
                    "model": "patrol-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2",
                }
                run(
                    state, db, sessions, options, args.interval, args.sample, args.rounds, args.oncall,
                    environment_state=environment_state, skill_file=args.skill_file,
                    capture_session=bool(args.capture_url),
                    services=args.services or SERVICES, langfuse=args.langfuse,
                )
            return
        with writer_lock(state), open_sessions(state) as sessions:
            record = checkpoint(db, args.service)
            if not record:
                raise RuntimeError(f"No completed inspection for {args.service}")
            if args.command == "resume":
                items = {row["id"]: row for row in checkpoint_rows(sessions, record)}
                show_checkpoint(record, record["session_id"], items)
            else:
                child, items = fork(sessions, record)
                show_checkpoint(record, child, items)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Patrol stopped", file=sys.stderr)
    except (OSError, RuntimeError, sqlite3.Error, ValueError, KeyError, ImportError) as exc:
        sys.exit(str(exc))
