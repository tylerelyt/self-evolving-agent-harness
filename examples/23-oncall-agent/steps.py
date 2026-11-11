"""Six independently executable lesson-23 verification steps."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import run23 as native
from scenario import CASES, SKILL, decide, score

HERE = Path(__file__).resolve().parent
SCHEMA = "lesson23-six-steps-v1"
RESULTS = {1: "01-baseline", 2: "02-candidates", 3: "03-replay", 4: "04-reject", 5: "05-publish", 6: "06-adopt"}
TITLES = {1: "正式版执行、外部上下文与归档", 2: "生成两来源隔离候选", 3: "新旧独立留出回放", 4: "评分故障拒绝与原版继续", 5: "恢复评分并发布", 6: "恢复原Session并验证新客户端采用"}


class StepError(ValueError):
    pass


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def own(run, relative):
    path = run / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(run):
        raise StepError("路径必须属于当前练习目录。")
    if any(part.is_symlink() for part in [path, *path.parents] if part != run.parent):
        raise StepError("练习产物不能通过软链接指向其他目录。")
    return path


def result_path(run, number):
    return run / "steps" / (RESULTS[number] + ".json")


def open_state(run):
    run = run.expanduser().resolve()
    try:
        state, manifest = native.read(own(run, "state.json")), native.read(own(run, "run.json"))
    except (OSError, ValueError) as exc:
        raise StepError("请先用 init 建立本讲独立练习目录；reference 目录不能当作分步目录。") from exc
    if (state.get("schema") != SCHEMA or not state.get("exercise_id")
            or state["exercise_id"] != manifest.get("exercise_id")
            or manifest.get("group_id") != "lesson23-exercise-" + state["exercise_id"]):
        raise StepError("不是第23讲分步练习目录。")
    return run, state


@contextmanager
def locked(run):
    with own(run, ".step.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StepError("当前练习另有命令正在运行；本命令没有开始执行。") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def controls(run):
    value = native.read(own(run, "controls.json"))
    if (set(value) != {"score_outage", "prefer_source"} or type(value["score_outage"]) is not bool
            or value["prefer_source"] not in {"skillclaw", "gepa"}):
        raise StepError("controls.json 仅接受布尔 score_outage 和候选来源 prefer_source。")
    return value


def fingerprint(run, paths):
    found = {}
    for relative in paths:
        root = own(run, relative)
        files = [root] if root.is_file() else list(root.rglob("*")) if root.exists() else []
        for path in files:
            if path.is_symlink():
                raise StepError("产物目录含软链接，拒绝建立验证记录。")
            relative_path = path.relative_to(run)
            if path.is_file() and path.suffix in {".json", ".md", ".db", ".yaml", ".conf"} and "cache" not in relative_path.parts:
                found[str(relative_path)] = native.sha(path)
    return found


def guard_outputs(run, paths):
    for relative in paths:
        path = own(run, relative)
        if path.is_dir() and any(child.is_symlink() for child in path.rglob("*")):
            raise StepError("输出目录含软链接，本命令未开始执行。")


def shared_file(run):
    group = native.load_run(run)["group_id"]
    if not group.startswith("lesson23-exercise-") or "/" in group or "\\" in group:
        raise StepError("共享组不是本练习建立的独立组。")
    return own(run, f"shared/{group}/skills/{SKILL}/SKILL.md")


def check_formal(run, state, *, native_manifest=False):
    if native.sha(own(run, f"formal/{SKILL}/SKILL.md")) != state["baseline_sha256"]:
        raise StepError("原始基线已改变；请不要编辑固定 Skill 或复用其他讲的状态。")
    expected = state.get("formal_sha256")
    if expected is not None:
        path = shared_file(run)
        if not path.is_file() or native.sha(path) != expected:
            raise StepError("共享正式版与已记录决定不一致。")
        if native_manifest:
            record = next((item for item in native.hub(run).list_remote() if item["name"] == SKILL), {})
            if record.get("sha256") != expected:
                raise StepError("SkillHub 清单与正式内容不一致。")


def verify(run, state, *, through=6):
    if native.sha(HERE / "fixtures/gepa-tasks.json") != state["dataset_file_sha256"]:
        raise StepError("题包版本已改变；不能给旧轨迹套用新的题包。请新建练习目录。")
    check_formal(run, state)
    for key, completed in state["completed"].items():
        if int(key) > through:
            continue
        for relative, expected in completed["files"].items():
            path = own(run, relative)
            if not path.is_file() or native.sha(path) != expected:
                raise StepError(f"第{key}步证据已改变：{relative}。先恢复原文件或 reset --from-step {key}。")


def preflight(number):
    subprocess.run(["bash", str(native.ROOT / "scripts/setup_skillclaw.sh"), "--check"], check=True)
    if number == 1:
        subprocess.run([str(native.PYTHON), "-B", str(HERE / "setup_openviking.py"), "--check"], check=True)
    if number == 2:
        try:
            installed = version("gepa")
        except PackageNotFoundError:
            installed = None
        if installed != "0.0.27":
            raise StepError("第2步需要 gepa==0.0.27；请先安装本讲 requirements.txt。")


def init(run, prefer_source="gepa"):
    run = run.expanduser().resolve()
    run.mkdir(parents=True, exist_ok=False)
    identifier = uuid4().hex
    shutil.copytree(HERE / "seed", run / "formal")
    write(run / "run.json", {"format": "skillclaw-course-v1", "exercise_id": identifier,
        "group_id": "lesson23-exercise-" + identifier, "publish_mode": "validated", "model": "lesson23-script",
        "mode": "local-script-model", "complete_scores": False, "prefer_source": prefer_source})
    write(run / "controls.json", {"score_outage": True, "prefer_source": prefer_source})
    state = {"schema": SCHEMA, "exercise_id": identifier, "created_at": time.time(), "completed": {}, "failed": None,
             "baseline_sha256": native.sha(run / f"formal/{SKILL}/SKILL.md"), "formal_sha256": None,
             "dataset_file_sha256": native.sha(HERE / "fixtures/gepa-tasks.json")}
    write(run / "state.json", state)
    return {"status": "initialized", "run": str(run), "next_step": 1, "model_requests": 0}


def step1(run, state):
    from external_context import prepare_context, scripted_service
    with native.connection(), scripted_service(run / "openviking-service") as endpoint:
        bundle = prepare_context(run / "external-context", endpoint=endpoint)
        write(run / "context-manifest.json", {"bundle": "external-context/external-context.json",
              "context_sha256": bundle["context_sha256"], "step1": "native_live", "later_steps": "frozen_replay"})
        write(run / "initial-push.json", native.hub(run, "exercise-baseline").push_skills(str(run / "formal")))
        source = native.collect(run, run / f"formal/{SKILL}/SKILL.md")
        write(run / "experience.json", source)
    state["formal_sha256"] = state["baseline_sha256"]
    check_formal(run, state, native_manifest=True)
    return {"session_id": source["session_id"], "report_item_id": source["report_item_id"],
            "skill_sha256": source["skill_sha256"], "archive": source["archive"],
            "context": "native OpenViking find/read; service closed after this step", "next_step_not_started": 2}


def step2(run, state):
    from gepa_search import load_dataset, search
    source = native.read(run / "experience.json")
    baseline = run / f"formal/{SKILL}/SKILL.md"
    with native.connection():
        candidate, job = native.native_candidate(run, source)
        records = [native.register_candidate(run, candidate, baseline, source, job=job)]
        sessions = []
        def execute(directory, skill, task):
            result = native.execute_task(directory, skill, task=task,
                        context_file=run / "external-context/external-context.json")
            sessions.append(result["session_id"])
            return result
        selected, provenance = search(run / "learning/gepa", baseline, execute)
        if (provenance["baseline_sha256"] != state["baseline_sha256"] or provenance["candidate_sha256"] != native.sha(selected)
                or len(sessions) != len(set(sessions)) or set(sessions) != set(provenance["source_sessions"])):
            raise StepError("GEPA 来源版本或原生 Session 不一致。")
        records.append(native.register_candidate(run, selected, baseline, provenance, generator="gepa"))
    groups, dataset_sha = load_dataset()
    saved = native.read(Path(provenance["dataset_file"]))
    if saved["splits"] != groups or saved["dataset_sha256"] != dataset_sha:
        raise StepError("搜索使用的冻结题包已改变。")
    generation = {"records": records, "tasks": groups["holdout"], "excluded_sessions": [source["session_id"], *sessions],
                  "gepa_provenance": provenance, "dataset_sha256": dataset_sha}
    write(run / "learning/generation.json", generation)
    write(run / "candidates.json", records)
    check_formal(run, state, native_manifest=True)
    return {"candidate_sources": [record["source"] for record in records], "candidates": [record["candidate_id"] for record in records],
            "gepa_search_sessions": len(sessions), "holdout_executed": False, "formal_unchanged": True}


def step3(run, state):
    generation = native.read(run / "learning/generation.json")
    replays, sessions = [], []
    with native.connection():
        for record in generation["records"]:
            rows = []
            for task in generation["tasks"]:
                row = {"case": task["case"], "task_id": task["id"], "incident_id": task["incident_id"]}
                for arm, skill in (("baseline", run / f"formal/{SKILL}/SKILL.md"), ("candidate", run / record["path"])):
                    directory = run / "learning/replay" / record["source"] / task["case"] / arm
                    result = native.execute_task(directory, skill, task=task,
                                context_file=run / "external-context/external-context.json")
                    if (result["evidence"]["valid_trace"] is not True or result["loaded_skill_sha256"] != native.sha(skill)
                            or result["session_id"] in generation["excluded_sessions"]):
                        raise StepError("留出回放没有独立、有效且版本固定的原生证据。")
                    sessions.append(result["session_id"])
                    row[arm] = {"session_id": result["session_id"], "skill_sha256": result["loaded_skill_sha256"],
                                "trace": result["evidence"], "task": str(directory.relative_to(run))}
                for field in ("snapshot_sha256", "context_sha256", "scope"):
                    if row["baseline"]["trace"][field] != row["candidate"]["trace"][field]:
                        raise StepError("新旧版本面对的冻结输入不同。")
                rows.append(row)
            replays.append({"candidate": record, "cases": rows})
    if len(sessions) != len(set(sessions)):
        raise StepError("不同回放复用了原生 Session。")
    return {"replays": replays, "holdout_sessions": len(sessions), "scores_assigned": False,
            "publication_decided": False, "formal_sha256": state["formal_sha256"]}


def grade(run, *, outage):
    raw_path = result_path(run, 3)
    raw, generation = native.read(raw_path), native.read(run / "learning/generation.json")
    evaluations = []
    for replay in raw["replays"]:
        rows = deepcopy(replay["cases"])
        for row in rows:
            for arm in ("baseline", "candidate"):
                row[arm]["score"] = score(row[arm]["trace"], unavailable=outage and arm == "candidate" and row["case"] == "upstream")
        record = replay["candidate"]
        if native.sha(run / record["path"]) != record["candidate_id"]:
            raise StepError("候选内容改变，旧轨迹不能评估新内容。")
        decision = decide(rows, record["base_version"], record["candidate_id"], source_sessions=generation["excluded_sessions"])
        evaluations.append({"candidate": record, "cases": rows, **decision})
    return {"raw_replay_sha256": native.sha(raw_path), "score_outage": outage, "evaluations": evaluations}


def step4(run, state):
    if controls(run)["score_outage"] is not True:
        raise StepError("第4步先观察评分故障：controls.json 的 score_outage 应为 true。")
    result = grade(run, outage=True)
    if any(item["decision"] != "REJECT" or item["reason"] != "unjudgeable_cases" for item in result["evaluations"]):
        raise StepError("没有验证到评分缺失时拒绝采用。")
    generation = native.read(run / "learning/generation.json")
    task = next(task for task in generation["tasks"] if task["case"] == "drift")
    receiver = run / "rejection/retained-version"
    pull = native.hub(run, "retained-version").pull_skills(str(receiver / "skills"), mirror=True)
    with native.connection():
        actual = native.execute_task(receiver / "task", receiver / f"skills/{SKILL}/SKILL.md", task=task,
                                    context_file=run / "external-context/external-context.json")
    if actual["loaded_skill_sha256"] != state["baseline_sha256"] or len(actual["evidence"]["queries"]) != 2:
        raise StepError("拒绝候选后，保留版没有真实继续执行。")
    check_formal(run, state, native_manifest=True)
    result.update(retained_task={"session_id": actual["session_id"], "report_item_id": actual["report_item_id"],
                                "loaded_sha256": actual["loaded_skill_sha256"], "queries": actual["evidence"]["queries"], "pull": pull},
                  formal_unchanged=True)
    return result


def step5(run, state):
    control = controls(run)
    if control["score_outage"] is not False:
        raise StepError("先预测结果，再运行 scoring --available；第5步只改变评分可用性，不重跑模型。")
    prior = native.read(result_path(run, 4))
    result = grade(run, outage=False)
    if result["raw_replay_sha256"] != prior["raw_replay_sha256"]:
        raise StepError("第4步与第5步使用的原生回放不是同一份。")
    approved = [item for item in result["evaluations"] if item["decision"] == "APPROVE"]
    if not approved:
        raise StepError("恢复评分后仍未满足业务门槛，正式版保持不变。")
    chosen = next((item for item in approved if item["candidate"]["source"] == control["prefer_source"]), approved[0])
    record = chosen["candidate"]
    if record["base_version"] != state["baseline_sha256"]:
        raise StepError("候选的父版本不是本次基线。")
    candidate = run / record["path"]
    publication = native.hub(run, "exercise-approved").push_skills(str(candidate.parent.parent))
    state["formal_sha256"] = record["candidate_id"]
    check_formal(run, state, native_manifest=True)
    result.update(published_source=record["source"], published_sha256=record["candidate_id"], publication=publication,
                  model_tasks_executed=0, prior_rejection_sha256=native.sha(result_path(run, 4)))
    return result


def step6(run, state):
    with native.connection():
        source = native.read(run / "experience.json")
        continued = native.restore_checkpoint(run, source, context_mode="frozen")
        feedback = native.collect(run, shared_file(run), alias="instance-c", role="receiver",
                                  client=run / "receiver/instance-c", context_mode="frozen")
        write(run / "next-experience.json", feedback)
    receiver = native.read(run / "receiver/instance-c/task/result.json")
    if (continued["loaded_sha256"] != state["baseline_sha256"] or feedback["session_id"] == source["session_id"]
            or receiver["loaded_skill_sha256"] != state["formal_sha256"] or len(receiver["evidence"]["queries"]) != 4):
        raise StepError("原Session版本固定或新客户端实际采用没有通过。")
    return {"continued": continued, "receiver_session_id": receiver["session_id"], "receiver_report_item_id": receiver["report_item_id"],
            "receiver_sha256": receiver["loaded_skill_sha256"], "receiver_queries": receiver["evidence"]["queries"],
            "feedback_archive": feedback["archive"], "context_mode": "frozen", "next_evolution_started": False}


def owned_paths(run, number, *, tolerate_source_error=False):
    group = native.load_run(run)["group_id"]
    paths = {1: ["formal", "online", "external-context", "context-manifest.json", "openviking-service", "experience.json", "initial-push.json", "proxy/instance-a"],
             2: ["learning/native", "learning/gepa", "learning/generation.json", "candidate-records", "candidates", "candidates.json"],
             3: ["learning/replay"], 4: ["rejection"], 5: [], 6: ["continued", "receiver", "next-experience.json", "proxy/instance-c"]}[number]
    paths = [*paths, "steps/" + RESULTS[number] + ".json"]
    if number in (1, 6):
        source_file = run / ("experience.json" if number == 1 else "next-experience.json")
        sources = [source_file]
        if number == 6:
            sources.append(run / "receiver/instance-c/task/result.json")
        for source_file in sources:
            if source_file.is_file():
                try:
                    session = native.read(source_file)["session_id"]
                    if not isinstance(session, str) or not session.startswith("lesson23-") or "/" in session:
                        raise StepError("归档 Session 不属于本讲。")
                    paths += [f"experiences/{session}.json", f"shared/{group}/sessions/{session}.json"]
                except (OSError, ValueError, KeyError):
                    if not tolerate_source_error:
                        raise
    return paths


def execute(run, number):
    run, state = open_state(run)
    with locked(run):
        run, state = open_state(run)
        verify(run, state)
        for previous in range(1, number):
            if str(previous) not in state["completed"]:
                raise StepError(f"先完成第{previous}步；本命令没有补跑任何前置步骤。")
        if str(number) in state["completed"]:
            return {"status": "already_complete", "step": number, "result": str(result_path(run, number)), "model_tasks_executed": 0}
        if state.get("failed"):
            raise StepError(f"上次第{state['failed']['step']}步未完成，证据已保留；先 reset --from-step {state['failed']['step']} 再重试。")
        if number == 5 and controls(run)["score_outage"]:
            raise StepError("先预测结果，再运行 scoring --available；第5步没有重跑模型或修改任何状态。")
        guard_outputs(run, ["steps", *owned_paths(run, number, tolerate_source_error=True),
                            "shared/" + native.load_run(run)["group_id"]])
        preflight(number)
        check_formal(run, state, native_manifest=number > 1)
        state["failed"] = {"step": number, "status": "running"}
        write(run / "state.json", state)
        try:
            result = globals()["step" + str(number)](run, state)
            write(result_path(run, number), result)
            state["completed"][str(number)] = {"finished_at": time.time(), "files": fingerprint(run, owned_paths(run, number))}
            state["failed"] = None
            write(run / "state.json", state)
        except BaseException as exc:
            state["failed"] = {"step": number, "status": "failed", "error": str(exc)}
            write(run / "state.json", state)
            raise
    return {"status": "complete", "step": number, "title": TITLES[number], "result": str(result_path(run, number)),
            "next_step": number + 1 if number < 6 else None}


def set_scoring(run):
    run, state = open_state(run)
    with locked(run):
        run, state = open_state(run)
        verify(run, state)
        if "4" not in state["completed"] or "5" in state["completed"]:
            raise StepError("先完成第4步拒绝验证；已发布后请先 reset --from-step 5。")
        value = controls(run)
        value["score_outage"] = False
        write(run / "controls.json", value)
    return {"score_outage": False, "changed": "only scoring availability", "model_tasks_executed": 0}


def reset(run, first):
    run, state = open_state(run)
    with locked(run):
        run, state = open_state(run)
        # A reset may discard changed evidence from this point onward, but must
        # preserve and verify every earlier step before touching owned paths.
        verify(run, {**state, "formal_sha256": None}, through=first - 1)
        paths = []
        for number in range(first, 7):
            paths.extend(owned_paths(run, number, tolerate_source_error=True))
            paths.extend(relative for relative in state["completed"].get(str(number), {}).get("files", {})
                         if relative.startswith(("experiences/", "shared/")))
        if first == 1:
            paths.remove("formal")  # immutable seed belongs to init, not to the live service
            paths.extend(["shared/" + native.load_run(run)["group_id"], "experiences"])
        resolved = [(relative, own(run, relative)) for relative in dict.fromkeys(paths)]
        guard_outputs(run, [*paths, "shared/" + native.load_run(run)["group_id"]])
        archive = own(run, "archive/" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
        archive.mkdir(parents=True)
        write(archive / "state-before-reset.json", state)
        if first <= 5 and (run / "shared").exists():
            receipt = native.hub(run, "exercise-reset").push_skills(str(run / "formal"))
            state["formal_sha256"] = state["baseline_sha256"]
            check_formal(run, state, native_manifest=True)
            write(archive / "formal-restoration.json", receipt)
        for relative, source in reversed(resolved):
            if source.exists():
                destination = archive / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), destination)
        for key in list(state["completed"]):
            if int(key) >= first:
                del state["completed"][key]
        state["failed"] = None
        if first == 1:
            state["formal_sha256"] = None
        if first <= 4:
            value = controls(run)
            value["score_outage"] = True
            write(run / "controls.json", value)
        write(run / "state.json", state)
    return {"status": "reset", "next_step": first, "archive": str(archive), "model_tasks_executed": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    create = commands.add_parser("init", help="Create only this exercise's starting state; no models or services")
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--prefer-source", choices=("skillclaw", "gepa"), default="gepa")
    step = commands.add_parser("step", help="Execute exactly one step, never its prerequisites or successors")
    step.add_argument("number", type=int, choices=range(1, 7))
    step.add_argument("--run", type=Path, required=True)
    for name in ("status", "scoring", "reset"):
        child = commands.add_parser(name)
        child.add_argument("--run", type=Path, required=True)
        if name == "scoring":
            child.add_argument("--available", action="store_true", required=True)
        elif name == "reset":
            child.add_argument("--from-step", type=int, choices=range(1, 7), required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init(args.out, args.prefer_source)
        elif args.command == "step":
            result = execute(args.run, args.number)
        elif args.command == "scoring":
            result = set_scoring(args.run)
        elif args.command == "reset":
            result = reset(args.run, args.from_step)
        elif args.command == "status":
            run, state = open_state(args.run)
            verify(run, state)
            result = {"run": str(run), "completed": sorted(map(int, state["completed"])), "failed": state["failed"],
                      "next_step": next((n for n in range(1, 7) if str(n) not in state["completed"]), None),
                      "controls": controls(run), "formal_sha256": state["formal_sha256"], "evidence_intact": True}
        else:
            parser.print_help()
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "not_completed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    native.install_signal_handlers()
    main()
