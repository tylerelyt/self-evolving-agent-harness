#!/usr/bin/env python3
"""Stage 1: recurring read-only service patrol with durable checkpoints."""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from urllib.error import URLError
import uuid
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from environment import (ALERT_SOURCE, DEFAULT_STATE, SERVICES, classify, get_snapshot, load_json,
                         probe_service, read_alert, write_json)
from fixture import respond
from hermes_runtime import (ONCALL_SKILL, PATROL_SKILL, checkpoint_rows, ensure_loopback_no_proxy, fork,
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

def connect(state, *, readonly=False):
    path = state / "patrol.sqlite"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if readonly else sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    if readonly:
        return db
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


def read_live_status(environment_state, service):
    try:
        metrics = get_snapshot(environment_state, service)
        environment_id = metrics["environment_id"]
        if (metrics["source"] != "live_lab_http_requests"
                or not isinstance(environment_id, str) or not environment_id):
            raise ValueError("Invalid lab metrics scope")
        return {"state": "reachable", "environment_id": environment_id, "health": classify(metrics),
                "requests": metrics["requests"], "window_end": metrics["window_end"]}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        return {"state": "unavailable", "error": type(exc).__name__}


def record_scope(record, live):
    environment_id = json.loads(record["observation"])["environment_id"]
    observed_id = live.get("environment_id")
    return {"environment_id": environment_id,
            "matches_environment": None if observed_id is None else environment_id == observed_id}


def read_status(state, environment_state):
    """Summarize live HTTP readiness and durable evidence without opening an Agent session."""
    has_manifest = (environment_state / "environment.json").is_file()
    services = {
        service: {"live": read_live_status(environment_state, service) if has_manifest else {"state": "not_started"},
                  "checkpoint": None, "last_incident": None}
        for service in SERVICES
    }
    live_ids = {entry["live"].get("environment_id") for entry in services.values()}
    if not has_manifest:
        environment_status = "not_started"
    elif None in live_ids:
        environment_status = "unavailable"
    elif len(live_ids) != 1:
        environment_status = "mixed_environment"
    else:
        environment_status = "ready"

    database = state / "patrol.sqlite"
    if database.is_file():
        with closing(connect(state, readonly=True)) as db:
            db.execute("BEGIN")
            for service, entry in services.items():
                record = checkpoint(db, service)
                if record:
                    evidence = ("id", "checked_at", "status", "session_id", "turn_id",
                                "evidence_item_id", "report_item_id")
                    entry["checkpoint"] = {key: record[key] for key in evidence}
                    entry["checkpoint"].update(record_scope(record, entry["live"]))
                saved_incidents = incidents(db, service)
                if saved_incidents:
                    latest = saved_incidents[0]
                    incident_id = latest["incident_id"]
                    receipt = state / "pages" / incident_id / "page.json"
                    entry["last_incident"] = {
                        **record_scope(latest, entry["live"]),
                        "incident_id": incident_id, "session_id": latest["session_id"],
                        "report_item_id": latest["report_item_id"],
                        "recovered_inspection_id": latest["recovered_inspection_id"],
                        "page_receipt": str(receipt) if Path(incident_id).name == incident_id and receipt.is_file() else None,
                    }
    return {"schema": "capstone-patrol-status-v1", "state": str(state),
            "environment": {"state": str(environment_state), "status": environment_status},
            "patrol_database": str(database) if database.is_file() else None, "services": services}


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


def steer_oncall(steer, alert):
    return steer(f"{STEER_PREFIX}{dump(alert)}\n"
                 f"用 skill_view 读取 {ONCALL_SKILL}，执行其中的告警初判任务。")


def inspect_service(sessions, db, service, state, options, skill_sha, *,
                    patrol_skill_sha, snapshot=get_snapshot, probe_request=probe_service,
                    capture_session=False, langfuse=False,
                    fresh_session=False, auto_recover=True, incoming_alert=None, route_alert=steer_oncall):
    last = checkpoint(db, service)
    session_id = last["session_id"] if last and not fresh_session else "patrol-" + service + "-" + uuid.uuid4().hex
    turn_id = uuid.uuid4().hex
    records = incidents(db, service)
    seen_alerts = {row["incident_id"] for row in records}
    open_incidents = [row for row in records if row["recovered_inspection_id"] is None]
    active = None
    alert_id = None
    skill_read = False
    patrol_read = False
    observed_environment = None

    def deliver_alert(steer, name, receipt):
        nonlocal alert_id
        if name != "service_snapshot" or active or alert_id:
            return
        metrics = json.loads(receipt)
        if metrics.get("error"):
            return
        alert = incoming_alert if incoming_alert is not None else read_alert(
            state, service, seen_ids=seen_alerts, environment_id=metrics["environment_id"],
        )
        if not alert:
            return
        candidate = alert["incident_id"]
        emitted_at = alert["emitted_at"]
        if (alert["source"] != ALERT_SOURCE or alert["service"] != service
                or alert["environment_id"] != metrics["environment_id"]
                or type(emitted_at) not in (int, float) or not 0 < emitted_at <= metrics["window_end"]
                or not re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", candidate) or candidate in seen_alerts):
            return
        if route_alert(steer, alert):
            alert_id = candidate

    def load_skill():
        nonlocal skill_read
        if not alert_id:
            return {"error": "No On-call steer was accepted for this turn"}
        receipt = read_oncall_skill(skill_sha)
        skill_read = True
        return receipt

    def load_patrol_skill():
        nonlocal patrol_read
        receipt = read_oncall_skill(patrol_skill_sha, name=PATROL_SKILL)
        patrol_read = True
        return receipt

    def probe():
        if not skill_read:
            return {"error": "Read the assigned On-call Skill before probing"}
        try:
            receipt = probe_request(state, service, environment_id=observed_environment)
            if not receipt.get("error") and (receipt.get("service") != service
                    or receipt.get("environment_id") != observed_environment):
                return {"error": "Probe response belongs to a different service or environment"}
            return receipt
        except (URLError, OSError, ValueError, KeyError) as exc:
            return {"error": f"Cannot probe {service}: {exc}"}

    def current():
        nonlocal active, observed_environment
        if not patrol_read:
            return {"error": "Read the assigned patrol Skill before taking measurements"}
        try:
            metrics = snapshot(state, service)
            observed_environment = metrics["environment_id"]
            active = next((row for row in open_incidents if json.loads(row["observation"])["environment_id"]
                           == metrics["environment_id"]), None)
            return dict(metrics, history_available=last is not None)
        except (URLError, OSError, ValueError, KeyError, RuntimeError) as exc:
            return {"error": f"Cannot read {service} metrics: {exc}"}

    def previous():
        return [{key: row[key] for key in ("id", "checked_at", "status", "summary")}
                for row in history(db, service, 3)]

    prompt = (
        f"巡检 {service}/{ENVIRONMENT}/{REGION}，巡检编号 {turn_id}。"
        f"调用 patrol_skill_view 读取 {PATROL_SKILL}，按其中的例行巡检任务执行。"
    )
    # Keep patrol method reads separate from the On-call SkillClaw evolution target.
    tools = {"patrol_skill_view": load_patrol_skill, "service_snapshot": current, "search_inspections": previous}
    if skill_sha:
        tools.update(skill_view=load_skill, oncall_probe=probe)
        prompt += "只有收到本轮 Hermes steer 的 ONCALL_ALERT 才进入 On-call 调查。"
    rows, transcript = run_turn(
        sessions, session_id, prompt, tools, options, deliver_alert if skill_sha else None,
        capture_session=capture_session, task_id=turn_id if langfuse else None, max_iterations=7,
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
    return publish(db, service, observation, report, session_id, turn_id, evidence, message, incident,
                   active if auto_recover else None)


def show_checkpoint(record, session_id, items):
    show({"session_id": session_id, "turn_id": record["turn_id"],
          "source_session_id": record["session_id"],
          "evidence_item": items[record["evidence_item_id"]],
          "report_item": items[record["report_item_id"]]})


def show(value):
    print(dump(value), flush=True)


def run(state, db, sessions, options, interval, sample, rounds, oncall, *,
        environment_state=None, skill_file=None, capture_session=False, services=SERVICES, langfuse=False,
        fleet=False, page_oncall=False, oncall_steps=None, incident_id=None):
    skill_sha = install_oncall_skill(state, skill_file) if oncall else None
    patrol_skill_sha = install_oncall_skill(state, name=PATROL_SKILL)
    environment_state = environment_state or state
    route_alert = oncall_steps.route_alert if oncall_steps else steer_oncall
    handoff = (dict(branch_report=oncall_steps.branch_report, notify=oncall_steps.notify_operator)
               if oncall_steps else {})
    order = sorted(dict.fromkeys(services), key=lambda service: (checkpoint(db, service) or {}).get("id", 0))
    round_number = 0
    while rounds is None or round_number < rounds:
        selected = order[:sample]
        for service in selected:
            try:
                if page_oncall:
                    page_pending_incident(state, db, sessions, service, environment_state, options, langfuse, **handoff)
                with writer_lock(state):
                    if fleet:
                        record = inspect_fleet_service(state, sessions, db, service, environment_state,
                                                       options, langfuse, patrol_skill_sha,
                                                       route_alert=route_alert)
                    else:
                        if oncall and skill_file is not None:
                            skill_sha = install_oncall_skill(state, skill_file)
                        pending = read_alert(
                            environment_state, service,
                            seen_ids={item["incident_id"] for item in incidents(db, service)},
                            environment_id=get_snapshot(environment_state, service)["environment_id"],
                        ) if capture_session else None
                        # A proxy-captured incident needs a fresh Session and its
                        # native ID header from the first model request onward.
                        # An alert arriving during this ordinary patrol Turn is
                        # picked up on the next Turn, before those choices are made.
                        selected_alert = (pending or {}) if capture_session else None
                        if incident_id is not None:
                            selected_alert = load_json(environment_state / "alerts" / service / f"{incident_id}.json")
                        capture_incident = capture_session and bool(selected_alert)
                        record = inspect_service(
                            sessions, db, service, environment_state, options, skill_sha,
                            patrol_skill_sha=patrol_skill_sha,
                            capture_session=capture_incident, langfuse=langfuse,
                            auto_recover=not page_oncall, route_alert=route_alert,
                            fresh_session=capture_incident,
                            incoming_alert=selected_alert,
                        )
                    if record is not None:
                        show(record)
                    if page_oncall:
                        check_paged_recovery(state, db, service, record)
                if page_oncall and "incident_id" in record:
                    page_pending_incident(state, db, sessions, service, environment_state, options, langfuse, **handoff)
            except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
                if rounds is not None:
                    raise RuntimeError(f"{service}: {exc}") from exc
                print(f"{service}: {exc}", file=sys.stderr, flush=True)
        order = order[sample:] + selected
        round_number += 1
        if rounds is None or round_number < rounds:
            time.sleep(interval)


def page_pending_incident(state, db, sessions, service, environment_state, options, langfuse, *,
                          branch_report=fork, notify=None):
    from investigation import investigate, page_operator

    active = next((record for record in incidents(db, service) if record["recovered_inspection_id"] is None), None)
    if active is None:
        return
    inbox = state / "pages" / active["incident_id"]
    page_path = inbox / "page.json"
    if page_path.is_file():
        return
    current = get_snapshot(environment_state, service)
    if current["environment_id"] != json.loads(active["observation"])["environment_id"]:
        return
    inbox.mkdir(parents=True, exist_ok=True)
    report_path = inbox / "investigation.json"
    if report_path.is_file():
        report = load_json(report_path)
    else:
        report = investigate(state, environment_state, active, options, langfuse=langfuse, sessions=sessions,
                             branch_report=branch_report)
        write_json(report_path, report)
    scope = f"{report['environment_id']}/{service}"
    (notify or page_operator)(page_path, "lab-oncall-operator", scope, report, report_path.name)


def link_recovery(db, incident_id, inspection_id):
    with db:
        return db.execute("""UPDATE incidents SET recovered_inspection_id = ?
            WHERE incident_id = ? AND recovered_inspection_id IS NULL""", (inspection_id, incident_id)).rowcount == 1


def check_paged_recovery(state, db, service, record):
    if record["status"] != "healthy":
        return
    active = next((item for item in incidents(db, service) if item["recovered_inspection_id"] is None), None)
    if active is None:
        return
    page_path = state / "pages" / active["incident_id"] / "page.json"
    if not page_path.is_file():
        return
    page = load_json(page_path)
    snapshot = json.loads(record["observation"])
    if (snapshot["error_rate"] != 0 or snapshot["environment_id"] != page["report"]["environment_id"]
            or snapshot["window_start"] <= page["paged_at"]):
        return
    prior = db.execute("""SELECT 1 FROM inspections WHERE service = ? AND environment = ? AND region = ?
        AND status = 'healthy' AND checked_at < ? AND json_extract(observation, '$.window_start') > ?
        AND json_extract(observation, '$.error_rate') = 0 AND json_extract(observation, '$.environment_id') = ?
        LIMIT 1""", (*scope(service), snapshot["window_start"], page["paged_at"], snapshot["environment_id"])).fetchone()
    if prior:
        link_recovery(db, active["incident_id"], record["id"])


def inspect_fleet_service(state, sessions, db, service, environment_state, options, langfuse, patrol_skill_sha, *,
                          route_alert=steer_oncall):
    try:
        dispatch = load_json(state / "dispatch.json")
    except FileNotFoundError:
        dispatch = None
    if dispatch:
        directory = Path(dispatch["directory"])
        incident_path = directory / "detected.json"
        if incident_path.is_file():
            if service == dispatch["service"] and not (directory / "page.json").is_file():
                return None
        elif (service == dispatch["service"] and (alert := read_alert(environment_state, service))
              and alert["emitted_at"] >= dispatch["armed_at"]):
            skill_sha = install_oncall_skill(state, Path(dispatch["skill"]))
            if skill_sha != dispatch["skill_sha256"]:
                raise RuntimeError("Fleet worker received an unapproved Skill")
            record = inspect_service(
                sessions, db, service, environment_state, {**options, "base_url": dispatch["capture_url"]},
                skill_sha, patrol_skill_sha=patrol_skill_sha, capture_session=True, langfuse=langfuse,
                fresh_session=True, auto_recover=False, incoming_alert=alert,
                route_alert=route_alert,
            )
            if "incident_id" in record:
                write_json(directory / "alert.json", alert)
                write_json(incident_path, {"pid": os.getpid(), "incident_id": record["incident_id"]})
            return record
    return inspect_service(sessions, db, service, environment_state, options, None,
                           patrol_skill_sha=patrol_skill_sha, auto_recover=False)


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


def main(argv=None, *, oncall_steps=None):
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
    runner.add_argument("--page-oncall", action="store_true", help="Investigate, page the local operator and continue patrol")
    runner.add_argument("--environment-state", type=Path, help="Use a separately started lab environment")
    runner.add_argument("--skill-file", type=Path, help="Use a pinned On-call Skill for this patrol run")
    runner.add_argument("--capture-url", help="SkillClaw Client Proxy endpoint for this instance")
    runner.add_argument("--langfuse", action="store_true", help="Send native traces to the configured Langfuse project")
    runner.add_argument("--fleet-worker", action="store_true", help=argparse.SUPPRESS)
    runner.add_argument("--implementation", type=Path, help="On-call implementation used by this worker")
    runner.add_argument("--incident-id", help=argparse.SUPPRESS)
    status = commands.add_parser("status", help="Read lab readiness, saved patrol checkpoints and last incidents")
    status.add_argument("--environment-state", type=Path, help="Inspect a separately started lab environment")
    for command in ("history", "resume", "fork", "incidents"):
        commands.add_parser(command).add_argument("service", choices=SERVICES)
    args = parser.parse_args(argv)
    if args.command == "run" and args.implementation:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from implementation import load_implementation

        oncall_steps = load_implementation(args.implementation)
    if args.command == "run" and (
        not math.isfinite(args.interval) or args.interval < 0
        or (args.rounds is None and args.interval <= 0)
        or (args.rounds is not None and args.rounds < 1)
    ):
        parser.error("Use a positive interval for continuous patrol and a positive round count for tests")
    if args.command == "run" and args.fleet_worker and (
            args.rounds is not None or args.oncall or args.page_oncall or args.capture_url or args.environment_state is None):
        parser.error("Fleet workers use a continuous schedule and a separate environment")
    if args.command == "run" and args.page_oncall and not args.oncall:
        parser.error("--page-oncall requires --oncall")
    if args.command == "run" and args.incident_id is not None and (
            not args.services or len(args.services) != 1
            or not re.fullmatch(rf"lab-{args.services[0]}-[0-9a-f]{{12}}", args.incident_id)):
        parser.error("A selected incident requires one matching --service")
    state = args.state.resolve()
    if args.command == "status":
        result = read_status(state, (args.environment_state or state).resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return
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
                    fleet=args.fleet_worker, page_oncall=args.page_oncall, oncall_steps=oncall_steps,
                    incident_id=args.incident_id,
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
