"""State-boundary regressions for the diagnostic Memory workbench worker."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import workbench_evolution as evolution
from environment import DIAGNOSTIC_SOURCE


class DiagnosticMemoryStagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = self.root / 'environments/instance-a'
        self.environment.mkdir(parents=True)
        self.scope = {'service': 'payments', 'environment_id': 'a' * 32}
        self.native_calls = []
        self.keep_review = False
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.observation = {'source': 'live_lab_http_requests', **self.scope}
        stack.enter_context(patch.object(evolution, 'get_snapshot', side_effect=lambda *args: dict(self.observation)))
        stack.enter_context(patch.object(evolution, 'probe_diagnostic', side_effect=self.probe))
        stack.enter_context(patch.object(evolution, 'migrate_diagnostics', side_effect=self.migrate))
        stack.enter_context(patch.object(evolution, 'run_native', side_effect=self.constructed_native))

    def probe(self, environment, service, path, environment_id):
        migrated = (environment / f'{service}.diagnostics-v2').exists()
        status = 410 if migrated and path == evolution.DIAGNOSTIC_V1 else 200
        return {'source': DIAGNOSTIC_SOURCE, 'service': service, 'environment_id': environment_id,
                'path': path, 'http_status': status, 'active': status == 200,
                'replacement': evolution.DIAGNOSTIC_V2 if status == 410 else None}

    def migrate(self, environment, service):
        (environment / f'{service}.diagnostics-v2').touch(exist_ok=False)

    def constructed_native(self, directory, phase, script, arguments, live):
        """Only model the worker contract here; native execution has a separate run."""
        self.native_calls.append((phase, script))
        state = directory / phase / uuid4().hex[:12]
        state.mkdir(parents=True)
        output = state / 'result.json'
        argv = dict(zip(arguments[::2], arguments[1::2]))
        memory = Path(argv['--memory-file']).read_bytes()
        if script == 'route_task.py':
            remembered = json.loads(memory.decode().split(' ', 1)[1])['path']
            first = self.probe(self.environment, 'payments', remembered, self.scope['environment_id'])
            receipts = [first]
            if first['http_status'] == 410:
                receipts.append(self.probe(self.environment, 'payments', first['replacement'], self.scope['environment_id']))
            result = {'session_id': 'constructed-' + uuid4().hex, 'scope': self.scope,
                      'memory_sha256': hashlib.sha256(memory).hexdigest(), 'native_memory_loaded': True,
                      'loaded_memory_entry': memory.decode().removesuffix('\n'), 'prompt_item_id': 1,
                      'report_item_id': 10, 'tool_receipts': [
                          {'name': 'diagnostic_lookup', 'call_id': f'call-{index}',
                           'item_id': 3 + index * 2, 'receipt': receipt} for index, receipt in enumerate(receipts)],
                      'report': {**self.scope, 'path': receipts[-1]['path']}}
        else:
            repair = evolution.load_json(Path(argv['--repair']))
            revised = state / evolution.MEMORY_FILE
            revised.parent.mkdir(parents=True)
            revised.write_bytes(memory if self.keep_review else evolution.memory_entry(self.scope, evolution.DIAGNOSTIC_V2).encode())
            result = {'status': 'keep' if self.keep_review else 'proposed',
                      'source_sha256': evolution.digest(Path(argv['--repair'])),
                      'repair_session_id': repair['session_id'], 'repair_report_item_id': repair['report_item_id'],
                      'native_review': {'completed': True, 'memory_write_verified': not self.keep_review,
                          'memory_before_sha256': hashlib.sha256(memory).hexdigest(),
                          'memory_sha256': evolution.digest(revised), 'session_id': 'constructed-review',
                          'memory_tool_calls': [] if self.keep_review else [{'action': 'replace'}]}}
        evolution.write_json(output, result)
        return output, result

    def run_through(self, last):
        result = None
        for action in evolution.ACTIONS[:last]:
            result = evolution.run_action(self.root, action)
        return result

    def test_each_action_stops_at_its_own_observable_stage(self):
        for action, expected in zip(evolution.ACTIONS, evolution.STAGES):
            result = evolution.run_action(self.root, action)
            self.assertEqual(result['status'], expected)
            self.assertTrue(result['evidence_valid'])
            self.assertEqual(set(result['stages']), set(evolution.STAGES[:evolution.STAGES.index(expected) + 1]))
        self.assertEqual([phase for phase, _ in self.native_calls], ['before', 'repair', 'review', 'adopt'])
        repair = result['stages']['repaired']['result']
        adopted = result['stages']['adopted']['result']
        self.assertNotEqual(repair['session_id'], adopted['session_id'])
        self.assertEqual([item['receipt']['http_status'] for item in repair['tool_receipts']], [410, 200])
        self.assertEqual([item['receipt']['http_status'] for item in adopted['tool_receipts']], [200])
        self.assertNotEqual(result['original_memory']['sha256'], result['active_memory']['sha256'])
        evolution.run_action(self.root, 'diagnostic_adopt')
        self.assertEqual(len(self.native_calls), 4)

    def test_stage_order_and_environment_scope_precede_mutation(self):
        with self.assertRaisesRegex(ValueError, 'requires stage before'):
            evolution.run_action(self.root, 'diagnostic_migrate')
        self.assertFalse((self.environment / 'payments.diagnostics-v2').exists())
        self.run_through(1)
        self.observation['environment_id'] = 'b' * 32
        with self.assertRaisesRegex(ValueError, 'earlier HTTP environment'):
            evolution.run_action(self.root, 'diagnostic_migrate')
        self.assertFalse((self.environment / 'payments.diagnostics-v2').exists())

    def test_altered_repair_revokes_evidence_and_blocks_memory_write(self):
        result = self.run_through(3)
        directory = evolution.sequence_directory(self.root, 'instance-a', 'payments')
        artifact = directory / result['stages']['repaired']['artifact']
        artifact.write_text('{}')
        self.assertFalse(evolution.diagnostic_snapshot(self.root)['evidence_valid'])
        with self.assertRaisesRegex(ValueError, 'artifact has changed'):
            evolution.run_action(self.root, 'diagnostic_revise')
        self.assertEqual(len(self.native_calls), 2)

    def test_environment_and_migration_marker_cannot_redirect_outside_the_workbench(self):
        self.run_through(1)
        marker = self.environment / 'payments.diagnostics-v2'
        marker.symlink_to(self.root / 'outside-marker')
        with self.assertRaisesRegex(ValueError, 'migration marker'):
            evolution.run_action(self.root, 'diagnostic_migrate')
        self.assertFalse((self.root / 'outside-marker').exists())
        marker.unlink()
        (self.root / 'environments').rename(self.root / 'other-environments')
        (self.root / 'environments').symlink_to(self.root / 'other-environments', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'workbench-owned HTTP environment'):
            evolution.run_action(self.root, 'diagnostic_migrate')

    def test_native_keep_preserves_old_memory_and_waits_for_a_later_review(self):
        before = self.run_through(3)
        self.keep_review = True
        result = evolution.run_action(self.root, 'diagnostic_revise')
        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(result['stages']['revised']['status'], 'kept')
        self.assertEqual(result['active_memory'], before['active_memory'])
        with self.assertRaisesRegex(ValueError, 'requires stage revised'):
            evolution.run_action(self.root, 'diagnostic_adopt')
        self.keep_review = False
        self.assertEqual(evolution.run_action(self.root, 'diagnostic_revise')['status'], 'revised')

    def test_failed_worker_preserves_current_stage_and_can_retry(self):
        self.run_through(2)
        with patch.object(evolution, 'run_native', side_effect=RuntimeError('Stopped before a report')):
            with self.assertRaisesRegex(RuntimeError, 'Stopped'):
                evolution.run_action(self.root, 'diagnostic_repair')
        state = evolution.diagnostic_snapshot(self.root)
        self.assertEqual(state['status'], 'migrated')
        self.assertEqual(state['attempts'][-1]['status'], 'failed')
        self.assertEqual(evolution.run_action(self.root, 'diagnostic_repair')['status'], 'repaired')


if __name__ == '__main__':
    unittest.main()
