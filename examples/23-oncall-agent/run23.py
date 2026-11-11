"""Assemble lesson 23's candidate handoff without starting the separate capstone."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
from skillclaw_lab.compat import strict_evolution_result, structured_tool_status_compat
from skillclaw_lab.observations import verify_archive
from skillclaw_lab.runtime import PYTHON, hub, install_signal_handlers, load_run, make_config, save, sha, stop_process_group
from scenario import CASES, METHOD, SCOPE, SKILL, decide, score, snapshots
from script_model import connection


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def execute_task(directory, skill, case="drift", role="receiver", *, base_url=None, resume=False):
    directory.mkdir(parents=True, exist_ok=resume)
    request = {"skill": str(skill.resolve()), "case": case, "role": role, "scope": SCOPE,
               "base_url": base_url or os.environ["GLM_BASE_URL"], "model": os.environ["GLM_MODEL"], "resume": resume}
    save(directory / "input.json", request)
    with (directory / ("resume.log" if resume else "task.log")).open("w") as log:
        worker = subprocess.Popen([str(PYTHON), "-B", str(HERE / "task_worker.py"), str(directory)],
                                  stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            worker.wait(timeout=120)
        finally:
            stop_process_group(worker)
    if worker.returncode:
        raise RuntimeError(f"Native Hermes task failed; inspect {directory}")
    return read(directory / "result.json")


def collect(run, skill, *, alias="instance-a", role="source", client=None):
    from skillclaw.api_server import SkillClawAPIServer

    client = client or run / "online" / alias
    client.mkdir(parents=True)
    skills = client / "skills"
    save(client / "pull.json", hub(run, alias).pull_skills(str(skills), mirror=True))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = make_config(run, alias, skills, port)
    proxy = SkillClawAPIServer(config)
    with structured_tool_status_compat() as compatibility:
        proxy.start()
        try:
            if not proxy.wait_until_ready(30):
                raise RuntimeError("Native Client Proxy did not become ready")
            result = execute_task(client / "task", skills / SKILL / "SKILL.md", role=role,
                                  base_url=f"http://127.0.0.1:{port}/v1")
        finally:
            proxy.stop()
            proxy._thread.join(timeout=60)
            if proxy._thread.is_alive():
                raise RuntimeError("Native Client Proxy did not finish archiving")
    archive = run / "shared" / load_run(run)["group_id"] / "sessions" / (result["session_id"] + ".json")
    if result["loaded_skill_sha256"] != sha(skill):
        raise ValueError("Collected task did not actually load the supplied formal version")
    messages = read(client / "task" / "messages.json")
    checked = verify_archive(read(archive), messages, result["session_id"], alias)
    if not checked["usable_for_evolution"]:
        raise ValueError("Native archive metadata conflicts with actual tool receipts")
    retained = run / "experiences" / archive.name
    retained.parent.mkdir(exist_ok=True)
    shutil.copyfile(archive, retained)
    return {"instance": alias, "session_id": result["session_id"], "report_item_id": result["report_item_id"],
            "skill_sha256": result["loaded_skill_sha256"], "archive": str(retained.relative_to(run)), "archive_sha256": sha(retained),
            "native_archive_checks": checked, "compatibility": compatibility,
            "task": str((client / "task").relative_to(run))}


def native_candidate(run, source):
    """Native Summarize/Aggregate/Execute in a separate staging store."""
    from evolve_server.core.config import EvolveServerConfig
    from evolve_server.core.utils import parse_skill_content
    from evolve_server.engines.workflow import EvolveServer

    stage = run / "learning" / "native"
    stage.mkdir(parents=True)
    save(stage / "run.json", load_run(run))
    baseline = run / "formal" / SKILL / "SKILL.md"
    staged_seed = stage / "seed" / SKILL / "SKILL.md"
    staged_seed.parent.mkdir(parents=True)
    shutil.copyfile(baseline, staged_seed)
    hub(stage, "staging-seed").push_skills(str(stage / "seed"))
    queue = stage / "shared" / load_run(run)["group_id"] / "sessions"
    queue.mkdir(parents=True)
    archive = run / source["archive"]
    if sha(archive) != source["archive_sha256"] or source["skill_sha256"] != sha(baseline):
        raise ValueError("Source archive or its actual Skill version changed before evolution")
    shutil.copyfile(archive, queue / archive.name)
    observed = {}
    for turn in read(archive)["turns"]:
        for receipt in turn.get("tool_results_raw", []):
            if receipt.get("name") in {"query_pool_config", "query_pool_effective"}:
                observed[receipt["name"]] = json.loads(receipt["content"])
    if (set(observed) != {"query_pool_config", "query_pool_effective"}
            or observed["query_pool_config"]["max_connections"] == observed["query_pool_effective"]["max_connections"]):
        raise ValueError("Source archive does not support the configuration-comparison method")
    baseline_skill = parse_skill_content(SKILL, baseline.read_text())
    engine = EvolveServer(EvolveServerConfig(
        storage_backend="local", local_root=str(stage / "shared"), group_id=load_run(stage)["group_id"],
        llm_api_key="lesson23-local-script", llm_base_url=os.environ["GLM_BASE_URL"], llm_model="lesson23-script",
        publish_mode="validated", use_session_judge=False, use_skill_verifier=False, skill_reload_mode="off",
        history_path=str(stage / "evolve_history.jsonl"), processed_log_path=str(stage / "processed.json"),
        debug_dump_dir=str(stage / "debug"),
    ))

    async def scripted(messages, **_kwargs):
        system = messages[0]["content"]
        if "produce a trajectory-aware analytical summary" in system:
            return "实际工具回执显示：" + json.dumps(observed, ensure_ascii=False) + "。应比较配置与生效值；差异尚不能证明根因。"
        if "edit the ORIGINAL skill" not in system or source["session_id"] not in messages[1]["content"]:
            raise ValueError("Native Execute did not carry the recorded source Session")
        addition = ("\n\n## " + METHOD + "\n\n"
                    "仅在健康正常且上游 P95 不大于 1000 毫秒时，调用 query_pool_config 查询配置文件的连接池上限，"
                    "再用 query_pool_effective 查询实例实际生效值与等待数。比较两份实际回执，记录差异并继续调查配置漂移的关联。"
                    "上游已异常时不执行此分支。差异仅是观测，不能直接断言根因。\n")
        return json.dumps({"action": "improve_skill", "rationale": "来源会话中的真实参数差异支持补齐条件诊断步骤。",
                           "skill": {**baseline_skill, "content": baseline_skill["content"] + addition}}, ensure_ascii=False)

    engine._llm.chat = scripted
    try:
        with strict_evolution_result() as compatibility:
            native = asyncio.run(engine.run_once())
    finally:
        engine._llm._client.close()
    entries = native["evolutions"]
    if (native["had_processing_error"] or native["sessions"] != 1 or native["uploaded_skills"]
            or len(entries) != 1 or entries[0]["action"] != "queued_for_validation"
            or entries[0]["skill_name"] != SKILL or set(entries[0]["session_ids"]) != {source["session_id"]}):
        raise RuntimeError("Native evolution did not produce exactly one isolated, sourced candidate")
    candidate = stage / "shared" / load_run(stage)["group_id"] / "candidate_skills" / entries[0]["validation_job_id"] / "SKILL.md"
    if sha(stage / "shared" / load_run(stage)["group_id"] / "skills" / SKILL / "SKILL.md") != sha(baseline):
        raise ValueError("Candidate generation changed its formal baseline")
    save(stage / "summary.json", {"native_workflow": native, "compatibility": compatibility,
                                  "scripted_model": True, "session_judge_enabled": False,
                                  "native_validation": "queued_only; independent lesson gate decides publication"})
    return candidate, entries[0]["validation_job_id"]


def register_candidate(run, candidate, baseline, source, *, generator="skillclaw", job=None):
    if generator not in {"skillclaw", "gepa"}:
        raise ValueError("Candidate source must be skillclaw or gepa")
    if not candidate.read_text().startswith("---\nname: " + SKILL + "\n"):
        raise ValueError("This lesson only evaluates its own timeout-triage Skill")
    if generator == "skillclaw":
        provenance = {"source_sessions": [source["session_id"]], "source_archive_sha256": source["archive_sha256"]}
    else:
        if (not isinstance(source.get("dataset_sha256"), str) or len(source["dataset_sha256"]) != 64
                or not source.get("optimizer_run")):
            raise ValueError("A GEPA handoff needs its frozen dataset fingerprint and optimizer run")
        provenance = {"dataset_sha256": source["dataset_sha256"], "optimizer_run": source["optimizer_run"]}
    digest = sha(candidate)
    path = run / "candidates" / digest / SKILL / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(candidate, path)
    record = {"candidate_id": digest, "base_version": sha(baseline), "source": generator,
              "status": "pending_evaluation", "skill": SKILL, "path": str(path.relative_to(run)),
              **provenance,
              "native_validation_job": job}
    save(run / "candidate.json", record)
    return record


def learn(run):
    started = time.time()
    source = read(run / "experience.json")
    baseline = run / "formal" / SKILL / "SKILL.md"
    candidate, job = native_candidate(run, source)
    record = register_candidate(run, candidate, baseline, source, job=job)
    save(run / "learning" / "fixtures.json", {case: snapshots(case) for case in CASES})
    rows = []
    for case in CASES:
        row = {"case": case}
        for arm, skill in (("baseline", baseline), ("candidate", run / record["path"])):
            task = run / "learning" / "evaluation" / case / arm
            try:
                result = execute_task(task, skill, case)
                trace = result["evidence"]
                assessment = score(trace, unavailable=(not load_run(run)["complete_scores"] and case == "upstream" and arm == "candidate"))
                row[arm] = {"session_id": result["session_id"], "skill_sha256": result["loaded_skill_sha256"],
                            "trace": trace, "score": assessment, "task": str(task.relative_to(run))}
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                row[arm] = {"trace": {"valid_trace": False}, "score": {"status": "execution_error", "passed": None}, "error": str(exc)}
        rows.append(row)
    decision = decide(rows, record["base_version"], record["candidate_id"], source_sessions=[source["session_id"]])
    record.update(status="approved" if decision["decision"] == "APPROVE" else "rejected", evaluation="learning/evaluation.json")
    save(run / "candidate.json", record)
    result = {"candidate": record, "cases": rows, **decision, "started_at": started, "finished_at": time.time()}
    save(run / "learning" / "evaluation.json", result)
    save(run / "learning" / "processed-sources.json", {"source_sessions": [source["session_id"]],
                                                        "native_staging": "learning/native"})
    return result


def restore_checkpoint(run, source):
    started = time.time()
    original = run / source["task"]
    restored = run / "continued" / "instance-a"
    restored.mkdir(parents=True)
    shutil.copytree(original / "home", restored / "home", ignore=shutil.ignore_patterns("state.db", "state.db-wal", "state.db-shm"))
    with closing(sqlite3.connect(original / "home" / "state.db")) as src, closing(sqlite3.connect(restored / "home" / "state.db")) as dst:
        src.backup(dst)
    shutil.copyfile(original / "checkpoint.json", restored / "checkpoint.json")
    checkpoint = read(restored / "checkpoint.json")
    task = execute_task(restored, run / "formal" / SKILL / "SKILL.md", role="receiver", resume=True)
    if (task["session_id"] != checkpoint["session_id"] or not task["resumed"]
            or task["report_item_id"] <= checkpoint["report_item_id"] or not task["prior_message_count"]):
        raise ValueError("Restored native Session did not continue beyond its saved report Item")
    return {"checkpoint": checkpoint, "continued_report_item_id": task["report_item_id"],
            "session_id": task["session_id"], "loaded_sha256": task["loaded_skill_sha256"], "task": str(restored.relative_to(run)),
            "started_at": started, "completed_at": time.time()}


def run(out, complete_scores):
    out.mkdir(parents=True, exist_ok=False)
    save(out / "run.json", {"format": "skillclaw-course-v1", "group_id": "lesson23-" + uuid4().hex[:12],
                            "publish_mode": "validated", "model": "lesson23-script", "complete_scores": complete_scores,
                            "scope": "lesson23_candidate_handoff", "mode": "local-script-model"})
    shutil.copytree(HERE / "seed", out / "formal")
    baseline = out / "formal" / SKILL / "SKILL.md"
    baseline_sha = sha(baseline)
    with connection():
        save(out / "initial-push.json", hub(out, "formal-publisher").push_skills(str(out / "formal")))
        source = collect(out, baseline)
        save(out / "experience.json", source)
        (out / "learning").mkdir()
        with (out / "learning" / "worker.log").open("w") as log:
            background = subprocess.Popen([str(PYTHON), "-B", str(HERE / "run23.py"), "--learn-worker", str(out)],
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                continued = restore_checkpoint(out, source)
                background.wait(timeout=180)
            finally:
                stop_process_group(background)
        if background.returncode:
            decision = {"decision": "REJECT", "reason": "background_error", "worker_log": "learning/worker.log"}
            save(out / "learning" / "failure.json", decision)
        else:
            decision = read(out / "learning" / "evaluation.json")
        approved = decision["decision"] == "APPROVE"
        expected = decision["candidate"]["candidate_id"] if approved else baseline_sha
        if approved:
            candidate_file = out / decision["candidate"]["path"]
            reevaluated = decide(decision["cases"], baseline_sha, decision["candidate"]["candidate_id"], source_sessions=[source["session_id"]])
            if (sha(candidate_file) != expected or decision["candidate"]["base_version"] != baseline_sha
                    or reevaluated["decision"] != "APPROVE"):
                raise ValueError("Candidate bytes or independently evaluated evidence changed before publication")
            publication = hub(out, "approved-publisher").push_skills(str((out / decision["candidate"]["path"]).parent.parent))
            save(out / "approved-push.json", publication)
        formal_file = out / "shared" / load_run(out)["group_id"] / "skills" / SKILL / "SKILL.md"
        listing = next(entry for entry in hub(out).list_remote() if entry["name"] == SKILL)
        if sha(formal_file) != expected or listing["sha256"] != expected or sha(baseline) != baseline_sha:
            raise ValueError("Publication decision, formal content and pinned original version disagree")
        receiver = out / "receiver" / "instance-c"
        feedback = collect(out, formal_file, alias="instance-c", role="receiver", client=receiver)
        save(out / "next-experience.json", feedback)
        actual = read(receiver / "task" / "result.json")
        if actual["loaded_skill_sha256"] != expected or actual["session_id"] == source["session_id"]:
            raise ValueError("The new receiver did not actually load the approved formal version independently")
    report = {"status": "complete" if background.returncode == 0 else "background_failed_online_continued",
              "lesson": 23, "mode": "local-script-model", "baseline_sha256": baseline_sha,
              "coverage": {"native_hermes_sessiondb": "executed", "native_skillclaw": "executed",
                           "frozen_pair_evaluation": "executed", "openviking": "not_executed; local frozen fixtures supply context",
                           "gepa": "handoff_contract_only; optimizer search and its dataset adapter not executed"},
              "decision": decision["decision"], "decision_reason": decision["reason"],
              "source_session": source["session_id"], "source_archive": source["archive"],
              "online_continued": continued, "background_pid": background.pid,
              "formal_sha256": expected, "formal_unchanged": expected == baseline_sha,
              "next_experience": "next-experience.json", "next_evolution": "not_started; use only new source Sessions from the next batch",
              "receiver": {"instance": "instance-c", "session_id": actual["session_id"], "actual_loaded_sha256": actual["loaded_skill_sha256"],
                           "queries": actual["evidence"]["queries"], "task": str((receiver / "task").relative_to(out))},
              "candidate": "candidate.json" if (out / "candidate.json").is_file() else None,
              "evaluation": "learning/evaluation.json" if (out / "learning/evaluation.json").is_file() else None}
    save(out / "report.json", report)
    (out / "REPORT.md").write_text(
        f"# 第23讲运行记录\n\n模型模式：`local-script-model`。Hermes、SessionDB、Client Proxy、Evolve Server 与 SkillHub 实际运行。\n\n"
        f"本轮决定：**{report['decision']}**，原因：`{report['decision_reason']}`。\n\n"
        f"正式版是否保持原内容：`{report['formal_unchanged']}`。实例 A 从报告 Item {continued['checkpoint']['report_item_id']} 恢复后继续到 {continued['continued_report_item_id']}。\n\n"
        f"实例 C 的新 Session：`{actual['session_id']}`，实际读取 hash：`{expected}`。\n\n"
        "依次查看 `experience.json`、`candidate.json`、`learning/evaluation.json`、`continued/instance-a/result.json` 和 `receiver/instance-c/task/result.json`。\n\n"
        "评分故障属于评测状态，不能写成业务失败，也不能用排除故障后的局部通过率批准发布。快照仅恢复会话历史，不恢复进程或业务副作用。\n",
        encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Create a new independent output directory")
    parser.add_argument("--complete-scores", action="store_true", help="Remove the explicit scoring outage and test the same gate again in a new run")
    parser.add_argument("--learn-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.learn_worker:
        learn(args.learn_worker.resolve())
        return
    out = (args.out or HERE / "runtime" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])).resolve()
    report = run(out, args.complete_scores)
    print(json.dumps({"status": report["status"], "decision": report["decision"], "reason": report["decision_reason"],
                      "formal_unchanged": report["formal_unchanged"], "report": str(out / "REPORT.md")}, ensure_ascii=False, indent=2))
    if report["status"] != "complete":
        raise SystemExit(2)


if __name__ == "__main__":
    install_signal_handlers()
    main()
