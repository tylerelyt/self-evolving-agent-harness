"""第21讲收集与演化前检查，不启动代理或调用模型。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run21
from skillclaw_lab.runtime import save
from skillclaw_lab.test_observations import SKILL_TEXT, sample_archive, sample_task


class CollectionTests(unittest.TestCase):
    def setUp(self):
        root = run21.HERE / 'runtime'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save(self.root / 'run.json', {'format': 'skillclaw-course-v1', 'group_id': 'offline',
                                      'publish_mode': 'validated'})
        skill = self.root / 'seed/timeout-triage/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text(SKILL_TEXT)
        self.collection = []
        for alias in ('instance-a', 'instance-b'):
            result, messages, _ = sample_task()
            result['session_id'] = alias
            save(self.root / 'clients' / alias / 'task/result.json', result)
            save(self.root / 'clients' / alias / 'task/messages.json', messages)
            archive = sample_archive(messages, alias, alias)
            save(self.root / 'collected' / f'{alias}.json', archive)
            save(self.root / 'shared/offline/sessions' / f'{alias}.json', archive)
            self.collection.append({'alias': alias})

    def test_complete_two_source_collection(self):
        self.assertEqual(len(run21.verify_collection(self.root, self.collection)), 2)
        with self.assertRaisesRegex(RuntimeError, '两个'):
            run21.verify_collection(self.root, self.collection[:1])

    def test_permission_artifact_and_changed_queue_stop(self):
        path = self.root / 'clients/instance-a/task/result.json'
        result = json.loads(path.read_text())
        result['available_tools'] = result['available_tools'][:2]
        save(path, result)
        with self.assertRaisesRegex(RuntimeError, '同等工具权限'):
            run21.verify_collection(self.root, self.collection)
        result, _, _ = sample_task()
        result['session_id'] = 'instance-a'
        save(path, result)
        (self.root / 'shared/offline/sessions/instance-a.json').unlink()
        with self.assertRaisesRegex(RuntimeError, '已被消费'):
            run21.verify_collection(self.root, self.collection)

    def test_false_error_metadata_blocks_evolution(self):
        archive = self.root / 'collected/instance-a.json'
        record = json.loads(archive.read_text())
        record['turns'][0]['tool_results'][0].update(has_error=True, error_type='timeout')
        save(archive, record)
        save(self.root / 'shared/offline/sessions/instance-a.json', record)
        with self.assertRaisesRegex(RuntimeError, '冲突'):
            run21.verify_collection(self.root, self.collection)
        self.assertTrue((self.root / 'collection-checks.json').is_file())


if __name__ == '__main__':
    with patch('socket.socket.connect', side_effect=AssertionError('离线测试禁止网络连接')):
        unittest.main(verbosity=2)
