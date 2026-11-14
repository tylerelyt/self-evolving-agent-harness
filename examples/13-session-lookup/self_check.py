#!/usr/bin/env python3
"""无模型反例自检。构造消息不是模型轨迹，不写 live-latest 或回放哈希。"""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent'))


def request(call_id, name='terminal', args=None, message_id=1):
    return {'id': message_id, 'role': 'assistant', 'content': '构造自检请求',
            'tool_calls': [{'id': call_id, 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args or {})}}]}


def receipt(call_id, payload, message_id=2):
    return {'id': message_id, 'role': 'tool', 'tool_call_id': call_id,
            'content': json.dumps(payload)}


class Checks(unittest.TestCase):
    def test_missing_wrong_duplicate_receipts_remain_unknown(self):
        from validation_checks import pair_messages, execution_state
        for messages in (
            [request('a')],
            [request('a'), receipt('b', {'status': 'completed'})],
            [request('a'), receipt('a', {'status': 'completed'}), receipt('a', {'status': 'failed'}, 3)],
            [request(None), receipt(None, {'status': 'completed'})],
        ):
            self.assertEqual(execution_state(pair_messages(messages)[0]), 'unknown')

    def test_accepted_denied_and_completed_are_distinct(self):
        from validation_checks import pair_messages, execution_state
        for state in ('accepted', 'denied', 'completed'):
            pair = pair_messages([request('a'), receipt('a', {'status': state})])[0]
            self.assertEqual(execution_state(pair), state)
        messages = [request('a'), receipt('a', {'output': json.dumps({'status': 'completed'}), 'exit_code': 1})]
        self.assertEqual(execution_state(pair_messages(messages)[0]), 'failed')
        for payload, expected in (
            ({'success': True, 'staged': True, 'pending_id': 'approval-1'}, 'accepted'),
            ({'success': True}, 'unknown'),
            ({'success': True, 'done': True}, 'completed'),
        ):
            messages = [request('m', 'memory'), receipt('m', payload)]
            self.assertEqual(execution_state(pair_messages(messages)[0]), expected)

    def test_native_memory_receipt_discovery_scroll_and_reopen(self):
        from hermes_state import SessionDB
        from tools.memory_tool import MemoryStore, memory_tool
        from tools.session_search_tool import session_search
        from validation_checks import pair_messages, execution_state, digest, scroll_messages
        path = Path(os.environ['HERMES_HOME']) / 'self-check.db'
        store = MemoryStore()
        store.load_from_disk()
        # 工具真实执行，消息由测试构造后写入原生库；不声称模型自主调用。
        actual = memory_tool(action='add', content='receipt_only_marker_q719', store=store)
        self.assertTrue(json.loads(actual)['success'])
        db = SessionDB(path)
        try:
            db.create_session('self-check', source='cli')
            db.set_session_title('self-check', '构造自检，不是模型运行')
            db.append_message('self-check', 'user', 'lesson13-memory 核查请求')
            call = request('memory-1', 'memory', {'action': 'add', 'content': 'receipt_only_marker_q719'})
            db.append_message('self-check', 'assistant', call['content'], tool_calls=call['tool_calls'])
            db.append_message('self-check', 'tool', actual, tool_call_id='memory-1', tool_name='memory')
            db.append_message('self-check', 'tool', 'toolonlyneedle739', tool_call_id='unpaired-self-check', tool_name='terminal')
            db.append_message('self-check', 'assistant', '已收到工具回执')
            before = digest(db.get_messages('self-check', include_inactive=True))
            discovery = json.loads(session_search(query='lesson13-memory', db=db))
            self.assertTrue(discovery['success'])
            hit = discovery['results'][0]
            default = json.loads(session_search(query='toolonlyneedle739', db=db))
            explicit = json.loads(session_search(query='toolonlyneedle739', role_filter='tool', db=db))
            self.assertEqual(default.get('results', []), [])
            self.assertTrue(explicit.get('results'))
            messages, windows = scroll_messages(db, hit['session_id'], hit['match_message_id'], window=1)
            pair = next(p for p in pair_messages(messages) if p['name'] == 'memory')
            self.assertEqual(execution_state(pair), 'completed')
            self.assertEqual(pair['receipt'], json.loads(actual))
            self.assertGreater(len(windows), 1)
            self.assertEqual(before, digest(db.get_messages('self-check', include_inactive=True)))
        finally:
            db.close()
        db = SessionDB(path)
        try:
            self.assertEqual(before, digest(db.get_messages('self-check', include_inactive=True)))
        finally:
            db.close()

    def history_fixture(self, memory_payload, *, source_status='completed', hide_receipts=False,
                        rollback_state='denied', tamper_memory_receipt=False):
        """构造请求留档；检索、展开、重开与验收都使用真实 SessionDB。"""
        from hermes_state import SessionDB
        from tools.session_search_tool import session_search
        from validation_checks import digest, verify_live
        import uuid
        directory = Path(os.environ['HERMES_HOME']) / ('trace-' + uuid.uuid4().hex)
        directory.mkdir()
        db = SessionDB(directory / 'state.db')
        sessions = ('memory-source', 'incident-source', 'audit-source')
        for sid in sessions:
            db.create_session(sid, source='cli')
        def call(sid, identity, name, args, payload):
            db.append_message(sid, 'assistant', '', tool_calls=request(identity, name, args)['tool_calls'])
            db.append_message(sid, 'tool', json.dumps(payload), tool_call_id=identity, tool_name=name)
        db.append_message(sessions[0], 'user', 'lesson13-memory 保存约定')
        call(sessions[0], 'memory-raw', 'memory', {'action': 'add', 'content': '约定'}, memory_payload)
        db.append_message(sessions[1], 'user', 'rollback 之后恢复的说法需要核查')
        call(sessions[1], 'rollback-raw', 'terminal', {'command': 'payctl rollback'},
             {'status': rollback_state, 'task_created': rollback_state != 'denied'})
        call(sessions[1], 'pause-raw', 'terminal', {'command': 'payctl pause repeat-pay'}, {'status': 'completed'})
        call(sessions[1], 'describe-raw', 'terminal', {'command': 'payctl describe'},
             {'version': 'sample-v2', 'batch_active': False})
        call(sessions[1], 'logs-raw', 'terminal', {'command': 'payctl logs'}, {'rows': [{'status': 200}] * 10})
        before = {sid: digest(db.get_messages(sid, include_inactive=True)) for sid in sessions[:2]}
        db.append_message(sessions[2], 'user', '从原始请求与回执核查两项旧事')
        for index, (sid, query) in enumerate(zip(sessions[:2], ('lesson13-memory', 'rollback'))):
            discovery = json.loads(session_search(query=query, db=db))
            hit = next(h for h in discovery['results'] if h['session_id'] == sid)
            call(sessions[2], 'search-' + str(index), 'session_search', {'query': query}, discovery)
            args = {'session_id': sid, 'around_message_id': hit['match_message_id'], 'window': 20}
            view = json.loads(session_search(db=db, **args))
            if hide_receipts:
                view['messages'] = [m for m in view['messages'] if m['role'] != 'tool']
            if tamper_memory_receipt and sid == sessions[0]:
                for message in view['messages']:
                    if message.get('tool_call_id') == 'memory-raw':
                        message['content'] = json.dumps({'success': True, 'done': True})
            call(sessions[2], 'scroll-' + str(index), 'session_search', args, view)
        db.close()
        tasks = {key: {'session': sid, 'status': source_status if key == 'memory' else 'completed',
                       'result': {'completed': key != 'memory' or source_status == 'completed',
                                  'final_response': '构造验收回答'}}
                 for key, sid in zip(('memory', 'incident', 'audit'), sessions)}
        return verify_live(directory / 'state.db', tasks, directory, before)

    def test_failed_memory_is_successfully_traced_without_claiming_it_was_saved(self):
        from tools.memory_tool import MemoryStore, memory_tool
        store = MemoryStore(); store.load_from_disk()
        actual = json.loads(memory_tool(action='replace', old_text='missing-self-check-source',
                                        content='不会写入', store=store))
        self.assertFalse(actual['success'])
        result = self.history_fixture(actual, source_status='failed')
        self.assertTrue(result['passed'], result['checks'])
        self.assertFalse(result['source_memory']['write_completed'])
        self.assertFalse(result['source_memory']['file_nonempty'])
        self.assertEqual(result['source_memory']['operations'][0]['execution_state'], 'failed')
        self.assertEqual(result['source_memory']['task_status'], 'failed')

    def test_staged_or_unknown_memory_never_becomes_completed(self):
        for payload, expected in (({'success': True, 'staged': True}, 'accepted'),
                                  ({'success': True}, 'unknown'),
                                  ({'success': True, 'done': True}, 'completed')):
            with self.subTest(expected=expected):
                result = self.history_fixture(payload)
                self.assertTrue(result['passed'], result['checks'])
                self.assertEqual(result['source_memory']['operations'][0]['execution_state'], expected)
                self.assertEqual(result['source_memory']['write_completed'], expected == 'completed')

    def test_discovery_without_original_receipts_does_not_pass_trace_checks(self):
        result = self.history_fixture({'success': False, 'error': '构造拒绝'}, hide_receipts=True)
        self.assertFalse(result['passed'])
        self.assertFalse(result['checks']['核查实际读到Memory请求和原回执'])
        self.assertFalse(result['checks']['核查实际读到回滚及暂停的原请求回执'])

    def test_recalled_receipt_cannot_turn_a_failed_source_into_success(self):
        result = self.history_fixture({'success': False, 'error': '构造原始写入失败'}, tamper_memory_receipt=True)
        self.assertFalse(result['passed'])
        self.assertFalse(result['checks']['核查实际读到Memory请求和原回执'])
        self.assertFalse(result['source_memory']['write_completed'])
        self.assertEqual(result['source_memory']['operations'][0]['execution_state'], 'failed')

    def test_a_completed_rollback_cannot_be_scored_as_denied(self):
        result = self.history_fixture({'success': False}, rollback_state='completed')
        self.assertFalse(result['passed'])
        self.assertFalse(result['checks']['回滚被拒绝而不是已经完成'])

    def test_wait_allows_a_failed_source_only_after_it_is_drained(self):
        import lab
        failed = {'status': 'failed', 'drained': False, 'error': '构造源任务失败'}
        drained = {**failed, 'drained': True}
        with patch.object(lab, 'api', side_effect=[failed, [], drained, []]), patch.object(lab.time, 'sleep'):
            task, reviews = lab.wait('source', allow_failed_source=True)
        self.assertEqual(task['status'], 'failed')
        self.assertTrue(task['drained'])
        with patch.object(lab, 'api', side_effect=[drained, []]), self.assertRaises(RuntimeError):
            lab.wait('ordinary-task')

    def test_no_live_permission_or_old_replay_never_load_credentials(self):
        from demo import LiveRun, ReplayUnavailable
        with patch.dict(sys.modules, {'credentials': SimpleNamespace(load_credentials=Mock(side_effect=AssertionError('不得读凭据'))) }):
            run = LiveRun()
            try:
                with self.assertRaisesRegex(RuntimeError, '调用模型'):
                    run.prepare()
                with patch.dict(os.environ, {'LAB_VALIDATION_RUN_ID': 'old'}):
                    with self.assertRaises(ReplayUnavailable):
                        run.prepare()
            finally:
                run.close()

    def make_service(self, fail=False):
        from oncall_server import Service
        service = Service.__new__(Service)
        service.lock = threading.RLock()
        service.queue = queue.Queue()
        service.accepting = True
        service.stopping = service.closed = False
        service.observer = SimpleNamespace(wait_existing=lambda timeout: True)
        calls = []
        def close_agent():
            calls.append('agent')
            if fail:
                raise RuntimeError('构造退出失败')
        service.agents = {'one': SimpleNamespace(shutdown_memory_provider=lambda messages: calls.append('provider'), close=close_agent)}
        service.histories = {'one': []}
        service.session_db = SimpleNamespace(close=lambda: calls.append('session_db'))
        service.db = SimpleNamespace(close=lambda: calls.append('task_db'))
        def worker():
            self.assertIsNone(service.queue.get())
            service.queue.task_done()
        service.worker = threading.Thread(target=worker)
        service.worker.start()
        return service, calls

    def test_shutdown_drains_and_closes_resources_even_after_error(self):
        for fail in (False, True):
            service, calls = self.make_service(fail)
            with patch('oncall_server.record') as save:
                if fail:
                    with self.assertRaisesRegex(RuntimeError, '退出失败'):
                        service.close()
                else:
                    service.close()
                self.assertEqual(save.call_args.args[1]['completed'], not fail)
            self.assertEqual(calls, ['provider', 'agent', 'session_db', 'task_db'])
            self.assertFalse(service.worker.is_alive())
            self.assertFalse(service.accepting)

    def test_shutdown_blocks_new_work_before_existing_work_finishes(self):
        from oncall_server import Service
        service = Service.__new__(Service)
        service.lock = threading.RLock()
        service.accepting = True
        service.stopping = False
        service.queue = queue.Queue()
        service.queue.put('running')
        service.observer = SimpleNamespace(wait_existing=lambda timeout: True)
        with self.assertRaisesRegex(ValueError, '仍有'):
            service.request_shutdown()
        self.assertFalse(service.accepting)
        with self.assertRaisesRegex(ValueError, '停止接收'):
            service.submit('any', 'must not run')

    def test_notebook_error_closes_owned_run(self):
        from notebook_workshop import NotebookWorkshop
        workshop = NotebookWorkshop()
        try:
            with patch.object(workshop, 'close') as close:
                workshop.after_cell(SimpleNamespace(error_before_exec=None, error_in_exec=KeyboardInterrupt()))
                close.assert_called_once()
        finally:
            workshop.close()

    def test_http_shutdown_with_real_empty_service_and_databases(self):
        from http.server import ThreadingHTTPServer
        from common import http
        from oncall_server import Handler, Service
        import run_agent
        directory = Path(os.environ['SESSION_LAB_RUNTIME']) / 'http-check'
        # 不创建 Agent，不触发模型；若意外走到模型入口就立即失败。
        with patch.object(run_agent.AIAgent, '__init__', side_effect=AssertionError('不得创建模型客户端')), \
             patch('oncall_server.observations', return_value=directory):
            service = Service(key=None, model='self-check-no-model', base_url='http://127.0.0.1:1')
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.service, server.token = service, 'self-check-local-token'
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                self.assertTrue(http(base + '/status', token=server.token)['healthy'])
                self.assertTrue(http(base + '/shutdown', {}, server.token)['stopping'])
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            finally:
                server.shutdown()
                try:
                    service.close()
                finally:
                    server.server_close()
                    thread.join(timeout=5)
            self.assertFalse(service.worker.is_alive())
            self.assertTrue(json.loads((directory / 'service-cleanup.json').read_text())['completed'])


def main():
    (HERE / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='self-check-', dir=HERE / 'runtime') as name:
        work = Path(name)
        # 不继承凭据，不读取个人 Home；所有临时文件都在本讲目录中。
        os.environ.clear()
        os.environ.update(PATH='/usr/bin:/bin', HOME=str(work / 'home'),
                          HERMES_HOME=str(work / 'hermes'), SESSION_LAB_RUNTIME=str(work),
                          HERMES_SRC=str(SOURCE), PYTHONDONTWRITEBYTECODE='1')
        (work / 'home').mkdir()
        (work / 'hermes').mkdir()
        (work / 'hermes/config.yaml').write_text('memory:\n  write_approval: false\n')
        sys.path.insert(0, str(SOURCE))
        sys.dont_write_bytecode = True
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Checks)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('自检只执行原生本地工具与构造反例；模型请求为0，不代表live已验收。')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
