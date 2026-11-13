"""Generate a SkillClaw candidate in isolation and distribute approved bytes."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

from hermes_runtime import ONCALL_SKILL, ROOT


STAGING_GROUP = "patrol-evolution"
FORMAL_GROUP = "patrol-formal"
REPEAT_PROBE = "首次探测返回 5xx 时，再调用一次 oncall_probe"
ENGINEERING_MARKER = "\n\nCOURSE_ENGINEERING_ANALYSIS\n"


def _hub(shared: Path, group: str, alias: str):
    from skillclaw.skill_hub import SkillHub

    return SkillHub(
        backend="local", endpoint="", bucket="", access_key_id="", secret_access_key="",
        local_root=str(shared), group_id=group, user_alias=alias,
    )


def _push(shared: Path, group: str, source: Path, expected_sha: str) -> dict:
    publisher = _hub(shared, group, "course-publisher")
    official = shared / group / "skills" / ONCALL_SKILL / "SKILL.md"
    record = next((entry for entry in publisher.list_remote() if entry["name"] == ONCALL_SKILL), {})
    repaired = record.get("sha256") == expected_sha and (
        not official.is_file() or sha256(official.read_bytes()).hexdigest() != expected_sha
    )
    if repaired:
        # A native push can be interrupted after writing the file but before its
        # manifest. The approved snapshot restores the bytes the manifest names.
        content = (source / ONCALL_SKILL / "SKILL.md").read_bytes()
        if sha256(content).hexdigest() != expected_sha:
            raise RuntimeError("修复文件与批准哈希不一致。")
        publisher._bucket.put_object(publisher._skill_key(ONCALL_SKILL), content)
    result = publisher.push_skills(str(source))
    record = next((entry for entry in publisher.list_remote() if entry["name"] == ONCALL_SKILL), {})
    if not official.is_file() or sha256(official.read_bytes()).hexdigest() != expected_sha:
        raise RuntimeError("SkillHub 正式文件的内容与批准哈希不一致。")
    if record.get("sha256") != expected_sha:
        raise RuntimeError("SkillHub 共享清单未记录批准哈希。")
    return {"push": result, "manifest": record, "repaired_entrypoint": repaired}


def _snapshot(root: Path, content: bytes) -> tuple[Path, str]:
    digest = sha256(content).hexdigest()
    source = root / "versions" / digest
    entry = source / ONCALL_SKILL / "SKILL.md"
    if not entry.exists():
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(content)
    if entry.read_bytes() != content:
        raise RuntimeError("内容快照与版本哈希不一致。")
    return source, digest


def _stage_sessions(root: Path, archives: list[Path], baseline_sha: str) -> list[dict]:
    queue = root / "shared" / STAGING_GROUP / "sessions"
    queue.mkdir(parents=True)
    sources = []
    ids = set()
    for path in archives:
        content = path.read_bytes()
        archive = json.loads(content)
        session_id = archive["session_id"]
        alias = archive.get("user_alias")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id) or session_id in ids or not alias:
            raise RuntimeError("演化队列必须包含有实例归属的独立原生会话。")
        ids.add(session_id)
        read_hashes = set()
        for turn in archive["turns"]:
            for receipt in turn.get("tool_results_raw", []):
                if receipt.get("name") == "skill_view":
                    body = json.loads(receipt["content"])
                    if body.get("success") and body.get("name") == ONCALL_SKILL:
                        actual = sha256(body["content"].encode("utf-8")).hexdigest()
                        if actual != body.get("sha256"):
                            raise RuntimeError("归档中的 Skill 内容与读取回执哈希不一致。")
                        read_hashes.add(actual)
        if read_hashes != {baseline_sha}:
            raise RuntimeError(f"归档 {session_id} 未证明只读取本轮正式 Skill。")
        (queue / f"{session_id}.json").write_bytes(content)
        sources.append({
            "instance": alias, "session_id": session_id, "archive": str(path),
            "archive_sha256": sha256(content).hexdigest(), "skill_sha256": baseline_sha,
        })
    if len({source["instance"] for source in sources}) < 2:
        raise RuntimeError("共享演化至少需要两个独立巡检实例的会话。")
    return sources


def _scripted_reply(messages: list[dict], baseline: dict, source_ids: set[str]) -> str:
    """Supply deterministic model outputs while native code controls the workflow."""
    system, user = (message["content"] for message in messages[:2])
    if "produce a trajectory-aware analytical summary" in system:
        payload = json.loads(user)
        if payload["session_id"] not in source_ids:
            raise RuntimeError("收到未登记的摘要会话。")
        probes = []
        for interaction in payload["interactions"]:
            for result in interaction.get("tool_results", []):
                if result["tool_name"] == "oncall_probe":
                    match = re.search(r'"http_status"\s*:\s*(\d{3})', result["content"])
                    if match:
                        probes.append(int(match.group(1)))
        return f"会话 {payload['session_id']} 处理巡检告警。HTTP 探测实际返回 {probes}。一次探测只能证明该请求的结果。"
    if "edit the ORIGINAL skill" not in system or not all(f"### Session {sid}" in user for sid in source_ids):
        raise RuntimeError("原生执行阶段未携带所有巡检来源。")

    body = baseline["content"]
    has_5xx = re.search(r'oncall_probe\([^\n]*"http_status"\s*:\s*5\d{2}', user)
    _, marker, encoded = user.partition(ENGINEERING_MARKER)
    if marker:
        guidance = json.loads(encoded)
        cases = guidance["cases"]
        if {case["source_session_id"] for case in cases} != source_ids:
            raise RuntimeError("工程师分析没有对应本轮原始初判会话。")
        has_5xx = has_5xx and any(case["method_recommendation"]["action"] == "add_bounded_second_probe"
                                 for case in cases)
    if REPEAT_PROBE in body or not has_5xx:
        return json.dumps({"action": "skip", "rationale": "已覆盖二次探测，或本批缺少支持修订的 5xx 回执。"}, ensure_ascii=False)
    body = body.replace("assessment 在单次 5xx 时用 failure_observed，在首次探测为 2xx 时用 not_reproduced。", "")
    addition = (
        f"{REPEAT_PROBE}。\n"
        "保留 probe_http_status 为首次状态，probe_http_statuses 依序列出全部状态。"
        "assessment 只评估当前探测。两次 5xx 为 persistent，先 5xx 后 2xx 为 intermittent，"
        "首次 2xx 时结束探测并填 not_reproduced。回执缺失或不符合以上组合时保留未知。"
        "恢复仍由后续巡检独立验证。"
    )
    return json.dumps({
        "action": "improve_skill",
        "rationale": "同批巡检会话保留了真实探测回执。为首个 5xx 增加一次有界复核，区分持续与间歇失败。",
        "skill": {**baseline, "content": f"{body.rstrip()}\n\n## 异常探测复核\n\n{addition}"},
    }, ensure_ascii=False)


def evolve(
    root: Path, baseline: Path, source_sessions: list[Path], connection: dict | None = None,
    *, engineering_analysis: dict | None = None,
) -> tuple[Path | None, dict]:
    """Run SkillClaw once with staging-only validation jobs."""
    from evolve_server.core.config import EvolveServerConfig
    from evolve_server.core.utils import parse_skill_content
    from evolve_server.engines.workflow import EvolveServer

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    content = baseline.read_bytes()
    source, baseline_sha = _snapshot(root, content)
    baseline_skill = parse_skill_content(ONCALL_SKILL, content.decode("utf-8"))
    archives = _stage_sessions(root, source_sessions, baseline_sha)
    source_ids = {archive["session_id"] for archive in archives}
    guidance = None
    analysis_summary = None
    execution_requests = []
    if engineering_analysis is not None:
        from engineering_analysis import load_analysis

        guidance = load_analysis(engineering_analysis, baseline_sha, source_ids)
        analysis_summary = {
            "artifact": engineering_analysis["path"], "sha256": engineering_analysis["sha256"],
            "guidance_sha256": engineering_analysis["guidance_sha256"],
            "policy_id": guidance["policy"]["id"], "policy_kind": guidance["policy"]["kind"],
            "policy_author": guidance["policy"]["author"], "source_ids": sorted(source_ids),
            "branch_sources": guidance["cases"],
        }

    shared = root / "shared"
    queue_manifest = root / "queue-inputs.json"
    queued = []
    for record in archives:
        path = shared / STAGING_GROUP / "sessions" / f"{record['session_id']}.json"
        raw = path.read_bytes()
        if json.loads(raw)["session_id"] != record["session_id"]:
            raise ValueError("Native evolution queue differs from its recorded initial sessions")
        queued.append({"session_id": record["session_id"], "instance": record["instance"],
                       "sha256": sha256(raw).hexdigest()})
    queue_manifest.write_text(json.dumps(queued, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _push(shared, STAGING_GROUP, source, baseline_sha)
    scripted = connection is None
    options = connection or {"base_url": "http://127.0.0.1:1/v1", "api_key": "local-script", "model": "patrol-script"}
    engine = EvolveServer(EvolveServerConfig(
        storage_backend="local", local_root=str(shared), group_id=STAGING_GROUP,
        llm_api_key=options["api_key"], llm_base_url=options["base_url"], llm_model=options["model"],
        llm_max_tokens=6000, llm_temperature=0, publish_mode="validated",
        use_session_judge=False, use_skill_verifier=False, skill_reload_mode="off",
        history_path=str(root / "evolve_history.jsonl"), processed_log_path=str(root / "processed.json"),
        debug_dump_dir=str(root / "debug"),
    ))
    engine._llm._client = engine._llm._client.with_options(timeout=120, max_retries=0)
    original_chat = engine._llm.chat

    async def chat(messages, **kwargs):
        kwargs["max_tokens"] = min(kwargs.get("max_tokens", 6000), 6000)
        if "Return EXACTLY one JSON object" in messages[0]["content"]:
            kwargs["response_format"] = {"type": "json_object"}
        execution = "edit the ORIGINAL skill" in messages[0]["content"]
        request_record = None
        if execution:
            from engineering_analysis import digest

            messages = [dict(message) for message in messages]
            if guidance is not None:
                user = next(message for message in messages if message["role"] == "user")
                user["content"] += ENGINEERING_MARKER + json.dumps(guidance, ensure_ascii=False, sort_keys=True)
            path = root / f"execution-request-{len(execution_requests) + 1:04d}.json"
            payload = {"messages": messages, "parameters": kwargs,
                       "engineering_analysis_sha256": engineering_analysis["guidance_sha256"] if guidance else None}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            request_record = {"path": str(path), "messages_sha256": digest(messages),
                              "engineering_analysis_sha256": payload["engineering_analysis_sha256"], "status": "requested"}
            execution_requests.append(request_record)
        response = (_scripted_reply(messages, baseline_skill, source_ids) if scripted
                    else await original_chat(messages, **kwargs))
        if not response.strip():
            raise RuntimeError("SkillClaw 模型响应为空。")
        if request_record is not None:
            request_record.update(status="completed", response_sha256=sha256(response.encode()).hexdigest())
        return response

    engine._llm.chat = chat
    sys.path.insert(0, str(ROOT / "examples"))
    try:
        from skillclaw_lab.compat import strict_evolution_result

        with strict_evolution_result() as compatibility:
            native = asyncio.run(engine.run_once())
    finally:
        engine._llm._client.close()
        sys.path.remove(str(ROOT / "examples"))

    summary = {
        "model": options["model"], "scripted_model": scripted, "sources": archives,
        "baseline_sha256": baseline_sha, "native_workflow": native,
        "compatibility": compatibility, "status": "keep", "execution_requests": execution_requests,
        "queue_manifest": {"path": str(queue_manifest), "sha256": sha256(queue_manifest.read_bytes()).hexdigest(),
                           "session_ids": sorted(source_ids)},
    }
    if analysis_summary is not None:
        summary["engineering_analysis"] = analysis_summary
    candidate = None
    records = native["evolutions"]
    staging = shared / STAGING_GROUP
    if (native["had_processing_error"] or native["sessions"] != len(archives)
            or native["skill_groups"] != 1 or native["no_skill_sessions"]
            or native["uploaded_skills"] or len(records) > 1):
        summary["status"] = "failed"
    elif records:
        record = records[0]
        if (record["action"] != "queued_for_validation" or record["skill_name"] != ONCALL_SKILL
                or set(record["session_ids"]) != source_ids):
            summary["status"] = "failed"
        else:
            candidate = staging / "candidate_skills" / record["validation_job_id"] / "SKILL.md"
            summary.update(
                status="candidate", candidate=str(candidate),
                candidate_sha256=sha256(candidate.read_bytes()).hexdigest(),
            )
    if guidance is not None and records and not any(
        request["status"] == "completed" and request["engineering_analysis_sha256"] == engineering_analysis["guidance_sha256"]
        for request in execution_requests
    ):
        summary["status"] = "failed"
    if sha256((staging / "skills" / ONCALL_SKILL / "SKILL.md").read_bytes()).hexdigest() != baseline_sha:
        summary["status"] = "failed"
    (root / "evolution.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if summary["status"] == "failed":
        raise RuntimeError(f"原生演化未确认共享候选或保留决定。见 {root / 'evolution.json'}")
    return candidate, summary


def release(root: Path, skill: Path, approved_sha256: str, receivers: dict[str, Path],
            *, restoring=False) -> dict:
    """Publish exact approved bytes, then pull via each instance's native client."""
    content = skill.read_bytes()
    if sha256(content).hexdigest() != approved_sha256:
        raise ValueError("待发布 Skill 的内容与业务验收批准哈希不一致。")
    entries = {alias: directory / ONCALL_SKILL / "SKILL.md" for alias, directory in receivers.items()}
    if not restoring:
        for entry in entries.values():
            entry.parent.mkdir(parents=True, exist_ok=True)
            if entry.exists() and not entry.is_file():
                raise ValueError(f"SkillHub 接收路径不是文件：{entry}")
    source, _ = _snapshot(root, content)
    shared = root / "shared"
    published = _push(shared, FORMAL_GROUP, source, approved_sha256)
    pulls, errors = {}, {}
    for alias, directory in receivers.items():
        entry = entries[alias]
        try:
            result = _hub(shared, FORMAL_GROUP, alias).pull_skills(
                str(directory), mirror=False, include_names=[ONCALL_SKILL],
            )
            if not entry.is_file() or entry.read_bytes() != content:
                raise RuntimeError(f"{alias} 未从正式 SkillHub 同步到批准版本。")
            pulls[alias] = {"pull": result, "skill": str(entry), "sha256": approved_sha256}
        except Exception as exc:
            if not restoring:
                raise
            errors[alias] = str(exc)
    return {"sha256": approved_sha256, **published, "receivers": pulls, "receiver_errors": errors}
