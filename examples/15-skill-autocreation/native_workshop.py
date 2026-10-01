"""独立场景实验：实际调查、Hermes 原生复盘、库变化与新 Session 使用。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from common import HERE, completed, record, tool_calls, sanitize_logging, receipt_object
from native_trace import candidate_check
from package_integrity import (
    snapshot, content_files, package_hash, install_candidate, native_review_complete,
)
import scenario

REVISION = "aaf9688519cca58dd5f76a589a0911aff269b060"
IS_PATCH = HERE.name.startswith("16-")
CONFIG = """memory:
  memory_enabled: false
  user_profile_enabled: false
  nudge_interval: 0
  flush_min_turns: 0
skills:
  creation_nudge_interval: 0
  write_approval: false
  external_dirs: []
compression:
  enabled: false
tools:
  tool_search:
    enabled: "off"
"""


def check_space():
    free = shutil.disk_usage(HERE).free
    if free < 1024**3:
        raise RuntimeError("可用空间不足1GiB，停止生成运行目录")
    return free


def source_info():
    source = Path(os.environ.get("HERMES_SRC", HERE.parents[1]/".deps/hermes-agent")).resolve()
    head = subprocess.check_output(
        ["git", "--no-optional-locks", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if head != REVISION:
        raise RuntimeError("Hermes 不是课程固定提交")
    files = ("native_workshop.py", "native_trace.py", "scenario.py",
             "package_integrity.py", "common.py", "review_observer.py")
    return {"hermes_revision": head,
            "source_hashes": {p: hashlib.sha256((HERE/p).read_bytes()).hexdigest() for p in files}}


def configure_home(home):
    home.mkdir(parents=True, exist_ok=True)
    (home/"config.yaml").write_text(CONFIG)
    (home/".no-bundled-skills").touch()
    os.environ["HERMES_HOME"] = str(home)
    sanitize_logging()


def model_options():
    # 仅从当前进程读取凭据；默认使用 GLM 官方接口。
    key = os.environ.get("GLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 GLM_API_KEY；请在运行进程中配置，不要写入源码或 notebook")
    return dict(
        model=os.environ.get("GLM_MODEL", "").strip() or "glm-5.2",
        provider="glm", api_key=key,
        base_url=os.environ.get("GLM_BASE_URL", "").strip()
        or "https://open.bigmodel.cn/api/paas/v4",
    )


def register_query(audit):
    from tools.registry import registry
    def invoke(args, **kwargs):
        receipt = scenario.query(**args)
        audit.append({"args": args, "receipt": receipt})
        return json.dumps(receipt, ensure_ascii=False)
    registry.register(name="investigate", toolset="lesson_investigation", schema=scenario.SCHEMA,
                      handler=invoke, check_fn=lambda: True, description="只读构造现场调查")


def seed_library():
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import set_current_write_origin, reset_current_write_origin
    origin = set_current_write_origin("background_review" if IS_PATCH else "foreground")
    receipts = []
    try:
        actions = [dict(action="create", name=scenario.SEED_NAME, content=scenario.SEED)]
        if IS_PATCH:
            actions.append(dict(action="write_file", name=scenario.SEED_NAME,
                                file_path="references/alert-tools.md", file_content=scenario.REFERENCE))
        for args in actions:
            raw = skill_manage(**args)
            receipt = json.loads(raw) if isinstance(raw, str) else raw
            if not receipt.get("success"):
                raise RuntimeError("构造起点保存失败，不继续模型运行")
            receipts.append({"args": args, "receipt": receipt, "source": "course-fixture"})
    finally:
        reset_current_write_origin(origin)
    return {"model_generated": False, "model_requests": 0,
            "maintenance_eligibility": "explicit-course-fixture" if IS_PATCH else "user-owned",
            "calls": receipts}


def parse_answer(result):
    """只接受完整 JSON 对象或唯一明确的 json 代码块，不从散文猜答案。"""
    import re
    text = result.get("final_response", "")
    if not isinstance(text, str):
        return {}, "invalid"
    text = text.strip()
    try:
        value = json.loads(text)
    except ValueError:
        starts = re.findall(r"^```json[ \t]*$", text, flags=re.MULTILINE | re.IGNORECASE)
        blocks = re.findall(
            r"^```json[ \t]*\n(.*?)^```[ \t]*$",
            text, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        if len(starts) > 1:
            return {}, "ambiguous"
        if len(starts) != 1 or len(blocks) != 1:
            return {}, "invalid"
        try:
            value = json.loads(blocks[0])
        except ValueError:
            return {}, "invalid"
        return (value, "json_block") if isinstance(value, dict) else ({}, "invalid")
    return (value, "pure_json") if isinstance(value, dict) else ({}, "invalid")


def loaded_before_query(calls, content, names):
    for call in calls:
        if call["name"] == "investigate":
            return False
        if (call["name"] == "skill_view" and call["args"].get("name") in names
                and not call["args"].get("file_path")
                and receipt_object(call).get("success")
                and receipt_object(call).get("content") == content):
            return True
    return False


def check_foreground(task, case, output_path):
    """先保存原始返回和持久消息，再做派生检查；检查异常不覆盖上游错误。"""
    task.update(business_checks=False, answer_checks=False, persisted_calls_match=False,
                answer_format="unchecked", pure_json=False)
    record(output_path, task)
    try:
        task["calls"] = tool_calls(task["result"].get("messages", []))
        task["persisted_calls"] = tool_calls(task["persisted"])
        task["persisted_calls_match"] = task["calls"] == task["persisted_calls"]
        record(output_path, task)
        if not completed(task["result"]):
            return task
        answer, answer_format = parse_answer(task["result"])
        task.update(answer_format=answer_format, pure_json=answer_format == "pure_json",
                    business_checks=scenario.verify(task["calls"], case),
                    answer_checks=scenario.verify_answer(answer, case))
    except Exception as error:
        task["validation_error"] = type(error).__name__ + ": " + str(error)
    finally:
        record(output_path, task)
    return task


def foreground(db, message, case, audit, output_path):
    from run_agent import AIAgent
    from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
    sid = "lesson-" + uuid.uuid4().hex
    db.create_session(sid, source="course-native-workshop")
    agent = AIAgent(**model_options(), session_id=sid, session_db=db,
                    enabled_toolsets=["skills", "lesson_investigation"],
                    max_iterations=16, quiet_mode=True, skip_context_files=True)
    set_thread_tool_whitelist({"investigate", "skills_list", "skill_view"},
                              deny_msg_fmt="前台只读，禁止 {tool_name}。")
    try:
        result = agent.run_conversation(
            user_message=message + "\n" + scenario.ANSWER_FORMAT,
            system_message=(
                "前台先查找并阅读适用方法，再用当前只读工具调查。区分已确认与未知。"
                "前台不能写 Skill 或改业务；这一限制只约束前台调查。"
                "独立后台复盘按原生 Skill 维护权限处理，不修改业务。"),
            conversation_history=[])
        task = {"session_id": sid, "history_at_start": 0, "result": result,
                "model_requests": agent._api_call_count, "calls": [], "persisted": []}
        record(output_path, task)
        try:
            task["persisted"] = db.get_messages_as_conversation(sid, repair_alternation=True)
        except Exception as error:
            task["persistence_error"] = type(error).__name__ + ": " + str(error)
            record(output_path, task)
            raise RuntimeError(str(result.get("error") or task["persistence_error"])) from error
        task = check_foreground(task, case, output_path)
        prompt = agent._cached_system_prompt
        return task, prompt
    finally:
        clear_thread_tool_whitelist()
        agent.close()


def review_job(job_path):
    job_path = Path(job_path).resolve()
    if not job_path.is_relative_to((HERE/"runtime").resolve()):
        raise ValueError("学习任务必须属于本讲运行目录")
    job = json.loads(job_path.read_text())
    home = job_path.parent/"candidate"
    configure_home(home)
    before_package = snapshot(home/"skills")
    if package_hash(before_package) != job["baseline_sha256"]:
        raise RuntimeError("复盘开始前候选基线已变化")
    from run_agent import AIAgent
    from agent.background_review import spawn_background_review_thread
    from review_observer import ReviewObserver
    register_query([])
    parent = AIAgent(**model_options(), enabled_toolsets=["skills", "lesson_investigation"],
                     quiet_mode=True, skip_context_files=True, max_iterations=16)
    parent._cached_system_prompt = job["cached_prompt"]
    observer = ReviewObserver(job)
    observer.install()
    try:
        # 使用固定版本的原生提示，通过公开的 focus 参数补充课程要求。
        target, prompt = spawn_background_review_thread(
            parent, job["snapshot"], review_memory=False,
            review_skills=True, focus=scenario.FOCUS)
        thread = threading.Thread(target=target, name="bg-review", daemon=True)
        thread.start()
        thread.join()
        record(job_path.parent/"review.json", {**observer.result, "prompt": prompt,
               "before": content_files(before_package), "before_package": before_package})
        after_package = snapshot(home/"skills")
        review = {**observer.result, "prompt": prompt,
                  "before": content_files(before_package), "after": content_files(after_package),
                  "before_package": before_package, "after_package": after_package,
                  "baseline_sha256": job["baseline_sha256"]}
        record(job_path.parent/"review.json", review)
        try:
            review.update(candidate_check(review))
        except Exception as error:
            review["validation_error"] = type(error).__name__ + ": " + str(error)
            record(job_path.parent/"review.json", review)
            raise
        record(job_path.parent/"review.json", review)
    finally:
        observer.restore()
        parent.close()


def terminate_owned(process):
    """调用者只传入自己以独立进程组启动的 Popen，不按目录或PID文件寻址。"""
    if process.poll() is not None:
        return
    import signal
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def run_review(run, job):
    path = run/"job.json"
    record(path, job)
    with (run/"review.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-B", str(HERE/"native_workshop.py"), "--live", "--review-job", str(path)],
            stdout=log, stderr=log, env=os.environ.copy(), start_new_session=True)
        try:
            code = process.wait(timeout=600)
            if code:
                raise RuntimeError("原生复盘进程失败，查看本次 review.log")
        finally:
            terminate_owned(process)
            record(run/"review-process.json", {
                "owned": True, "pid": process.pid, "exited": process.poll() is not None,
                "returncode": process.returncode,
            })
    return json.loads((run/"review.json").read_text())


def review_outcome(review):
    if not native_review_complete(review, allow_no_calls=True):
        raise RuntimeError("原生复盘未正常结束或调用无法追溯：" + str(
            review.get("result", {}).get("error") or review.get("result", {}).get("failure_reason")
            or review.get("validation_error") or "见 review.json 原始返回"))
    calls = review["calls"]
    writes = [c for c in calls if c["name"] == "skill_manage"]
    if review["before"] == review["after"]:
        if review["before_package"] != review["after_package"]:
            raise RuntimeError("正文未变但来源记录发生变化，不能记作无更新")
        if any(not receipt_object(c).get("success") for c in writes):
            raise RuntimeError("写入失败，不能冒称没有值得保存的经验")
        return "no-update"
    first_write = next(i for i, c in enumerate(calls) if c["name"] == "skill_manage")
    before_write = calls[:first_write]
    if not any(c["name"] == "skills_list" and receipt_object(c).get("success") for c in before_write):
        raise RuntimeError("本次复盘未先查技能库，不采用")
    if not any(c["name"] == "skill_view" and c["args"].get("name") == scenario.SEED_NAME
               and receipt_object(c).get("success") for c in before_write):
        raise RuntimeError("本次复盘未先读已有相关方法，不采用")
    checked = candidate_check(review)
    if not checked["candidate_complete"]:
        raise RuntimeError("方法变化不完整或无法逐次追溯，不采用")
    succeeded = [c for c in writes if receipt_object(c).get("success")]
    if IS_PATCH and (checked["candidate_name"] != scenario.SEED_NAME
                     or not any(c["args"].get("action") == "patch" for c in succeeded)
                     or any(c["args"].get("action") == "create" for c in succeeded)):
        raise RuntimeError("本讲要求修订已有同名方法；本次未形成对应增量修订")
    if not IS_PATCH and not any(c["args"].get("action") == "create" for c in succeeded):
        raise RuntimeError("本次未创建新方法，保留真实轨迹，不冒称创建成功")
    return "candidate"


def run_live():
    model_options()
    check_space()
    version = source_info()
    version["validation_run_id"] = os.environ.get("LESSON15_VALIDATION_ID")
    run = HERE/"runtime"/("native-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    home = run/"hermes"
    configure_home(home)
    record(run/"version.json", version)
    audit = []
    register_query(audit)
    record(run/"seed.json", seed_library())
    from hermes_state import SessionDB
    db = SessionDB(home/"state.db")
    try:
        task, prompt = foreground(db, scenario.TASK, scenario.INITIAL_CASE, audit,
                                run/"investigation.json")
        record(run/"investigation.json", task)
        if not all((completed(task["result"]), task["business_checks"],
                    task["answer_checks"], task["persisted_calls_match"])):
            raise RuntimeError("调查未通过：" + str(task["result"].get("error")
                or task.get("validation_error") or "业务、答案或持久记录检查未通过"))
        before = snapshot(home/"skills")
        check_space()
        shutil.copytree(home/"skills", run/"candidate/skills", symlinks=True)
        job = {"id": run.name, "snapshot": task["result"]["messages"], "cached_prompt": prompt,
               "baseline_sha256": package_hash(before)}
        review = run_review(run, job)
        outcome = review_outcome(review)
        if outcome == "no-update":
            result = {"status": "no-update", "passed": False, "path": "native-live",
                      "observation_complete": True, "skill_created": False,
                      "reason": "本次复盘未改变技能正文，不执行创建或复用展示；判断依据见原生回复",
                      "model_requests": task["model_requests"] + review["model_requests"]}
        else:
            installed = install_candidate(run/"candidate/skills", home/"skills",
                                          review["candidate_sha256"], job["baseline_sha256"])
            record(run/"adoption.json", {**installed, "mode": "course-local-adoption",
                                       "human_approval": False})
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
            expected = review["after"][review["candidate_directory"]+"/SKILL.md"]
            future = []
            for case, message in scenario.FUTURE_CASES:
                item, _ = foreground(db, message, case, audit, run/("future-"+case+".json"))
                item["loaded_adopted_content"] = loaded_before_query(
                    item["calls"], expected,
                    (review["candidate_name"], review["candidate_directory"]))
                future.append(item)
                record(run/("future-"+case+".json"), item)
                if not completed(item["result"]):
                    break
            passed = all(completed(t["result"]) and t["business_checks"] and t["answer_checks"]
                         and t["persisted_calls_match"] and t["loaded_adopted_content"] for t in future)
            result = {"status": "passed" if passed else "failed", "passed": passed,
                      "path": "native-live", "new_sessions": len(future),
                      "model_requests": task["model_requests"] + review["model_requests"]
                                        + sum(t["model_requests"] for t in future)}
        record(run/"audit.json", audit)
        record(run/"result.json", {**result, **version})
        record(HERE/"validation/native-live-latest.json",
               {**result, **version, "run": run.name, "report": str(run/"REPORT.md")})
        record(run/"REPORT.md", "# 本次原生练习\n\n状态："+result["status"]+
               "\n\n[调查](investigation.json) · [原生复盘](review.json) · [库变化与运行结果](result.json) · [业务回执](audit.json)\n")
        print(json.dumps({**result, "report": str(run/"REPORT.md")}, ensure_ascii=False))
        return result
    except BaseException as error:
        record(run/"failure.json", {"status": "failed", "error": str(error), "path": "native-live"})
        record(HERE/"validation/native-live-latest.json",
               {"status": "failed", "passed": False, "error": str(error), "run": run.name, **version})
        raise
    finally:
        db.close()
        record(run/"cleanup.json", {"session_db_closed": True, "persistent_server": False})


def inspect_only():
    """仅查看构造现场；不创建 Hermes Agent，不读取模型凭据，不写运行结果。"""
    if IS_PATCH:
        result = {case: scenario.dataset(case) for case in ("east", "west", "single")}
    else:
        result = {case: {kind: scenario.query("test", case, kind)
                        for kind in ("normal", "exception")} for case in ("alpha", "beta")}
    print(json.dumps({"path": "constructed-inspection", "model_requests": 0,
                      "title": scenario.TITLE, "observations": result}, ensure_ascii=False, indent=2))


def main():
    # SIGTERM 也经过 finally，回收本次创建的复盘子进程并关闭数据库。
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="运行真实模型调查与复盘，会产生 API 费用")
    parser.add_argument("--review-job", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.review_job:
        if not args.live:
            parser.error("后台模型运行必须显式指定 --live")
        review_job(args.review_job)
    elif args.live:
        result = run_live()
        return 0 if result["status"] in ("passed", "no-update") else 1
    else:
        inspect_only()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
