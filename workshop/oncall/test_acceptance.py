"""验收器回归：接受不同的真实记忆表述，拒绝加载错误或被改写的快照。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import acceptance


class SnapshotAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = '查询日志时先 services describe 取得 log_entry，再用 logs read。'
        self.new = '查询日志时先按服务、区域、环境查 catalog，再读取返回入口。'
        self.sessions = {
            'migration': {'memory_at_start': self.old, 'memory_snapshot': self.old, 'cached_prompt': self.old},
            'fresh': {'history_at_start': 0, 'memory_at_start': self.new, 'agent_instance': 'fresh-agent'},
            'discovery': {},
        }
        self.sessions['migration']['agent_instance'] = 'old-agent'
        self.tasks = [
            {'id': f't{i}', 'created_at': f'{i}0', 'started_at': f'{i}0', 'ended_at': f'{i}9',
             'session': session, 'message': '巡检或调查告警', 'status': 'completed', 'calls': []}
            for i, session in enumerate(('discovery', 'migration', 'fresh'), 1)]
        self.reviews = [
            {'review_id': 'r1', 'task_id': 't1', 'thread': 'bg-review', 'daemon': True, 'status': 'completed',
             'memory_after': self.old, 'calls': [{'name': 'memory', 'args': {'action': 'add'}, 'receipt': {'success': True}}]},
            {'review_id': 'r2', 'task_id': 't2', 'thread': 'bg-review', 'daemon': True, 'status': 'completed',
             'memory_after': self.new, 'calls': [{'name': 'memory', 'args': {'action': 'replace', 'old_text': self.old}, 'receipt': {'success': True}}]},
        ]
        self.queries = [
            {'at': '11', 'path': '/services', 'params': {}, 'code': 200},
            {'at': '12', 'path': '/logs', 'params': {'entry': 'payment-api-prod'}, 'code': 200, 'count': 3},
            {'at': '21', 'path': '/catalog', 'params': {}, 'code': 200},
            {'at': '22', 'path': '/logs', 'params': {}, 'code': 200, 'count': 3},
            {'at': '31', 'path': '/catalog', 'params': {'region': 'eu-west', 'env': 'staging'}, 'code': 200},
            {'at': '32', 'path': '/logs', 'params': {'entry': 'logs://payment-api/eu-west/staging/v2'}, 'code': 200, 'count': 3},
        ]

    def save(self, relative, value):
        file = self.root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(value, ensure_ascii=False))

    def run_report(self):
        for task in self.tasks:
            self.save('tasks/' + task['id'] + '.json', task)
        for review in self.reviews:
            self.save('reviews/' + review['review_id'] + '.json', review)
        for sid, session in self.sessions.items():
            self.save('sessions/' + sid + '-final.json', session)
        self.save('environment-regional.json', {})
        self.save('migration-check.json', {'old_entry_http_410': True, 'memory_unchanged': True, 'same_oncall_pid': True})
        (self.root / 'business-queries.jsonl').write_text('\n'.join(json.dumps(q) for q in self.queries))
        with patch.object(acceptance, 'observations', return_value=self.root), \
             patch.object(acceptance, 'runtime', return_value=self.root), \
             patch.object(acceptance, 'api', side_effect=OSError('离线测试')):
            acceptance.report([])
        return json.loads((self.root / 'acceptance.json').read_text())

    def test_generic_legacy_rule_is_accepted(self):
        self.assertNotIn('payment-api-prod', self.old)
        self.assertTrue(self.run_report()['complete'])

    def test_unrelated_new_memory_is_rejected(self):
        self.sessions['fresh']['memory_at_start'] = '不相干的另一条 catalog 记忆'
        result = self.run_report()
        self.assertFalse(result['complete'])
        self.assertNotEqual(next(c['status'] for c in result['checks'] if c['item'] == '新会话是否加载'), '通过')

    def test_unsuccessful_initial_memory_write_is_rejected(self):
        self.reviews[0]['calls'][0]['receipt']['success'] = False
        self.assertFalse(self.run_report()['complete'])

    def test_refreshed_old_session_cannot_count_as_frozen(self):
        self.sessions['migration']['memory_snapshot'] = self.new
        self.assertFalse(self.run_report()['complete'])


if __name__ == '__main__':
    unittest.main()
