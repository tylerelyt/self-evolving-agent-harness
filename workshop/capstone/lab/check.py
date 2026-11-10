"""Run the learner's code against the actual capstone interfaces and evidence."""

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
import importlib
import json
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import mkdtemp
import time

CAPSTONE = Path(__file__).resolve().parents[1]
PATROL = CAPSTONE / "patrol"
sys.path.insert(0, str(PATROL))

from environment import classify, get_snapshot, inject_fault, load_json, read_alert, write_json
from fixture import CONFIRMATION_STEP
from fleet_eval import evaluate
from hermes_runtime import (ONCALL_SKILL, PATROL_SKILL, checkpoint_rows, ensure_loopback_no_proxy,
                            install_oncall_skill, open_sessions)
from investigation import investigate
from patrol import STEER_PREFIX, connect, incidents, inspect_service, model_connection, page_pending_incident


SERVICE = "payments"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(read, accept):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            value = read()
            if accept(value):
                return value
        except (OSError, KeyError, RuntimeError, ValueError):
            pass
        time.sleep(.2)
    raise RuntimeError("实验环境没有在 20 秒内给出所需观测，查看 environment.log")


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


def patrol_parts(root, part, implementation, options):
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
            page_pending_incident(state, db, sessions, SERVICE, lab, options, False, notify=implementation.notify_operator)
            require(path.is_file(), "第 3 关：值班收件箱缺少 page.json")
            page = load_json(path)
            expected = {"incident_id": alert["incident_id"], "session_id": child,
                        "report_item_id": result["report_item_id"], "report": result["report"],
                        "channel": "local_oncall_inbox", "recipient": "lab-oncall-operator",
                        "scope": f"{result['environment_id']}/{SERVICE}", "evidence": "investigation.json"}
            require(all(page.get(key) == value for key, value in expected.items())
                    and type(page.get("paged_at")) in (float, int) and 0 < page["paged_at"] <= time.time(),
                    "第 3 关：通知没有引用实际分支报告、事故范围或交接时间")
            page_bytes = path.read_bytes()
            page_pending_incident(state, db, sessions, SERVICE, lab, options, False, notify=implementation.notify_operator)
            require(path.read_bytes() == page_bytes, "第 3 关：同一事故被重复通知")
            later = inspect_service(sessions, db, SERVICE, lab, options, skill, patrol_skill_sha=patrol_skill,
                                    route_alert=implementation.route_alert, auto_recover=False)
            require(later["session_id"] == healthy["session_id"] and later["id"] > record["id"]
                    and "incident_id" not in later, "第 3 关：交接之后没有回到原巡检会话")
            write_json(root / "continued.json", later)
            print(f"PASS 3 · 通知落盘，原巡检会话继续更新检查点。{path}", flush=True)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", type=int, choices=range(5), required=True, help="按顺序检查到指定关卡，0 只启动现场")
    parser.add_argument("--solution", action="store_true", help="运行结课工程提供的参考实现")
    parser.add_argument("--out", type=Path, help="本次记录使用的独立新目录")
    args = parser.parse_args()
    if args.out:
        root = args.out.resolve()
        try:
            root.mkdir(parents=True)
        except OSError as exc:
            parser.error(f"请指定一个可创建的新输出目录。{exc}")
    else:
        parent = CAPSTONE / "runtime"
        parent.mkdir(exist_ok=True)
        root = Path(mkdtemp(prefix="student-lab-", dir=parent))
    print(f"本轮证据 {root}", flush=True)
    try:
        module_name = "solutions.self_improvement_agent" if args.solution else "self_improvement_agent"
        implementation = importlib.import_module(module_name) if args.part else None
        ensure_loopback_no_proxy()
        with model_connection(True) as url:
            options = {"base_url": url, "api_key": "local-fixture", "model": "patrol-fixture"}
            patrol_parts(root, args.part, implementation, options)
            if args.part == 4:
                release_part(root, implementation, options)
    except (OSError, ImportError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL · {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
