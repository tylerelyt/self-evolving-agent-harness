"""离线检查，只生成临时文件和轨迹样例，不生成验证任务或通过结果。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import run22
from skillclaw_lab.test_observations import sample_archive, sample_task


class LessonChecks(unittest.TestCase):
    def setUp(self):
        root = run22.HERE / "runtime"
        if root.is_symlink():
            raise RuntimeError("离线检查不允许 runtime 指向其他目录。")
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="offline-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "source"
        self.run.mkdir()
        self.manifest = {"format": "skillclaw-course-v1", "group_id": "offline-team", "publish_mode": "validated"}
        run22.save(self.run / "run.json", self.manifest)
        self.skill = self.run / "seed/timeout-triage/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("---\nname: timeout-triage\ndescription: 离线同步样例\n---\n只读健康和上游。\n")
        (self.run / "shared/offline-team").mkdir(parents=True)
        (self.run / "evolve_history.jsonl").write_text("\n")

    def test_direct_source_uses_published_files_without_validation(self):
        self.manifest["publish_mode"] = "direct"
        run22.save(self.run / "run.json", self.manifest)
        run22.hub(self.run).push_skills(str(self.run / 'seed'))
        publisher = self.run / 'publisher/timeout-triage/SKILL.md'
        publisher.parent.mkdir(parents=True)
        publisher.write_text(self.skill.read_text() + '离线构造的新版，用于同步测试。\n')
        run22.hub(self.run).push_skills(str(publisher.parent.parent))
        run22.save(self.run / 'result.json', {
            'publish_mode': 'direct', 'shared_content_changed': True,
            'seed_sha256': run22.sha(self.skill), 'shared_sha256': run22.sha(publisher),
            'evolution': {'skills_evolved': 1, 'had_processing_error': False}})
        with patch.object(run22, 'HERE', self.root):
            copied, _ = run22.copy_source(self.run)
        report = {}
        with patch.object(run22, 'validate_once', side_effect=AssertionError('direct 不应验证')), \
                patch.object(run22, 'evolve_once', side_effect=AssertionError('direct 不应再演化')):
            run22.prepare_publication(copied, report)
        self.assertEqual(report['published']['sha256'], run22.sha(publisher))
        remote = copied / 'shared/offline-team/skills/timeout-triage/SKILL.md'
        remote.write_text(self.skill.read_text())
        with self.assertRaisesRegex(run22.StopRun, '正式内容'):
            run22.prepare_publication(copied, {})

    def test_pending_sessions_stop(self):
        run22.save(self.run / "shared/offline-team/sessions/source.json", {"session_id": "offline"})
        with self.assertRaisesRegex(run22.StopRun, "待演化会话"):
            run22.source_parts(self.run)

    def test_conflicted_source_archive_stops_before_validation(self):
        _, messages, _ = sample_task()
        archive = sample_archive(messages)
        archive['turns'][0]['tool_results'][0].update(has_error=True, error_type='timeout')
        run22.save(self.run / 'collected/offline.json', archive)
        with self.assertRaisesRegex(run22.StopRun, '摘要与成功回执冲突'):
            run22.copy_source(self.run)

    def test_source_symlink_and_unsafe_group_stop(self):
        (self.run / "seed/link").symlink_to(self.root)
        with self.assertRaisesRegex(run22.StopRun, "软链接"):
            run22.source_parts(self.run)
        for name in ("../outside", "/tmp/outside", "..", "team/other", "a\\b"):
            with self.subTest(name=name), self.assertRaises(run22.StopRun):
                run22.component(name)

    def test_copy_keeps_history_and_original(self):
        run22.save(self.run / "processed.json", {"offline-history": True})
        with patch.object(run22, "HERE", self.root):
            copied, before = run22.copy_source(self.run)
        self.assertNotEqual(copied, self.run)
        self.assertEqual(before, run22.fingerprints(copied, run22.source_parts(copied)))
        self.assertEqual(before, run22.fingerprints(self.run, run22.source_parts(self.run)))
        self.assertEqual(json.loads((copied / "run.json").read_text()), self.manifest)

    def test_no_candidate_stops_without_model(self):
        store = run22.validation_store(self.run)
        with self.assertRaisesRegex(run22.StopRun, "没有待验证候选"):
            run22.pending_job(store)
        self.assertEqual(store.list_jobs(), [])

    def test_validation_store_first_import_in_fresh_process(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", "from pathlib import Path; import run22; "
             "assert run22.validation_store(Path(__import__('sys').argv[1])).list_jobs() == []", str(self.run)],
            cwd=run22.HERE, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_worker_empty_queue_and_glm_connection(self):
        from skillclaw.validation_worker import ValidationWorker
        env = {"GLM_API_KEY": "offline-no-request", "GLM_BASE_URL": "http://127.0.0.1:9/v1",
               "GLM_MODEL": "offline-model"}
        with patch.dict(os.environ, env):
            config = run22.make_config(self.run, "offline", self.run / "validator/skills")
            self.assertEqual(config.llm_api_base, config.prm_url)
            self.assertEqual(config.llm_api_key, config.prm_api_key)
            self.assertEqual(config.llm_model_id, config.prm_model)
            config.validation_enabled = True
            worker = ValidationWorker(config)
            try:
                with patch.object(worker._client, "chat", side_effect=AssertionError("不允许模型调用")):
                    summary = asyncio.run(worker.run_once(force=True))
                self.assertEqual(summary["validated_jobs"], 0)
                self.assertEqual(summary["reason"], "no open validation jobs")
            finally:
                worker._client._client.close()
                worker._prm_scorer._client.close()

    def test_native_push_pull_and_restore_in_isolated_root(self):
        shared = run22.hub(self.run)
        original_sha = run22.sha(self.skill)
        shared.push_skills(str(self.run / "seed"))
        receiver = self.run / "receiver/skills/timeout-triage/SKILL.md"
        run22.pull_receiver(self.run, original_sha)
        publisher = self.run / "offline-new/timeout-triage/SKILL.md"
        publisher.parent.mkdir(parents=True)
        publisher.write_text(self.skill.read_text() + "离线同步样例新增一行，不是模型演化结果。\n")
        shared.push_skills(str(publisher.parent.parent))
        run22.pull_receiver(self.run, run22.sha(publisher))
        self.assertEqual(run22.sha(receiver), run22.sha(publisher))
        shared.push_skills(str(self.run / "seed"))
        run22.pull_receiver(self.run, original_sha)
        self.assertEqual(run22.sha(receiver), original_sha)
        self.assertEqual(run22.sha(self.skill), original_sha)
        versions = shared.list_remote()
        self.assertEqual(versions[0]["version"], 3)

    def test_behavior_needs_real_receipt_scope_and_skill_view(self):
        # 仅用于检查判定函数的合成轨迹，不落为 Hermes 或 ValidationWorker 结果。
        sample, messages, digest = sample_task(batches=[['query_service_health', 'query_upstream_latency']])
        observe = lambda value, case="pool", **kw: run22.task_observation(value, digest, case, messages=messages, **kw)
        self.assertEqual(observe(sample)["behavior_status"], "mismatch")
        sample, messages, digest = sample_task()
        self.assertEqual(observe(sample)["behavior_status"], "matched")
        wrong = deepcopy(sample)
        wrong["native_skill_view_verified"] = False
        self.assertFalse(observe(wrong)["loading_verified"])
        self.assertEqual(observe(wrong)["behavior_status"], "not_assessable")
        wrong = deepcopy(sample)
        wrong["calls"][-1]["result"]["waiting_requests"] = 23
        self.assertEqual(observe(wrong)["behavior_status"], "not_assessable")
        wrong = deepcopy(sample)
        wrong["calls"][-1]["arguments"]["region"] = "wrong"
        self.assertFalse(observe(wrong)["execution_verified"])
        self.assertEqual(observe(wrong)["behavior_status"], "not_assessable")
        # 同批调用即使 handler 完成顺序正常，也不能冒充条件分支。
        wrong, messages, _ = sample_task(batches=[['query_service_health', 'query_upstream_latency', 'query_connection_pool']])
        self.assertEqual(observe(wrong)["behavior_status"], "mismatch")
        upstream, messages, _ = sample_task(case='upstream')
        self.assertEqual(observe(upstream, "upstream")["behavior_status"], "mismatch")
        upstream, messages, _ = sample_task(case='upstream', batches=[['query_service_health', 'query_upstream_latency']])
        self.assertEqual(observe(upstream, "upstream")["behavior_status"], "matched")
        upstream, messages, _ = sample_task(case='upstream', batches=[
            ['query_service_health', 'query_upstream_latency'], ['query_connection_leases']])
        self.assertEqual(observe(upstream, "upstream")["behavior_status"], "mismatch")
        sample, messages, _ = sample_task()
        restored = observe(sample, restored=True)
        self.assertTrue(restored["loading_verified"])
        self.assertTrue(restored["execution_verified"])
        self.assertFalse(restored["observations"]["no_connection_pool_call"])
        self.assertEqual(restored["behavior_checks"], {})
        self.assertEqual(restored["behavior_status"], "observation_only")
        sample, messages, _ = sample_task(batches=[['query_service_health', 'query_upstream_latency']])
        self.assertEqual(observe(sample, restored=True)["behavior_status"], "observation_only")
        self.assertFalse(run22.task_observation(sample, digest, 'pool')['execution_verified'])

    def test_native_poll_sync_and_shutdown_without_http_server(self):
        run22.hub(self.run).push_skills(str(self.run / 'seed'))
        env = {'GLM_API_KEY': 'offline-no-request', 'GLM_BASE_URL': 'http://127.0.0.1:9/v1'}
        with patch.dict(os.environ, env):
            result = run22.pull_receiver(self.run, run22.sha(self.skill), mode='poll')
            self.assertTrue(result['storage_verified'])
            self.assertEqual(result['mode'], 'poll')
            self.assertGreaterEqual(result['pull']['completed_polls'], 2)
            self.assertTrue(result['pull']['no_model_messages_sent'])
            # 限时未同步不能返回成功，轮询任务必须在 finally 中清理。
            with self.assertRaises(TimeoutError):
                asyncio.run(run22.wait_native_poll(self.run, 'missing-version', timeout=0.1))

    def test_next_poll_observes_update_without_any_model_message(self):
        from skillclaw.api_server import SkillClawAPIServer
        store = run22.hub(self.run)
        store.push_skills(str(self.run / 'seed'))
        publisher = self.run / 'publisher/timeout-triage/SKILL.md'
        publisher.parent.mkdir(parents=True)
        publisher.write_text(self.skill.read_text() + '第二次轮询的新内容，仅离线构造。\n')
        env = {'GLM_API_KEY': 'offline-no-request', 'GLM_BASE_URL': 'http://127.0.0.1:9/v1'}
        with patch.dict(os.environ, env):
            config = run22.make_config(self.run, 'offline-poll', self.run / 'receiver/skills')
        config.sharing_skill_reload_mode = 'poll'
        config.sharing_skill_reload_interval_seconds = 5
        config.record_enabled = False
        proxy = SkillClawAPIServer(config)

        async def check():
            receiver = self.run / 'receiver/skills/timeout-triage/SKILL.md'
            proxy._start_skill_reload_polling()
            task = proxy._skill_reload_task
            try:
                async with asyncio.timeout(10):
                    while not receiver.is_file():
                        await asyncio.sleep(0.02)
                    self.assertEqual(run22.sha(receiver), run22.sha(self.skill))
                    store.push_skills(str(publisher.parent.parent))
                    while run22.sha(receiver) != run22.sha(publisher):
                        await asyncio.sleep(0.05)
                    self.assertFalse(task.done())
            finally:
                await proxy._shutdown_cleanup()
            self.assertTrue(task.done())
            self.assertIsNone(proxy._skill_reload_task)
        asyncio.run(check())

    def test_native_readonly_skill_toolset_excludes_write_tool(self):
        from toolsets import get_toolset
        from skillclaw_lab.runtime import readonly_skill_toolset
        tools = get_toolset(readonly_skill_toolset())['tools']
        self.assertNotIn('skill_manage', tools)
        self.assertIn('skill_view', tools)

    def test_summary_keeps_upstream_deviation_and_loading_failures_separate(self):
        task = {"loading_verified": True, "execution_verified": True, "behavior_status": "matched"}
        report = {"receiver_pull": {"storage_verified": True}, "restore_pull": {"storage_verified": True},
                  "independent_sessions": True,
                  "tasks": {"pool": dict(task), "upstream": dict(task), "restored-pool": dict(task)}}
        report["tasks"]["restored-pool"]["behavior_status"] = "observation_only"
        report["tasks"]["upstream"]["behavior_status"] = "mismatch"
        outcome = run22.summarize_outcomes(report)
        self.assertEqual(outcome["status"], "behavior_mismatch")
        self.assertTrue(outcome["storage"]["old_content_restored"])
        self.assertTrue(outcome["loading"]["restored-pool"])
        self.assertEqual(outcome["behavior"]["restored-pool"], "observation_only")
        report["tasks"]["restored-pool"]["loading_verified"] = False
        outcome = run22.summarize_outcomes(report)
        self.assertEqual(outcome["status"], "verification_failed")
        self.assertTrue(outcome["storage"]["old_content_restored"])
        self.assertFalse(outcome["loading"]["restored-pool"])
        self.assertEqual(outcome["behavior"]["upstream"], "mismatch")
        report["restore_pull"]["storage_verified"] = False
        self.assertFalse(run22.summarize_outcomes(report)["storage"]["old_content_restored"])


if __name__ == "__main__":
    with patch('socket.socket.connect', side_effect=AssertionError('离线测试禁止网络连接')):
        unittest.main(verbosity=2)
