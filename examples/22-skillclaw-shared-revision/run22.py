"""用原生验证和共享接口继续第21讲，再观察新会话中的真实工具行为。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from functools import partial
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from skillclaw_lab.runtime import load_run, make_config, hub, evolve_once, run_task, sha, install_signal_handlers
from skillclaw_lab.observations import archive_metadata_conflicts, verify_task

SKILL = "timeout-triage"
ALIAS = "validator-22"
RERUN = "请检查第21讲记录；本程序不会替你重跑或追加会话演化。"


class StopRun(RuntimeError):
    """条件不足，保留已经得到的记录并停止。"""


def save(path: Path, value) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    secret = os.environ.get("GLM_API_KEY", "")
    if secret:
        text = text.replace(secret, "[凭据已隐藏]")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise StopRun("共享组、任务和技能名称必须是单个安全目录名。")
    return value


def require_plain_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise StopRun(f"不接受软链接或特殊文件：{path}")


def source_parts(source: Path) -> list[Path]:
    manifest = load_run(source)
    if manifest.get("publish_mode") not in ("direct", "validated"):
        raise StopRun("来源 run 的发布模式不受支持。")
    group = component(manifest.get("group_id"))
    parts = [source / "run.json", source / "seed", source / "shared" / group,
             source / "evolve_history.jsonl"]
    if manifest['publish_mode'] == 'direct':
        parts.append(source / 'result.json')
    if (source / 'collected').exists():
        parts.append(source / 'collected')
    if (source / 'collection-checks.json').is_file():
        parts.append(source / 'collection-checks.json')
    if not (source / "seed" / SKILL / "SKILL.md").is_file():
        raise StopRun("来源 run 没有保留旧版 seed/timeout-triage/SKILL.md。")
    for part in parts:
        if not part.exists():
            raise StopRun(f"来源 run 缺少 {part.name}，不能还原第21讲的运行。")
        require_plain_tree(part)
    for archive in (source / 'collected').glob('*.json'):
        if archive_metadata_conflicts(json.loads(archive.read_text())):
            raise StopRun('第21讲原生归档的错误摘要与成功回执冲突；不能继续把该候选当成可信演化结果。'
                          '保留旧实跑记录，先处理上游采集边界；此处不会创建新候选。')
    if (source / "processed.json").exists():
        require_plain_tree(source / "processed.json")
        parts.append(source / "processed.json")
    sessions = source / "shared" / group / "sessions"
    if any(sessions.rglob("*.json")):
        raise StopRun("来源仍有待演化会话；先检查第21讲的失败记录。本讲不悄悄重试这些会话。")
    return parts


def fingerprints(source: Path, parts: list[Path]) -> dict:
    return {str(path.relative_to(source)): sha(path)
            for part in parts for path in ([part] if part.is_file() else sorted(part.rglob("*")))
            if path.is_file()}


def copy_source(source: Path) -> tuple[Path, dict]:
    parts = source_parts(source)
    before = fingerprints(source, parts)
    runtime = HERE / "runtime"
    if runtime.is_symlink():
        raise StopRun("runtime 不能是指向外部目录的软链接。")
    runtime.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="shared-", dir=runtime))
    for part in parts:
        target = run / part.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if part.is_dir():
            shutil.copytree(part, target)
        else:
            shutil.copyfile(part, target)
    if fingerprints(source, parts) != before or fingerprints(run, source_parts(run)) != before:
        raise StopRun(f"复制期间来源文件发生变化，请等待第21讲结束后重试。副本保留在 {run}")
    save(run / "source.json", {"source_run": str(source), "files_sha256": before})
    return run, before


def validation_store(run: Path):
    # 上游 __init__ 会导入 workflow，需先完成它再读取 ValidationStore。
    import evolve_server
    from skillclaw.validation_store import ValidationStore
    return ValidationStore(backend="local", endpoint="", bucket="", access_key_id="",
                           secret_access_key="", local_root=str(run / "shared"),
                           group_id=component(load_run(run)["group_id"]))


def pending_job(store) -> dict:
    jobs = store.list_open_jobs()
    if not jobs:
        raise StopRun("没有待验证候选；尚未进行模型调用。请检查第21讲记录或已有发布决定。" + RERUN)
    if len(jobs) != 1 or jobs[0].get("candidate_skill", {}).get("name") != SKILL:
        raise StopRun("本练习需要唯一的 timeout-triage 待验证候选，不会代选候选或验证其他技能。")
    job = jobs[0]
    component(job["job_id"])
    if not job.get("current_skill") or not job.get("replay_cases"):
        raise StopRun("候选缺少 baseline 或回放题，无法完成原生对照验证。")
    if store.load_result(job["job_id"], ALIAS):
        raise StopRun("该验证客户端已有结果；本讲不会重复消费模型或覆盖结果。")
    return job


async def validate_once(run: Path) -> dict:
    from skillclaw.validation_worker import ValidationWorker
    config = make_config(run, ALIAS, run / "validator" / "skills")
    config.validation_enabled = True
    config.validation_max_concurrency = 1
    config.validation_max_jobs_per_day = 1
    worker = ValidationWorker(config)
    # 仅调整 GLM 请求参数，不替换原生回放、评分或决定逻辑。
    worker._client._client = worker._client._client.with_options(timeout=120, max_retries=0)
    worker._prm_scorer._client = worker._prm_scorer._client.with_options(timeout=120, max_retries=0)
    original_chat = worker._client.chat

    async def glm_chat(messages, **kwargs):
        kwargs.setdefault("extra_body", {"thinking": {"type": "disabled"}})
        return await original_chat(messages, **kwargs)

    worker._client.chat = glm_chat
    client = worker._prm_scorer._client
    client.chat.completions.create = partial(
        client.chat.completions.create, extra_body={"thinking": {"type": "disabled"}})
    try:
        return await worker.run_once(force=True)
    finally:
        worker._client._client.close()
        worker._prm_scorer._client.close()


def remote_listing(run: Path) -> list[dict]:
    records = hub(run).list_remote()
    for record in records:
        component(record["name"])
        for entry in record.get("files", []):
            path = str(entry["path"])
            if (PurePosixPath(path).is_absolute() or "\\" in path
                    or any(part in ("", ".", "..") for part in path.split("/"))):
                raise StopRun("远端技能清单含越界文件路径，已停止拉取。")
    return records


async def wait_native_poll(run: Path, expected_sha: str, timeout: float = 45) -> dict:
    from skillclaw.api_server import SkillClawAPIServer
    root = run / 'receiver/skills'
    config = make_config(run, 'receiver-22', root)
    config.sharing_skill_reload_mode = 'poll'
    config.sharing_skill_reload_interval_seconds = 5
    config.record_enabled = False
    proxy = SkillClawAPIServer(config)
    completed_polls = 0
    native_pull = proxy._pull_skills_from_cloud

    async def observed_pull(*args, **kwargs):
        nonlocal completed_polls
        await native_pull(*args, **kwargs)
        completed_polls += 1

    proxy._pull_skills_from_cloud = observed_pull
    # 运行原生轮询任务，不启动 HTTP 监听或长期服务。
    proxy._start_skill_reload_polling()
    try:
        async with asyncio.timeout(timeout):
            while True:
                skill = root / SKILL / 'SKILL.md'
                if completed_polls >= 2 and skill.is_file() and sha(skill) == expected_sha:
                    return {'mode': 'poll', 'native_poll_task': True, 'completed_polls': completed_polls,
                            'no_model_messages_sent': True, 'expected_content_observed': True}
                if proxy._skill_reload_task.done():
                    await proxy._skill_reload_task
                    raise StopRun('原生轮询在同步完成前退出。')
                await asyncio.sleep(0.05)
    finally:
        await proxy._shutdown_cleanup()


def pull_receiver(run: Path, expected_sha: str, mode: str = 'manual') -> dict:
    root = run / "receiver" / "skills"
    if not root.resolve().is_relative_to(run.resolve()):
        raise StopRun("接收端必须位于本次 run 内。")
    records = remote_listing(run)
    if mode == 'manual':
        result = hub(run).pull_skills(str(root), mirror=True)
    elif mode == 'poll':
        result = asyncio.run(wait_native_poll(run, expected_sha))
    else:
        raise ValueError('未知同步方式')
    skill = root / SKILL / "SKILL.md"
    remote = run / "shared" / component(load_run(run)["group_id"]) / "skills" / SKILL / "SKILL.md"
    record = next((item for item in records if item["name"] == SKILL), {})
    storage_checks = {
        "shared_file_matches": remote.is_file() and sha(remote) == expected_sha,
        "remote_manifest_matches": record.get("sha256") == expected_sha,
        "receiver_file_matches": skill.is_file() and sha(skill) == expected_sha,
    }
    if not all(storage_checks.values()):
        raise StopRun("拉取后文件、共享清单与预期版本不一致，不能启动接收端会话。")
    return {"mode": mode, "list_remote": records, "pull": result, "skill_sha256": sha(skill),
            "storage_checks": storage_checks, "storage_verified": True}


def task_observation(result: dict, expected_sha: str, case: str, *, messages=None, restored=False) -> dict:
    calls = result.get("calls", [])
    try:
        trace = verify_task(messages or [], calls, case, result.get('answer'))
        trace_error = None
    except (ValueError, KeyError, TypeError) as exc:
        trace, trace_error = {}, str(exc)
    any_pool = any(call.get("tool") == "query_connection_pool" for call in calls)
    any_leases = any(call.get("tool") == "query_connection_leases" for call in calls)
    loading_checks = {
        "loaded_sha_matches": result.get("loaded_skill_sha256") == expected_sha,
        "native_skill_view_verified": result.get("native_skill_view_verified") is True,
        "skill_content_in_receipt": expected_sha in trace.get('loaded_skill_hashes', []),
        "skill_not_modified": result.get('skill_unchanged', True) is True,
    }
    execution_checks = {
        "completed": result.get("completed") is True,
        **{key: trace.get(key) is True for key in ('health_and_upstream_receipts', 'receipt_messages_match',
                                                  'snapshot_receipts_match', 'readonly_tools_only',
                                                  'final_answer_matches')},
    }
    ordered_pool = trace.get('pool_after_observed_checks') is True
    observations = {
        "connection_pool_called": any_pool,
        "connection_leases_called": any_leases,
        "no_connection_pool_call": not any_pool,
        "pool_waiting_24_received": any_pool and trace.get('snapshot_receipts_match') is True,
        "pool_after_observed_checks": ordered_pool,
    }
    # 旧 Skill 未禁止额外查询。恢复后观察动作，但不把动作减少当成恢复条件。
    behavior_checks = {} if restored else (
        {"no_connection_pool_call": not any_pool, "no_connection_leases_call": not any_leases} if case == "upstream"
        else {"pool_waiting_24_after_normal_checks": ordered_pool})
    loading_verified = all(loading_checks.values())
    execution_verified = all(execution_checks.values())
    if restored:
        behavior_status = "observation_only"
    elif not loading_verified or not execution_verified:
        behavior_status = "not_assessable"
    else:
        behavior_status = "matched" if all(behavior_checks.values()) else "mismatch"
    return {"loading_checks": loading_checks, "loading_verified": loading_verified,
            "execution_checks": execution_checks, "execution_verified": execution_verified,
            "behavior_checks": behavior_checks, "behavior_status": behavior_status,
            "observations": observations,
            "trace_error": trace_error,
            "session_id": result.get("session_id"), "tool_order": [c.get("tool") for c in calls]}


def summarize_outcomes(report: dict) -> dict:
    tasks = report["tasks"]
    storage = {
        "published_files_synchronized": report["receiver_pull"].get("storage_verified") is True,
        "old_content_restored": report["restore_pull"].get("storage_verified") is True,
    }
    loading = {case: item["loading_verified"] for case, item in tasks.items()}
    execution = {case: item["execution_verified"] for case, item in tasks.items()}
    behavior = {case: item["behavior_status"] for case, item in tasks.items()}
    if not (all(storage.values()) and all(loading.values()) and all(execution.values())
            and report["independent_sessions"]):
        status = "verification_failed"
        message = "文件同步、加载或任务执行未通过检查；条件行为另列，不据此宣称全部成功。"
    elif any(value == "mismatch" for value in behavior.values()):
        status = "behavior_mismatch"
        message = "文件同步、旧内容恢复和新会话读取已确认，但条件行为存在偏差；恢复后动作仅作观察。"
    else:
        status = "completed"
        message = "文件、加载与预设条件检查通过；恢复后动作仅作观察，不代表已经证明新版带来收益。"
    return {"storage": storage, "loading": loading, "execution": execution,
            "behavior": behavior, "status": status, "message": message}


def prepare_publication(run: Path, report: dict) -> None:
    group = component(load_run(run)["group_id"])
    seed = run / "seed" / SKILL / "SKILL.md"
    remote = run / "shared" / group / "skills" / SKILL / "SKILL.md"
    mode = load_run(run)['publish_mode']
    report['publish_mode'] = mode
    collection_checks = run / 'collection-checks.json'
    report['source_collection_checks'] = json.loads(collection_checks.read_text()) if collection_checks.is_file() else None
    report["before"] = {"seed_sha256": sha(seed), "list_remote": remote_listing(run)}
    if mode == 'direct':
        prior = json.loads((run / 'result.json').read_text())
        if (prior.get('publish_mode') != 'direct' or not prior.get('shared_content_changed')
                or prior.get('seed_sha256') != sha(seed) or not remote.is_file()
                or prior.get('shared_sha256') != sha(remote) or sha(remote) == sha(seed)
                or not prior.get('evolution', {}).get('skills_evolved')
                or prior['evolution'].get('had_processing_error')):
            raise StopRun('direct 来源没有确认正式内容已更新；不代为演化或调用验证模型。')
        record = next((item for item in remote_listing(run) if item['name'] == SKILL), {})
        if record.get('sha256') != sha(remote):
            raise StopRun('direct 正式文件与共享清单不一致。')
        report['published'] = {'sha256': sha(remote), 'source': 'lesson21_direct'}
        return
    store = validation_store(run)
    job = pending_job(store)
    job_id = job["job_id"]
    report["job_id"] = job_id
    report["validation_worker"] = asyncio.run(validate_once(run))
    report["native_validation_results"] = store.list_results(job_id)
    save(run / "validation-observation.json", report)
    # 使用同一个 publish_mode 和复制过来的历史；不清空共享库或伪造通过结果。
    report["evolve"] = evolve_once(run)
    report["native_decision"] = store.load_decision(job_id)
    save(run / "validation-observation.json", report)
    decision = report["native_decision"] or {}
    if decision.get("status") != "published":
        status = decision.get("status", "waiting")
        report["status"] = status
        raise StopRun(f"原生发布状态为 {status}；停止后续分发和业务检查，不将候选当成正式版本。")
    if not remote.is_file() or sha(remote) == sha(seed):
        raise StopRun("原生决定虽为 published，但正式文件缺失或与旧版相同，未确认出现新版本。")
    published_sha = sha(remote)
    candidate = run / "shared" / group / "candidate_skills" / job_id / "SKILL.md"
    report["published"] = {"sha256": published_sha, "candidate_sha256": sha(candidate),
                           "candidate_equals_published": sha(candidate) == published_sha}


def run_lesson(run: Path, report: dict, sync_mode: str = 'poll') -> None:
    prepare_publication(run, report)
    group = component(load_run(run)['group_id'])
    seed = run / 'seed' / SKILL / 'SKILL.md'
    remote = run / 'shared' / group / 'skills' / SKILL / 'SKILL.md'
    published_sha = report['published']['sha256']
    backup = run / "observed-published" / SKILL / "SKILL.md"
    backup.parent.mkdir(parents=True)
    shutil.copyfile(remote, backup)
    report["receiver_pull"] = pull_receiver(run, published_sha, sync_mode)
    receiver = run / "receiver" / "skills" / SKILL / "SKILL.md"
    report["tasks"] = {}
    for case in ("pool", "upstream"):
        result = run_task(run / "tasks" / case, receiver, case=case)
        messages = json.loads((run / 'tasks' / case / 'messages.json').read_text())
        report["tasks"][case] = task_observation(result, published_sha, case, messages=messages)
        save(run / "observation.json", report)

    # 恢复的是旧内容；push 仍产生新的共享版本，不撤销历史会话。
    restore_root = run / "restore-source"
    shutil.copytree(seed.parent, restore_root / SKILL)
    report["restore_push"] = hub(run, alias="restore-22").push_skills(str(restore_root))
    if sha(remote) != sha(seed):
        raise StopRun("重新 push 后共享端仍不是旧内容，恢复未完成。")
    report["restore_pull"] = pull_receiver(run, sha(seed), sync_mode)
    result = run_task(run / "tasks" / "restored-pool", receiver, case="pool")
    messages = json.loads((run / 'tasks/restored-pool/messages.json').read_text())
    report["tasks"]["restored-pool"] = task_observation(result, sha(seed), "pool", messages=messages, restored=True)
    session_ids = [item["session_id"] for item in report["tasks"].values()]
    report["independent_sessions"] = bool(all(session_ids) and len(set(session_ids)) == 3)
    report["outcomes"] = summarize_outcomes(report)
    report["status"] = report["outcomes"]["status"]
    report["message"] = report["outcomes"]["message"]


def write_report(run: Path, report: dict) -> None:
    save(run / "observation.json", report)
    if report.get('publish_mode') == 'direct':
        publication = [
            '沿用第21讲已发布的正式版本，本讲不运行 ValidationWorker，也不再次产生发布决定。',
            '原始发布记录见来源 result.json，实际读取的正式文件哈希见 observation.json 的 published。',
        ]
    else:
        publication = [
            f"验证任务：`{report.get('job_id', '尚未选定')}`。",
            f"原生决定：`{(report.get('native_decision') or {}).get('status', '尚无决定')}`。",
            '原生回放回答、PRM 分数和投票见 observation.json 的 native_validation_results。',
            '候选与正式文件可能因原生合并流程不同，published 字段分别记录其哈希。',
        ]
    lines = ["# 第22讲运行记录", "", f"状态：`{report['status']}`。{report.get('message', '')}", "",
             "本次只在第22讲 runtime 的独立副本中验证、发布、同步与恢复；来源 run 不作修改。",
             "原生 ValidationWorker 比较文本回放并使用 PRM 评分，不是业务工具评测。",
             "业务检查另起 Hermes 会话，读取构造的教学快照，不连接生产系统。", "",
             "## 验证与发布", "", f"发布路径：`{report.get('publish_mode', '尚未选定')}`。",
             "第21讲采集使用的课程兼容层标志保存在 observation.json 的 source_collection_checks；缺失不推定为已启用。",
             *publication, "",
             "## 文件、加载与行为", "",
             f"发布版文件同步：`{report.get('receiver_pull', {}).get('storage_verified', '未检查')}`；"
             f"旧内容恢复同步：`{report.get('restore_pull', {}).get('storage_verified', '未检查')}`。", "",
             f"同步方式：`{report.get('receiver_pull', {}).get('mode', '未执行')}`；poll 使用原生轮询任务，限时观察后关闭，不启动 HTTP 服务。", "",
             "| 会话 | Skill 加载 | 查询执行 | 条件行为 | 实际工具顺序 |", "| --- | --- | --- | --- | --- |"]
    labels = {"matched": "符合预设", "mismatch": "存在偏差", "observation_only": "仅作观察",
              "not_assessable": "未能判定"}
    for case, result in report.get("tasks", {}).items():
        lines.append(f"| [{case}](tasks/{case}/result.json) | {result['loading_verified']} | "
                     f"{result['execution_verified']} | {labels[result['behavior_status']]} | "
                     + "、".join(result["tool_order"]) + " |")
    lines += ["", "pool 应在健康、上游正常后实际查询连接池并获得 waiting_requests=24；",
              "upstream 应识别异常，不把新增条件步骤变成必做动作；该偏差仍单独判定。",
              "restored-pool 核对来源保存的旧内容与实际读取，不替换为仓库当前 seed；继续查连接池仅记为观察，不是恢复失败或行为通过。",
              "查询执行核对完整快照回执、工具调用与最终回答的消息一致性；不评价最终回答的全部业务判断。",
              "条件顺序以模型发出调用的轮次和此前收到的回执为准，不使用并发工具完成顺序。",
              "各任务的 input.json 保存实际 system/user 提示，result.json 的 task_protocol 标明执行约定版本。",
              "同步和读取不等于新版带来收益，也不保证恢复后动作减少。实际记录保持原样。", "", "## 文件与历史", "",
              "- [来源与原始文件哈希](source.json)", "- [完整观察结果](observation.json)",
              "- [原生演化历史](evolve_history.jsonl)", "- [发布时的 Skill 内容](observed-published/timeout-triage/SKILL.md)",
              "- [原始旧内容](seed/timeout-triage/SKILL.md)",
              "- [接收端当前内容](receiver/skills/timeout-triage/SKILL.md)", "",
              "未执行到的阶段可能没有对应文件。恢复重新发布旧内容，不撤销历史调用，也不使用 restore hermes。"]
    (run / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, type=Path, help="第21讲 direct/validated 模式的 run 目录（只读）")
    parser.add_argument('--sync-mode', choices=('poll', 'manual'), default='poll')
    args = parser.parse_args(argv)
    source = args.source_run.expanduser().resolve()
    run = None
    report = {"observation_schema": "skillclaw-lesson22-v3", "status": "stopped",
              "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        run, before = copy_source(source)
        print(f"运行目录：{run}", flush=True)
        run_lesson(run, report, args.sync_mode)
    except (KeyboardInterrupt, SystemExit):
        report.update(status="interrupted", message="运行已中断，保留已完成记录；不会自动重试。")
    except Exception as exc:
        report["message"] = str(exc)
    finally:
        if run is not None:
            try:
                report['source_unchanged'] = fingerprints(source, source_parts(source)) == before
            except Exception:
                report['source_unchanged'] = False
            if not report['source_unchanged']:
                report.update(status='stopped', message='来源文件在运行期间变化，不能声明来源版本保持不变。')
            write_report(run, report)
            print(f"报告：{run / 'REPORT.md'}", flush=True)
        print(report.get("message", ""), flush=True)
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    install_signal_handlers()
    raise SystemExit(main())
