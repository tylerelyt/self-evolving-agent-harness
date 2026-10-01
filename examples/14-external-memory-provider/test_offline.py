"""零模型请求：真实 Hermes 方法配模拟 HTTP；SQLite 使用真实文件故障。"""
import copy
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import native_openviking as native
from openviking_config import build_config, checked_base

# 不加载任何个人 Home；所有文件均在本讲 runtime 下。
TEST_ROOT = native.HERE / 'runtime/offline-tests'
TEST_ROOT.mkdir(parents=True, exist_ok=True)
Manager, Provider = native.load_native(TEST_ROOT / 'empty-home')
from plugins.memory import openviking as upstream
import shared_store
from oncall_memory import OncallMemoryProvider


class NoNetwork(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('离线测试禁止真实联网'))
        self.guard.start()
        self.addCleanup(self.guard.stop)


class ConfigurationTests(NoNetwork):
    def test_official_and_budget_override(self):
        default = build_config({})
        self.assertEqual(default['vlm']['model'], 'glm-5.2')
        self.assertEqual(default['vlm']['max_tokens'], 8000)
        self.assertEqual(default['embedding']['dense']['model'], 'embedding-3')
        config = build_config({'GLM_BASE_URL': 'http://127.0.0.1:9000/v1',
                               'GLM_EMBEDDING_BASE_URL': 'http://127.0.0.1:9001/v1'})
        self.assertEqual(config['embedding']['dense']['api_base'], 'http://127.0.0.1:9001/v1')
        self.assertEqual(config['vlm']['api_key'], '${GLM_API_KEY}')
        self.assertTrue(config['server']['agent_evolution']['enabled'])
        self.assertIn('experiences', native.MEMORY_POLICY['memory_types'])
        with self.assertRaises(ValueError):
            checked_base('https://another-vendor.example/v1')
        with self.assertRaises(ValueError):
            checked_base('http://127.0.0.1:9001/v1/embeddings')

    def test_live_is_opt_in(self):
        with self.assertRaises(RuntimeError):
            native.NativeSession()

    def test_error_text_is_not_completed_answer(self):
        for status in ({'completed': False}, {'failed': True}, {'interrupted': True}, {'partial': True}):
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                native.require_completed({'completed': True, 'final_response': '返回了文本', **status})
        native.require_completed({'completed': True, 'final_response': '正常回答'})


class NativeTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'OPENVIKING_API_KEY': '', 'OPENVIKING_ENDPOINT': 'http://127.0.0.1:1933'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home = patch.object(native, 'HERE', Path(self.temp.name))
        self.home.start()
        self.addCleanup(self.home.stop)
        self.calls, self.messages = [], []
        self.fail_write = False
        def get(client, path, **kwargs):
            self.calls.append(('GET', path))
            if path == '/health':
                return {'status': 'ok', 'healthy': True, 'version': 'offline-fixture'}
            if path.endswith('/context'):
                return {'status': 'ok', 'result': {'messages': copy.deepcopy(self.messages)}}
            if '/sessions/' in path:
                return {'status': 'ok', 'result': {'pending_tokens': len(self.messages)}}
            raise OSError('构造读取失败')
        def post(client, path, data=None, **kwargs):
            self.calls.append(('POST', path, copy.deepcopy(data)))
            if '/messages' in path:
                if self.fail_write:
                    raise OSError('构造写入失败')
                self.messages.extend(data.get('messages', [data]))
            if '/search/' in path:
                raise OSError('构造读取失败')
            return {'status': 'ok', 'result': {}}
        for name, callback in [('get', get), ('post', post),
                               ('health_payload', lambda client: get(client, '/health'))]:
            item = patch.object(upstream._VikingClient, name, autospec=True, side_effect=callback)
            item.start()
            self.addCleanup(item.stop)

    def test_conversion_pairs_ids_and_excludes_recall(self):
        messages = native.demo_messages()
        # viking 回执故意不带 name，必须靠 tool_call_id 识别。
        payload = Provider._messages_to_openviking_batch(messages, assistant_peer_id='agent-A')
        parts = [p for m in payload for p in m['parts'] if p['type'] == 'tool']
        self.assertEqual([p['tool_id'] for p in parts], ['check-1'])
        self.assertEqual(parts[0]['tool_input'], {'instance': 'A'})
        self.assertIn('ingestion_paused', parts[0]['tool_output'])
        self.assertNotIn('旧检索内容', json.dumps(payload, ensure_ascii=False))
        self.assertTrue(all(m['peer_id'] == 'agent-A' for m in payload if m['role'] == 'assistant'))

    def test_sync_then_explicit_commit_and_shutdown(self):
        with native.NativeSession(allow_live=True) as session:
            self.assertIs(type(session.provider), Provider)
            report = session.sync(native.demo_messages())
            self.assertTrue(report['write_verified'])
            self.assertFalse(any('/commit' in c[1] for c in self.calls))
            session.end_session()
            self.assertEqual(sum('/commit' in c[1] for c in self.calls), 1)
            self.assertEqual(session.manager.prefetch_all('报表延迟怎么检查', session_id=session.sid), '')
            self.assertIn('experiences', self.calls[1][2]['memory_policy']['memory_types'])
        self.assertTrue(session.provider._shutting_down)
        self.assertIsNot(upstream._last_active_provider, session.provider)

    def test_failed_write_is_not_success_or_commit(self):
        self.fail_write = True
        with native.NativeSession(allow_live=True) as session:
            report = session.sync(native.demo_messages())
            self.assertFalse(report['write_verified'])
            with self.assertRaises(RuntimeError):
                session.end_session()
        self.assertFalse(any('/commit' in c[1] for c in self.calls))

    def test_cleanup_alone_never_commits(self):
        with native.NativeSession(allow_live=True) as session:
            session.sync(native.demo_messages())
        self.assertFalse(any('/commit' in c[1] for c in self.calls))

    def test_real_agent_initialization_loads_native_tools_without_model_call(self):
        with patch.dict(os.environ, {'GLM_API_KEY': 'offline-placeholder',
                                     'GLM_BASE_URL': 'http://127.0.0.1:9999/v1'}):
            with native.NativeSession(allow_live=True, agent_runtime=True) as session:
                self.assertIs(session.ai._memory_manager, session.manager)
                names = {tool['function']['name'] for tool in session.ai.tools}
                self.assertTrue({'viking_search', 'viking_read'} <= names)
        from hermes_logging import _reset_queued_handlers
        _reset_queued_handlers()
        self.assertFalse(any('/commit' in c[1] for c in self.calls))


class SQLiteTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'SESSION_LAB_RUNTIME': str(self.run)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.backend_dir = self.run / 'unavailable-backend'
        (self.run / 'business.json').write_text(json.dumps({'directory': str(self.backend_dir)}))
        with sqlite3.connect(self.run / 'oncall.sqlite3') as db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)')
            db.execute('INSERT INTO sessions VALUES (?,?)', ('A', json.dumps({'instance': 'A', 'scope': shared_store.SCOPE})))
        self.provider = OncallMemoryProvider()
        self.provider.initialize('A', hermes_home=str(self.run / 'hermes'))

    def test_real_unopenable_backend_persists_then_restart_retries(self):
        messages = [{'role': 'user', 'content': '报表延迟'}, {'role': 'assistant', 'content': '继续现场检查'}]
        with self.assertRaises(OSError):
            self.provider.sync_turn('报表延迟', '继续现场检查', messages=messages)
        queued = shared_store.pending()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['status'], 'failed')
        self.assertEqual(queued[0]['body']['raw']['messages'], messages)
        with sqlite3.connect(self.run / 'hermes/providers/outbox.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.backend_dir.mkdir()
        replacement = OncallMemoryProvider()
        replacement.initialize('A', hermes_home=str(self.run / 'hermes'))
        replacement.retry(force=True)
        replacement.retry(force=True)
        self.assertEqual(len(shared_store.rows()), 1)
        self.assertEqual(shared_store.pending()[0]['status'], 'raw_saved')
        self.assertEqual(shared_store.pending()[0]['body'], queued[0]['body'])
        with sqlite3.connect(shared_store.backend()) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        replacement.shutdown()

    def evidence(self):
        events = [('report', {'fresh': False, 'collected': True}), ('ingestion', {'paused': True}),
                  ('resume', {'status': 'completed'}), ('report', {'fresh': True})]
        return [{'tool_call_id': str(i), 'receipt': {'action': action, 'instance': 'A',
                 'scope': shared_store.SCOPE, 'version': 'v1', 'status': 'ok', **fields}}
                for i, (action, fields) in enumerate(events)]

    def test_cross_instance_and_reverse_time_never_make_success(self):
        evidence = self.evidence()
        self.assertIsNotNone(self.provider.extract_case('r', {}, evidence))
        mixed = copy.deepcopy(evidence)
        mixed[2]['receipt']['instance'] = 'B'
        self.assertIsNone(self.provider.extract_case('r', {}, mixed))
        self.assertIsNone(self.provider.extract_case('r', {}, [evidence[3], *evidence[:3]]))
        self.assertIsNone(self.provider.extract_case('r', {}, [evidence[0], evidence[2], evidence[1], evidence[3]]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
