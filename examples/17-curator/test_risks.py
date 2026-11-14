"""诊断能力、采用范围与归档读取反例；原生工具检查不调用模型。"""
import hashlib
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from common import HERE
from adoption import adopt_package, package_manifest
from curator_read_adapter import CuratorReadAdapter
from curation_workshop import review_outcome, extract_answer, assess_probe, CurationLab
from diagnostics import RULES, PLANS, query


def probe_fixture(kind='capacity', *, resource='renamed/diagnostic-rules.md', inlined=False):
    messages = []
    def tool(name, args, receipt):
        identifier = 'fixture-' + str(len(messages))
        messages.append({'role': 'assistant', 'tool_calls': [{'id': identifier,
            'function': {'name': name, 'arguments': json.dumps(args)}}]})
        messages.append({'role': 'tool', 'tool_call_id': identifier, 'content': json.dumps(receipt)})
    args = {'name': 'connection-diagnostics'}
    if not inlined:
        args['file_path'] = resource
    tool('skill_view', args, {'success': True, 'content': '# Relevant diagnostic rule\n```json\n' + json.dumps(RULES[kind]) + '\n```'})
    sections = ['metrics', 'holders'] + (['release_paths'] if kind == 'leak' else [])
    evidence = []
    for section in sections:
        receipt = query(kind, section)
        tool('diagnostic_query', {'section': section}, receipt)
        evidence += [row['id'] for row in receipt['records']]
    answer = {'diagnosis': RULES[kind]['diagnosis'],
        'judgments': {'demand_exceeds_limit': kind == 'capacity', 'connections_return_after_peak': kind == 'capacity',
                      'release_failure_confirmed': False}, 'evidence_ids': evidence, 'next_check': deepcopy(PLANS[kind])}
    return {'completed': True, 'messages': messages, 'final_response': json.dumps(answer)}


class DiagnosticRetentionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('HERMES_NATIVE_TESTS') == '1',
                         'Optional native check: set HERMES_NATIVE_TESTS=1 and HERMES_PYTHON to the pinned runtime')
    def test_native_registry_exposes_scoped_query_and_reads_actual_skill_resources_without_model(self):
        script = r'''
import json,shutil
from curation_workshop import CurationLab,assess_probe
from diagnostics import register_query,query,probe_tools
from test_risks import probe_fixture
lab=CurationLab()
try:
 lab.seed()
 from tools.registry import registry
 register_query('leak')
 names={tool['function']['name'] for tool in probe_tools()}
 assert {'skills_list','skill_view','diagnostic_query'} == names,names
 result=probe_fixture('leak')
 for index,message in enumerate(result['messages']):
  if message['role']!='assistant':continue
  call=message['tool_calls'][0]['function'];args=json.loads(call['arguments'])
  if call['name']=='skill_view':args={'name':'connection-leak-investigation','file_path':'references/release-path.md'}
  call['arguments']=json.dumps(args)
  receipt=registry.dispatch(call['name'],args)
  result['messages'][index+1]['content']=receipt if isinstance(receipt,str) else json.dumps(receipt)
 assert assess_probe(result,'leak')['task_pass'],assess_probe(result,'leak')
 print('PASS native tool definitions/dispatch and actual resource read; model result is an explicit fixture')
finally:shutil.rmtree(lab.root)
'''
        python = os.environ.get('HERMES_PYTHON') or sys.executable
        result = subprocess.run([python, '-B', '-c', script], cwd=HERE,
                                text=True, capture_output=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def change_answer(self, result, field, value):
        answer = json.loads(result['final_response']); answer[field] = value
        result['final_response'] = json.dumps(answer)
        return result

    def test_capacity_and_special_leak_branch_require_actual_evidence_and_plan(self):
        for kind in ('capacity', 'leak'):
            with self.subTest(kind=kind):
                self.assertTrue(assess_probe(probe_fixture(kind), kind)['task_pass'])

    def test_resource_can_be_renamed_or_inlined_in_common_entry(self):
        self.assertTrue(assess_probe(probe_fixture('leak', resource='references/merged.md'), 'leak')['task_pass'])
        self.assertTrue(assess_probe(probe_fixture('leak', inlined=True), 'leak')['task_pass'])
        result = probe_fixture('leak')
        rule = deepcopy(RULES['leak']); rule['next_check']['checks'].reverse()
        result['messages'][1]['content'] = json.dumps({'success': True, 'content': '```json\n' + json.dumps(rule) + '\n```'})
        self.assertTrue(assess_probe(result, 'leak')['task_pass'])

    def test_duplicate_or_orphan_receipts_cannot_pass(self):
        result = probe_fixture(); result['messages'].append(deepcopy(result['messages'][1]))
        self.assertFalse(assess_probe(result, 'capacity')['task_pass'])
        result = probe_fixture(); result['messages'][1]['tool_call_id'] = None
        self.assertFalse(assess_probe(result, 'capacity')['task_pass'])

    def test_correct_label_and_arbitrary_read_do_not_prove_retained_capability(self):
        result = probe_fixture('leak')
        result['messages'][1]['content'] = json.dumps({'success': True, 'content': 'unrelated handbook'})
        assessment = assess_probe(result, 'leak')
        self.assertFalse(assessment['checks']['related_rule_read'])
        self.assertFalse(assessment['task_pass'])
        legacy = self.change_answer(probe_fixture(), 'next_check', '再检查一下')
        self.assertFalse(assess_probe(legacy, 'capacity')['task_pass'])

    def test_capacity_rule_or_capability_label_alone_cannot_replace_leak_detail(self):
        result = probe_fixture('leak')
        for rule in (RULES['capacity'], {'schema': 'connection-diagnostic-rule-v1', 'capability': 'leak'}):
            result['messages'][1]['content'] = json.dumps({'success': True, 'content': '```json\n' + json.dumps(rule) + '\n```'})
            self.assertFalse(assess_probe(result, 'leak')['task_pass'])

    def test_query_receipts_scope_and_unqueried_citations_are_checked(self):
        result = probe_fixture('leak')
        receipt = json.loads(result['messages'][3]['content']); receipt['scope']['cluster'] = 'other'
        result['messages'][3]['content'] = json.dumps(receipt)
        self.assertFalse(assess_probe(result, 'leak')['checks']['current_queries_match'])
        result = probe_fixture('leak')
        result['messages'] = result['messages'][:-2]
        self.assertFalse(assess_probe(result, 'leak')['task_pass'])

    def test_unknown_release_code_cannot_be_reported_as_confirmed_failure(self):
        result = probe_fixture('leak')
        answer = json.loads(result['final_response']); answer['judgments']['release_failure_confirmed'] = True
        result['final_response'] = json.dumps(answer)
        self.assertFalse(assess_probe(result, 'leak')['checks']['necessary_judgments'])

    def test_next_step_must_cover_exception_early_return_and_failure_verification(self):
        for field, omitted in [('checks', 'exception'), ('checks', 'early_return'),
                               ('verification', 'failed_request_returns_connection')]:
            result = probe_fixture('leak'); answer = json.loads(result['final_response'])
            answer['next_check'][field].remove(omitted); result['final_response'] = json.dumps(answer)
            self.assertFalse(assess_probe(result, 'leak')['checks']['verifiable_next_check'])

    def test_support_contracts_exist_in_both_initial_packages(self):
        for kind, path in [('capacity', 'connection-pool-triage/references/pool-metrics.md'),
                           ('leak', 'connection-leak-investigation/references/release-path.md')]:
            result = probe_fixture(kind)
            result['messages'][1]['content'] = json.dumps({'success': True,
                'content': (HERE / 'materials' / path).read_text()})
            self.assertTrue(assess_probe(result, kind)['task_pass'])

    def test_same_task_before_and_after_is_required_including_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = object.__new__(CurationLab); lab.root = Path(directory); lab.checks = {}; lab.probes = {}
            assessment = assess_probe(probe_fixture(), 'capacity')
            for phase in ('before', 'after'):
                lab.probes[phase, 'capacity'] = {'protocol': {'model': 'fixture'}, 'input': 'same task',
                    'assessment': deepcopy(assessment), 'session_id': phase, 'passed': True}
            self.assertTrue(lab.compare_probe('capacity')['retained'])
            lab.probes['after', 'capacity']['assessment']['snapshot_sha256'] = 'changed'
            self.assertFalse(lab.compare_probe('capacity')['comparable'])


class RiskTests(unittest.TestCase):
    def test_json_envelope_does_not_hide_ambiguity(self):
        import json
        answer = {'diagnosis': 'capacity-pressure', 'next_check': '检查慢查询'}
        raw = json.dumps(answer)
        block = '```json\n' + raw + '\n```'
        self.assertTrue(extract_answer(raw)['strict_json_format_pass'])
        self.assertEqual(extract_answer(block)['answer'], answer)
        self.assertFalse(extract_answer(block)['strict_json_format_pass'])
        self.assertIsNone(extract_answer(block + '\n' + block)['answer'])
        self.assertIsNone(extract_answer('{"diagnosis":"unknown","diagnosis":"capacity-pressure"}')['answer'])
        self.assertEqual(extract_answer('实际不是容量问题\n' + block)['outside_text'], '实际不是容量问题')

    def test_no_op_requires_completed_current_run(self):
        native = {'started_at': 'current', 'llm_error': None}
        returned = {'started_at': 'current'}
        loops = [{'completed': True}]
        self.assertEqual(review_outcome(native, returned, loops, {}, {}), 'no-op')
        self.assertEqual(review_outcome(native, returned, loops, {}, {'a': 'new'}), 'changed')
        self.assertEqual(review_outcome({**native, 'llm_error': 'failed'}, returned, loops, {}, {}), 'incomplete')
        self.assertEqual(review_outcome(native, {'started_at': 'old'}, loops, {}, {}), 'incomplete')
        self.assertEqual(review_outcome(native, returned, [{'completed': False}], {}, {}), 'incomplete')

    def setUp(self):
        (HERE / 'runtime').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=HERE / 'runtime')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidate = self.root / 'candidate'; self.candidate.mkdir()
        self.formal = self.root / 'formal'; self.formal.mkdir()
        (self.formal / 'old.md').write_text('old')
        skill = self.candidate / 'general'; skill.mkdir()
        (skill / 'SKILL.md').write_text('references/detail.md')
        (skill / 'references').mkdir()
        (skill / 'references/detail.md').write_text('details')
        (self.candidate / '.usage.json').write_text('{}')

    def adopt(self, expected):
        return adopt_package(self.candidate, self.formal, self.root / 'stage', self.root / 'previous', expected)

    def test_hidden_state_is_checked(self):
        expected = package_manifest(self.candidate)
        (self.candidate / '.usage.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.adopt(expected)
        self.assertEqual((self.formal / 'old.md').read_text(), 'old')

    def test_binary_resource_is_checked(self):
        (self.candidate / 'general/resource.bin').write_bytes(bytes([0, 255, 1]))
        expected = package_manifest(self.candidate)
        self.adopt(expected)
        self.assertEqual(package_manifest(self.formal), expected)
        self.assertTrue((self.root / 'previous/old.md').exists())

    def test_copy_race_keeps_formal_directory(self):
        import shutil
        original = shutil.copytree
        def changed_copy(source, destination, *args, **kwargs):
            result = original(source, destination, *args, **kwargs)
            if Path(destination) == self.root / 'stage':
                (Path(destination) / '.usage.json').write_text('{"changed":true}')
            return result
        with patch('adoption.shutil.copytree', side_effect=changed_copy), self.assertRaises(ValueError):
            self.adopt(package_manifest(self.candidate))
        self.assertTrue((self.formal / 'old.md').exists())

    def test_symlink_is_rejected(self):
        (self.candidate / 'linked').symlink_to(self.formal)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            package_manifest(self.candidate)

    def test_changed_or_unread_support_file_blocks_archive(self):
        adapter = CuratorReadAdapter(self.candidate)
        for path in (self.candidate / 'general').rglob('*'):
            if path.is_file():
                adapter.reads[path.resolve()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(adapter.unread_target_files('general'), [])
        (self.candidate / 'general/references/detail.md').write_text('changed')
        self.assertEqual(adapter.unread_target_files('general'), ['general/references/detail.md'])
        with self.assertRaises(ValueError):
            adapter.unread_target_files('../formal')


if __name__ == '__main__':
    unittest.main()
