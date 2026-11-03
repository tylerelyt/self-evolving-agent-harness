"""题包接续的边界检查；构造结果不代表真实模型表现。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from enterprise import EnterpriseRunner, business_grader, load_manifest
from support import digest


class EnterpriseTests(unittest.TestCase):
    def setUp(self):
        self.cases = [{'id': str(i), 'split': 'holdout' if i == 2 else 'dev',
            'input': {'prompt': '独立调查'+str(i)},
            'snapshot': {'incident_id': str(i), 'snapshot_id': str(i)},
            'provenance': {'trace_ids': [str(i)]}} for i in range(3)]
        self.pack = {'ready': self.cases, 'pending': [{'id': 'pending'}],
                     'pack_sha256': 'verified', 'versions': {'scorer_version': 'same'}}

    def load(self, splits):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'manifest.json'
            path.write_text(json.dumps({'pack': 'pack.json', 'splits': splits}))
            with patch('enterprise.call_case', return_value=(self.pack, tmp)):
                return load_manifest(path)

    def test_explicit_splits_keep_original_answers_and_versions(self):
        self.cases[0]['annotation'] = {'expected_output': {'root_cause': 'independent'}}
        groups, source = self.load({'train':['0'], 'validation':['1'], 'holdout':['2']})
        self.assertEqual(groups['train'][0]['annotation'], self.cases[0]['annotation'])
        self.assertEqual(source['versions'], self.pack['versions'])
        self.assertEqual(source['pending_not_selected'], 1)

    def test_duplicate_incident_or_trace_and_holdout_leak_are_rejected(self):
        for field in ('incident_id', 'snapshot_id'):
            saved = deepcopy(self.cases[1])
            self.cases[1]['snapshot'][field] = self.cases[0]['snapshot'][field]
            with self.assertRaisesRegex(ValueError, '同一事故'):
                self.load({'train':['0'], 'validation':['1'], 'holdout':['2']})
            self.cases[1] = saved
        self.cases[1]['provenance']['trace_ids'] = ['0']
        with self.assertRaisesRegex(ValueError, '同一事故'):
            self.load({'train':['0'], 'validation':['1'], 'holdout':['2']})
        self.cases[1]['provenance']['trace_ids'] = ['1']
        with self.assertRaisesRegex(ValueError, '留出题'):
            self.load({'train':['2'], 'validation':['1'], 'holdout':['0']})

    def test_insufficient_or_pending_items_are_not_filled_in(self):
        for splits in ({'train':['0'], 'validation':[], 'holdout':['2']},
                       {'train':['0'], 'validation':['pending'], 'holdout':['2']},
                       {'train':['0'], 'validation':['0'], 'holdout':['2']}):
            with self.assertRaises(ValueError):
                self.load(splits)

    def test_infrastructure_is_not_scored_as_failure(self):
        with self.assertRaisesRegex(RuntimeError, '不能把基础设施'):
            business_grader({}, {'grading': {'status':'unscored', 'score':None}})
        task = {'annotation': {'expected_output':{}, 'required_evidence':['prod'], 'required_exclusion_evidence':['staging']}}
        verdict = {'status':'scored', 'score':0, 'errors':['回答格式无效']}
        result = business_grader(task, {'grading':verdict})
        self.assertEqual(result['feedback']['grading'], verdict)
        self.assertEqual(result['score'], 0)

    def test_holdout_and_session_limits_precede_execution(self):
        for limit in (0, 1):
            with self.subTest(limit=limit), patch('enterprise.call_case') as execute:
                runner = EnterpriseRunner(Path('.'), '', {'max_iterations':1}, limit)
                with self.assertRaisesRegex(ValueError, '留出题'):
                    runner(task={'group':'holdout'}, skill_body='method')
                with self.assertRaisesRegex(RuntimeError, '会话上限'):
                    runner(task={'group':'train'}, skill_body='method')
                execute.assert_not_called()
                self.assertEqual((runner.calls, runner.sessions), (0, 0))

    def test_actual_format_sessions_release_only_unused_reservation(self):
        task = {'group': 'train', 'id': 'offline', 'pack_path': 'unused', 'pack_sha256': 'verified'}
        header = '---\nname: oncall-investigation\n---\n'
        repairs = iter((None, {'completed': True, 'api_calls': 1, 'content_preserved': False}))

        def execute(pack, **kwargs):
            execution = {'skill_sha256': digest(kwargs['skill']), 'final_message': '{}',
                         'receipts': [], 'format_repair': next(repairs)}
            return {'pack_sha256': 'verified', 'execution': execution, 'grading': {}}, Path('offline')

        with tempfile.TemporaryDirectory() as tmp, patch('enterprise.call_case', side_effect=execute) as call:
            runner = EnterpriseRunner(Path(tmp), header, {'max_iterations': 1}, 3)
            runner(task=task, skill_body='只读调查')
            self.assertEqual((runner.calls, runner.sessions, runner.format_sessions), (1, 1, 0))
            runner(task=task, skill_body='只读调查')
            # 即使格式修正未被采纳，它也实际发生，不能退回这次会话额度。
            self.assertEqual((runner.calls, runner.sessions, runner.format_sessions), (2, 3, 1))
            with self.assertRaisesRegex(RuntimeError, '会话上限'):
                runner(task=task, skill_body='只读调查')
            self.assertEqual(call.call_count, 2)

    def test_execution_error_keeps_reserved_sessions(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('enterprise.call_case', side_effect=RuntimeError('执行中断')) as call:
            runner = EnterpriseRunner(Path(tmp), '---\nname: oncall-investigation\n---\n',
                                      {'max_iterations': 1}, 2)
            task = {'group': 'train', 'id': 'offline', 'pack_path': 'unused'}
            with self.assertRaisesRegex(RuntimeError, '执行中断'):
                runner(task=task, skill_body='只读调查')
            self.assertEqual(runner.sessions, 2)
            with self.assertRaisesRegex(RuntimeError, '会话上限'):
                runner(task=task, skill_body='只读调查')
            self.assertEqual(call.call_count, 1)

    def test_protocol_describes_the_selected_executor(self):
        from optimize import execution_protocol
        protocol = execution_protocol('model', 'http://localhost/v1', 7,
            {'kind':'lesson19_pack', 'versions':{'scorer_version':'oncall-evidence-v3'}})
        self.assertEqual((protocol['max_tokens'], protocol['worker_timeout_seconds']), (4096, 600))
        self.assertEqual(protocol['max_iterations'], 7)
        self.assertEqual(protocol['scorer_version'], 'oncall-evidence-v3')
        self.assertNotIn('preset_memory', protocol)
        self.assertEqual(protocol['base_url'], protocol['connection']['execution_endpoint'])


if __name__ == '__main__':
    unittest.main()
