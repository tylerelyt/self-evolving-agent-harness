"""不调用模型，检查共享范围、版本恢复和客户端验证开关。"""
import asyncio
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skillclaw_lab.runtime import connection, hub, make_config, save, sha, stop_process_group
from skillclaw_lab.test_observations import ObservationTests
from skillclaw_lab.test_compat import CompatibilityTests
from skillclaw_lab.test_task_protocol import TaskProtocolTests


class SharedStorageTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / 'runtime'
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temporary.name)
        save(self.root / 'run.json', {'format': 'skillclaw-course-v1', 'group_id': 'test-only',
                                     'publish_mode': 'validated'})

    def tearDown(self):
        self.temporary.cleanup()

    def test_native_pull_and_restore_content_as_new_version(self):
        source = self.root / 'seed/timeout-triage/SKILL.md'
        source.parent.mkdir(parents=True)
        source.write_text('---\nname: timeout-triage\ndescription: 离线单测\n---\n旧内容\n')
        first = source.read_bytes()
        store = hub(self.root)
        store.push_skills(str(source.parents[1]))
        store.pull_skills(str(self.root / 'reader'), mirror=True)
        received = self.root / 'reader/timeout-triage/SKILL.md'
        self.assertEqual(sha(source), sha(received))
        source.write_text(source.read_text().replace('旧内容', '单测构造的新内容，不是模型改进'))
        store.push_skills(str(source.parents[1]))
        second_version = store.list_remote()[0]['version']
        store.pull_skills(str(self.root / 'reader'), mirror=True)
        self.assertNotEqual(first, received.read_bytes())
        source.write_bytes(first)
        store.push_skills(str(source.parents[1]))
        self.assertGreater(store.list_remote()[0]['version'], second_version)
        store.pull_skills(str(self.root / 'reader'), mirror=True)
        self.assertEqual(first, received.read_bytes())

    def test_groups_do_not_share_records(self):
        from skillclaw.skill_hub import SkillHub
        source = self.root / 'seed/private-method/SKILL.md'
        source.parent.mkdir(parents=True)
        source.write_text('---\nname: private-method\n---\n仅属于第一组\n')
        hub(self.root).push_skills(str(source.parents[1]))
        other = SkillHub(backend='local', endpoint='', bucket='', access_key_id='',
                         secret_access_key='', local_root=str(self.root / 'shared'),
                         group_id='another-group', user_alias='reader')
        self.assertEqual(len(hub(self.root).list_remote()), 1)
        self.assertEqual(other.list_remote(), [])

    def test_validator_requires_explicit_enable(self):
        from skillclaw.validation_worker import ValidationWorker
        with patch.dict(os.environ, {'GLM_API_KEY': 'offline-test-only',
                                    'GLM_BASE_URL': 'http://127.0.0.1:1/v1'}):
            config = make_config(self.root, 'validator', self.root / 'skills')
            self.assertTrue(config.sharing_upload_sessions)
            self.assertEqual(config.prm_url, config.llm_api_base)
            worker = ValidationWorker(config)
            try:
                result = asyncio.run(worker.run_once(force=True))
                self.assertEqual(result['validated_jobs'], 0)
                self.assertIn('disabled', result['reason'])
            finally:
                worker._client._client.close()
                worker._prm_scorer._client.close()

    def test_untrusted_connection_string_rejected(self):
        for endpoint in ('http://example.com/v1', 'https://key:secret@example.com/v1',
                         'https://example.com/v1?key=secret'):
            with patch.dict(os.environ, {'GLM_API_KEY': 'offline-test-only', 'GLM_BASE_URL': endpoint}):
                with self.assertRaises(ValueError):
                    connection()

    def test_process_group_is_reaped(self):
        process = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True)
        try:
            stop_process_group(process)
            self.assertIsNotNone(process.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        finally:
            if process.poll() is None:
                stop_process_group(process)


if __name__ == '__main__':
    with patch('socket.socket.connect', side_effect=AssertionError('离线测试禁止网络连接')):
        unittest.main(verbosity=2)
