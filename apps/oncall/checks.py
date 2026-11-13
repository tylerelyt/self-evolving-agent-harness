"""Run the learner's code against the actual capstone interfaces and evidence."""

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import mkdtemp
import time

CAPSTONE = Path(__file__).resolve().parent
LAB = Path(__file__).resolve().parent
PATROL = CAPSTONE / "patrol"
sys.path.insert(0, str(CAPSTONE))
sys.path.insert(0, str(PATROL))

from environment import classify, get_snapshot, inject_fault, load_json, read_alert, write_json
from fixture import CONFIRMATION_STEP
from fleet_eval import evaluate
from hermes_runtime import (ONCALL_SKILL, PATROL_SKILL, checkpoint_rows, ensure_loopback_no_proxy,
                            install_oncall_skill, open_sessions)
from investigation import investigate
from implementation import load_implementation
from patrol import (STEER_PREFIX, check_paged_recovery, connect, incidents, inspect_service,
                    model_connection, page_pending_incident)
from readiness import STAGES, read_patrol_config
from workbench_config import load_model_environment


SERVICE = "payments"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(read, accept, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = read()
            if accept(value):
                return value
        except (OSError, KeyError, RuntimeError, ValueError):
            pass
        time.sleep(.2)
    raise RuntimeError(f"实验环境没有在 {timeout} 秒内给出所需观测，查看 environment.log")


@contextmanager
def environment(state):
    state.mkdir()
    with (state / "environment.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([sys.executable, "-B", str(PATROL / "environment.py"), "--state", str(state), "serve"],
                                  stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            wait_for(lambda: get_snapshot(state, SERVICE), lambda value: classify(value) == "healthy")
            yield
        finally:
            if server.poll() is None:
                server.send_signal(signal.SIGINT)
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def patrol_parts(root, part, implementation, options, *, after_handoff=None):
    state = root / "patrol"
    state.mkdir()
    lab = root / "environment"
    with environment(lab), closing(connect(state)) as db, open_sessions(state) as sessions:
        skill = install_oncall_skill(state)
        patrol_skill = install_oncall_skill(state, name=PATROL_SKILL)
        healthy = inspect_service(sessions, db, SERVICE, lab, options, None, patrol_skill_sha=patrol_skill)
        require(healthy["status"] == "healthy", "没有提交健康巡检检查点")
        write_json(root / "healthy.json", healthy)
        print("PASS 0 · HTTP 服务、Hermes 与健康巡检已就绪", flush=True)
        if part == 0:
            return

        inject_fault(lab, SERVICE, "on")
        try:
            alert = wait_for(lambda: read_alert(lab, SERVICE), bool)
            write_json(root / "alert.json", alert)
            record = inspect_service(sessions, db, SERVICE, lab, options, skill, patrol_skill_sha=patrol_skill,
                                     route_alert=implementation.route_alert, auto_recover=False)
            deliveries = [line[len(STEER_PREFIX):] for line in record.get("steer_delivery", "").splitlines()
                          if line.startswith(STEER_PREFIX)]
            require(record.get("incident_id") == alert["incident_id"] and len(deliveries) == 1,
                    "第 1 关：未在当前巡检回合收到告警。检查 route_alert 的返回值及 ONCALL_ALERT 前缀。")
            require(json.loads(deliveries[0]) == alert and record["session_id"] == healthy["session_id"]
                    and record["steer_item_id"] < record["skill_item_id"] < record["probe_item_id"] < record["report_item_id"],
                    "第 1 关：告警原文、会话或取证顺序与监控记录不符")
            write_json(root / "triage.json", record)
            print("PASS 1 · 同一会话收到 steer，并读取 On-call Skill 后完成初判", flush=True)
            if part == 1:
                return

            saved = next(row for row in incidents(db, SERVICE) if row["incident_id"] == alert["incident_id"])
            result = investigate(state, lab, saved, options, sessions=sessions,
                                 branch_report=implementation.branch_report)
            child = result["session_id"]
            original = checkpoint_rows(sessions, record)
            copied = checkpoint_rows(sessions, {"session_id": child, "report_item_id": result["copied_report_item_id"]})
            copied_fields = ("role", "content", "tool_calls", "tool_call_id")
            require(child != record["session_id"] and sessions.get_session(child)["parent_session_id"] == record["session_id"]
                    and result["source_report_item_id"] == record["report_item_id"]
                    and result["copied_report_item_id"] < result["skill_item_id"] < result["report_item_id"]
                    and len(original) == len(copied)
                    and all(all(a[key] == b[key] for key in copied_fields) for a, b in zip(original, copied)),
                    "第 2 关：补查会话没有从已保存的报告 Item 分支")
            inbox = state / "pages" / alert["incident_id"]
            inbox.mkdir(parents=True)
            write_json(inbox / "investigation.json", result)
            print(f"PASS 2 · 从报告 Item 分支，三个新探测 {[p['http_status'] for p in result['probes']]}", flush=True)
            if part == 2:
                return

            path = inbox / "page.json"
            notification_started_at = time.time()
            page_pending_incident(state, db, sessions, SERVICE, lab, options, False, notify=implementation.notify_operator)
            require(path.is_file(), "第 3 关：值班收件箱缺少 page.json")
            page = load_json(path)
            expected = {"incident_id": alert["incident_id"], "session_id": child,
                        "report_item_id": result["report_item_id"], "report": result["report"],
                        "channel": "local_oncall_inbox", "recipient": "lab-oncall-operator",
                        "scope": f"{result['environment_id']}/{SERVICE}", "evidence": "investigation.json"}
            branch_report = next(row for row in sessions.get_messages(child) if row["id"] == result["report_item_id"])
            completed_at = max(branch_report["timestamp"], result["probes"][-1]["checked_at"], notification_started_at)
            require(all(page.get(key) == value for key, value in expected.items())
                    and type(page.get("paged_at")) in (float, int) and completed_at <= page["paged_at"] <= time.time(),
                    "第 3 关：通知没有引用实际分支报告、事故范围或交接时间")
            page_bytes = path.read_bytes()
            page_pending_incident(state, db, sessions, SERVICE, lab, options, False, notify=implementation.notify_operator)
            require(path.read_bytes() == page_bytes, "第 3 关：同一事故被重复通知")
            later = inspect_service(sessions, db, SERVICE, lab, options, skill, patrol_skill_sha=patrol_skill,
                                    route_alert=implementation.route_alert, auto_recover=False)
            require(later["session_id"] == healthy["session_id"] and later["id"] > record["id"]
                    and page["paged_at"] < later["checked_at"]
                    and "incident_id" not in later, "第 3 关：交接之后没有回到原巡检会话")
            write_json(root / "continued.json", later)
            print(f"PASS 3 · 通知落盘，原巡检会话继续更新检查点。{path}", flush=True)
            if after_handoff is not None:
                after_handoff(root, implementation, options, state, lab, db, sessions, record, result, skill, patrol_skill)
        finally:
            inject_fault(lab, SERVICE, "off")


def release_part(root, implementation, options):
    baseline = PATROL / "skills" / ONCALL_SKILL / "SKILL.md"
    candidate = root / "candidate.md"
    candidate.write_text(baseline.read_text(encoding="utf-8") + "\n" + CONFIRMATION_STEP + "。\n", encoding="utf-8")
    policy = implementation.release_policy
    decision = evaluate(root / "evaluation", baseline, candidate, options, release_policy=policy)
    rows, old, new = decision["cases"], decision["baseline_sha256"], decision["candidate_sha256"]
    require(decision["passed"] is True, "第 4 关：有效且有增益的候选没有放行")
    require(policy(rows, old, old) is False, "第 4 关：相同内容指纹被放行")
    require(policy([], old, new) is False, "第 4 关：空评测被放行")
    for arm, field in (("baseline", "valid_trace"), ("candidate", "valid_trace"), ("candidate", "passed")):
        unsafe = deepcopy(rows)
        unsafe[0][arm][field] = False
        require(policy(unsafe, old, new) is False, f"第 4 关：放行了 {arm}.{field} 未通过的评测")
    tied = deepcopy(rows)
    for row in tied:
        row["baseline"]["passed"] = True
    require(policy(tied, old, new) is False, "第 4 关：没有任何严格增益也放行了候选")
    print(f"PASS 4 · 候选经三题成对回放后合格，反例均被拒绝。{root / 'evaluation/evaluation.json'}", flush=True)


def scheduled_patrol(root, implementation, live):
    config = read_patrol_config(implementation)
    write_json(root / "patrol-config.json", config)
    state = root / "patrol"
    lab = root / "environment"
    with environment(lab), (root / "patrol.log").open("w", encoding="utf-8") as output:
        command = [sys.executable, "-B", str(LAB / "run.py"), "--state", str(state),
                   "--environment-state", str(lab), "--implementation", str(implementation.__file__),
                   "--stage", "1", "--rounds", "2"]
        if live:
            command.append("--live")
        allowance = 180 if live else 30
        worker = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                timeout=max(allowance, config["interval"] + allowance))
        require(worker.returncode == 0, "Stage 1 巡检进程失败，查看 patrol.log")
    with closing(connect(state, readonly=True)) as db:
        records = [dict(row) for row in db.execute("SELECT * FROM inspections ORDER BY id")]
    require(len(records) == 2 * config["sample"] and len({row["turn_id"] for row in records}) == len(records),
            "Stage 1 没有按配置完成两轮实际巡检")
    require(all(row["service"] in config["services"] and row["status"] in ("healthy", "insufficient") for row in records),
            "Stage 1 的巡检范围或健康记录与配置不符")
    with open_sessions(state) as sessions:
        for record in records:
            rows = checkpoint_rows(sessions, record)
            tool = next((row for row in rows if row["id"] == record["evidence_item_id"]), None)
            returned = json.loads(tool["content"]) if tool is not None else {}
            returned.pop("history_available", None)
            require(tool is not None and tool["role"] == "tool" and returned == json.loads(record["observation"]),
                    "Stage 1 缺少可回查的 Hermes 指标回执")
    write_json(root / "patrol-checkpoints.json", records)
    print("PASS stage 1 · 自己配置的 Hermes 已实际完成两轮巡检", flush=True)


def memory_after_handoff(root, implementation, options, state, lab, db, sessions,
                         record, branch, skill, patrol_skill, *, live):
    from fact_handoff import connection as handoff_connection, read_evidence
    from shared_facts import publish_fact

    inject_fault(lab, SERVICE, "off")
    lower_bound = time.time()
    windows = []
    environment_id = json.loads(record["observation"])["environment_id"]
    print("Stage 3 · 正在等待处置后的两个独立健康窗口", flush=True)
    for _ in range(2):
        wait_for(lambda: get_snapshot(lab, SERVICE),
                 lambda value: value["environment_id"] == environment_id
                 and value["window_start"] > lower_bound and value["requests"] >= 10
                 and value["error_rate"] == 0 and classify(value) == "healthy", timeout=45)
        healthy = inspect_service(sessions, db, SERVICE, lab, options, skill, patrol_skill_sha=patrol_skill,
                                  route_alert=implementation.route_alert, auto_recover=False)
        snapshot = json.loads(healthy["observation"])
        require(snapshot["window_start"] > lower_bound and snapshot["requests"] >= 10
                and snapshot["error_rate"] == 0 and healthy["status"] == "healthy",
                "Stage 3 的恢复窗口没有实际健康巡检证据")
        check_paged_recovery(state, db, SERVICE, healthy)
        windows.append(snapshot)
        lower_bound = snapshot["window_end"]
    closed = next(item for item in incidents(db, SERVICE) if item["incident_id"] == record["incident_id"])
    require(closed["recovered_inspection_id"] == healthy["id"], "Stage 3 原事故未关联恢复检查点")
    fact = {
        "incident_id": record["incident_id"], "instance": "instance-a", "environment_id": environment_id,
        "service": SERVICE, "source_session_id": record["session_id"],
        "source_report_item_id": record["report_item_id"], "investigation_session_id": branch["session_id"],
        "investigation_report_item_id": branch["report_item_id"],
        "probe_statuses": [item["http_status"] for item in branch["probes"]],
        "recovery_end": windows[-1]["window_end"], "source_skill_sha256": record["skill_sha256"],
    }
    memory = root / "shared-facts.sqlite3"
    publish_fact(memory, fact)
    write_json(root / "memory-source.json", {"fact": fact, "recovery_windows": windows})
    recall_state = root / "memory-recall"
    recall_state.mkdir()
    scope = {key: fact[key] for key in ("incident_id", "service", "environment_id")}
    with handoff_connection(not live) as url:
        recall_options = {**options, "base_url": url}
        result = implementation.recall_memory(recall_state, memory, lab, scope, recall_options)
    require(isinstance(result, dict) and isinstance(result.get("session_id"), str),
            "Stage 3 recall_memory 没有返回真实 Session 证据")
    with open_sessions(recall_state) as recalled_sessions:
        rows = recalled_sessions.get_messages(result["session_id"])
    prompt = next((row["content"] for row in rows if row["role"] == "user"), None)
    require(prompt is not None, "Stage 3 找不到新 Session 的原始请求")
    verified = read_evidence(rows, prompt, scope, result["session_id"])
    require(result == verified and verified["status"] == "recalled" and verified["fact"] == fact
            and result["session_id"] not in (record["session_id"], branch["session_id"]),
            "Stage 3 没有从独立会话召回原事故并查询当前 HTTP 状态")
    write_json(root / "memory-recall.json", verified)
    print("PASS stage 3 · 新 Session 实际检索了已恢复事故，并独立查询当前现场", flush=True)


def run_stages(root, stage, implementation, options, *, live, checks):
    startup = root / "stage-1"
    startup.mkdir()
    scheduled_patrol(startup, implementation, live)
    checks.append({"stage": 1, "status": "passed", "evidence": ["stage-1/patrol-checkpoints.json", "stage-1/patrol/hermes/state.db"]})
    if stage == 1:
        return
    incident = root / "stage-2"
    incident.mkdir()

    def after_handoff(*args):
        checks.append({"stage": 2, "status": "passed", "evidence": ["stage-2/triage.json", "stage-2/continued.json", "stage-2/patrol/hermes/state.db"]})
        print("PASS stage 2 · 告警、分支调查和 paging 已接入同一份实现", flush=True)
        if stage >= 3:
            memory_after_handoff(*args, live=live)
            checks.append({"stage": 3, "status": "passed", "evidence": ["stage-2/memory-source.json", "stage-2/memory-recall.json", "stage-2/memory-recall/hermes/state.db"]})

    patrol_parts(incident, 3, implementation, options, after_handoff=after_handoff)
    if stage == 4:
        learning = root / "stage-4"
        learning.mkdir()
        release_part(learning, implementation, options)
        checks.append({"stage": 4, "status": "passed", "evidence": ["stage-4/evaluation/evaluation.json"]})
        print("PASS stage 4 · 独立成对回放与自己的放行规则均通过，发布和采用仍需工作台现场验证", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--part", type=int, choices=range(5), help="兼容原有第 0—4 关验收")
    mode.add_argument("--stage", type=int, choices=range(1, 5), help="工作台阶段，从巡检逐步接入记忆和学习")
    parser.add_argument("--solution", action="store_true", help="运行结课工程提供的参考实现")
    parser.add_argument("--implementation", type=Path, default=CAPSTONE / "workflow.py")
    parser.add_argument("--live", action="store_true", help="使用已配置的真实模型端点")
    parser.add_argument("--env-file", type=Path, help="读取本地模型配置，默认使用 repo 的 .env")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--out", type=Path, help="本次记录使用的独立新目录")
    destination.add_argument("--workbench", type=Path, help="把验收证据保存到运行中工作台的 checks 目录")
    args = parser.parse_args()
    if args.workbench is not None and not args.workbench.is_dir():
        parser.error("--workbench 应指向已经创建的工作台目录")
    if args.out:
        root = args.out.resolve()
        try:
            root.mkdir(parents=True)
        except OSError as exc:
            parser.error(f"请指定一个可创建的新输出目录。{exc}")
    else:
        parent = args.workbench.resolve() / "checks" if args.workbench else CAPSTONE / "runtime"
        parent.mkdir(parents=True, exist_ok=True)
        root = Path(mkdtemp(prefix="student-lab-", dir=parent))
    print(f"本轮证据 {root}", flush=True)
    source = (CAPSTONE / "workflow.py" if args.solution else args.implementation).resolve()
    report = {"schema": "oncall-workshop-check-v1", "status": "running", "stage": args.stage,
              "stage_title": STAGES[args.stage]["title"] if args.stage is not None else None,
              "part": args.part, "mode": "live" if args.live else "local-script-model",
              "implementation": {"path": str(source)}, "checks": [], "evidence": [], "started_at": time.time()}
    try:
        if args.live:
            load_model_environment(args.env_file)
        report["implementation"]["sha256"] = sha256(source.read_bytes()).hexdigest()
        implementation = load_implementation(source) if args.part != 0 else None
        ensure_loopback_no_proxy()
        with model_connection(not args.live) as url:
            options = {"base_url": url, "api_key": os.environ["GLM_API_KEY"] if args.live else "local-fixture",
                       "model": (os.environ.get("GLM_MODEL") or "glm-5.2") if args.live else "patrol-fixture"}
            if args.stage is not None:
                run_stages(root, args.stage, implementation, options, live=args.live, checks=report["checks"])
            else:
                patrol_parts(root, args.part, implementation, options)
                if args.part == 4:
                    release_part(root, implementation, options)
        require(sha256(source.read_bytes()).hexdigest() == report["implementation"]["sha256"],
                "验收期间实现文件发生变化，请重新运行检查")
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc) or type(exc).__name__)
        print(f"FAIL · {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        report.update(status="interrupted", error="检查已中断，当前阶段未通过")
        print(report["error"], file=sys.stderr)
    finally:
        report["completed_at"] = time.time()
        report["evidence"] = [path for check in report["checks"] for path in check["evidence"]]
        write_json(root / "check.json", report)
        print(root / "check.json", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
