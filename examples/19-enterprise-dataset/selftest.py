"""离线校准：构造对错回答，只检查数据处理、查询和评分，不证明模型效果。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dataset import HERE, build, digest, inspect_pack, prepare, read, verify
from engine import dependency
from grader import grade
from langfuse_io import export, publish, restore
from monitor import query, tool_schema


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.source = read(HERE / 'fixtures/rollouts.json')
        self.annotations = read(HERE / 'fixtures/annotations.json')
        self.snapshot = read(HERE / 'fixtures/snapshots/incident-001.json')
        self.snapshots = {'incident-001': self.snapshot}
        self.pack = prepare(self.source, self.annotations, self.snapshots)
        self.case = self.pack['ready'][0]
        coverage = self.snapshot['coverage']
        self.receipts = [query(self.snapshot, kind, 'prod-eu', coverage['start'], coverage['end'])
                         for kind in ('alerts', 'metrics', 'logs', 'traces')]
        self.receipts += [query(self.snapshot, kind, 'staging-eu', coverage['start'], coverage['end'], service='checkout-api')
                          for kind in ('alerts', 'logs')]
        self.answer = {**self.case['annotation']['expected_output'],
                       'evidence_ids': self.case['annotation']['required_evidence'] + self.case['annotation']['required_exclusion_evidence'],
                       'explanation': '连接池排队先于接口超时，调用链指向同集群上游。'}

    def execution(self, answer=None, receipts=None):
        return {'status': 'completed', 'final_message': json.dumps(answer or self.answer),
                'receipts': self.receipts if receipts is None else receipts}

    def test_incident_grouping_and_pending(self):
        self.assertEqual(len(self.pack['ready']), 1)
        self.assertEqual(len(self.pack['pending']), 2)
        self.assertEqual(self.case['provenance']['trace_ids'], ['demo-trace-001', 'demo-trace-002'])
        self.assertNotIn('output', self.case['input'])
        self.assertEqual(self.case['annotation']['expected_output']['root_cause'], 'prod-eu/db-gateway')

    def test_native_collection_requires_complete_matching_execution(self):
        from pipeline import confirm_native_trace
        execution = {'receipts': self.receipts, 'api_calls': 1, 'final_message': '调查结论',
                     'telemetry': {'task_id': 'task', 'session_id': 'session'}}
        observations = [{'name': 'LLM', 'type': 'GENERATION', 'endTime': '2026-11-02T10:00:00Z',
                         'input': [{'role': 'user', 'content': '调查'}], 'output': {'content': '调查结论'}}]
        observations += [{'name': 'Tool: monitor_query', 'output': json.dumps(r),
                          'endTime': '2026-11-02T10:00:00Z'} for r in self.receipts]
        source = {'traces': [{'metadata': {'task_id': 'task'}, 'output': {'content': '调查结论'},
                              'raw_trace': {'observations': observations}}]}
        self.assertTrue(confirm_native_trace(source, execution)['tool_results_identical'])
        missing_io = deepcopy(source)
        missing_io['traces'][0]['raw_trace']['observations'][0]['input'] = None
        with self.assertRaisesRegex(ValueError, '缺少实际输入或输出'):
            confirm_native_trace(missing_io, execution)
        changed_answer = deepcopy(source)
        changed_answer['traces'][0]['output'] = None
        with self.assertRaisesRegex(ValueError, '最终回答'):
            confirm_native_trace(changed_answer, execution)
        changed = deepcopy(source)
        changed['traces'][0]['raw_trace']['observations'][-1]['output'] = '{}'
        with self.assertRaisesRegex(ValueError, '工具结果'):
            confirm_native_trace(changed, execution)
        observations.pop()
        with self.assertRaisesRegex(ValueError, '工具结果'):
            confirm_native_trace(source, execution)

    def test_native_collection_rejects_other_task_and_unfinished_spans(self):
        from pipeline import confirm_native_trace
        execution = {'receipts': [self.receipts[0]], 'api_calls': 1, 'final_message': '调查结论',
                     'telemetry': {'task_id': 'task', 'session_id': 'session'}}
        source = {'traces': [{'metadata': {'task_id': 'other'}, 'output': {'content': '调查结论'}, 'raw_trace': {'observations': [
            {'name': 'LLM', 'type': 'GENERATION', 'endTime': 'done',
             'input': [{'role': 'user', 'content': '调查'}], 'output': {'content': '调查结论'}},
            {'name': 'Tool: monitor_query', 'output': self.receipts[0], 'endTime': 'done'}]}}]}
        with self.assertRaisesRegex(ValueError, '另一任务'):
            confirm_native_trace(source, execution)
        source['traces'][0]['metadata']['task_id'] = 'task'
        source['traces'][0]['raw_trace']['observations'][0]['endTime'] = None
        with self.assertRaisesRegex(ValueError, '尚未结束'):
            confirm_native_trace(source, execution)

    def test_format_trace_requires_model_io_and_no_tools(self):
        from pipeline import wait_for_trace
        final = json.dumps(self.answer)
        initial = '调查完成。\n' + final
        execution = {'receipts': [self.receipts[0]], 'api_calls': 2,
            'investigation_api_calls': 1, 'investigation_final_message': initial,
            'final_message': final,
            'telemetry': {'task_id': 'task', 'session_id': 'session',
                          'expected_trace_id': 'investigation', 'format_trace_id': 'format'},
            'format_repair': {'api_calls': 1, 'corrected_response': final}}
        generation = {'name': 'LLM', 'type': 'GENERATION', 'endTime': 'done',
                      'input': [{'role': 'user', 'content': '调查'}], 'output': {'content': initial}}
        source = {'traces': [{'metadata': {'task_id': 'task'}, 'output': {'content': initial},
            'raw_trace': {'observations': [generation,
                {'name': 'Tool: monitor_query', 'output': self.receipts[0], 'endTime': 'done'}]}}]}
        formatted = {'traces': [{'output': {'content': final}, 'raw_trace': {'observations': [
            {**generation, 'input': [{'role': 'user', 'content': initial}], 'output': {'content': final}}]}}]}

        def check(format_trace):
            traces = {'investigation': source, 'format': format_trace}
            # 只检查一轮读取；坏轨迹直接走到截止时间，不等待或访问远端。
            with patch('pipeline.collect', side_effect=lambda lf, ids: traces[ids[0]]), \
                    patch('pipeline.time.monotonic', side_effect=[0, 0, 1]), \
                    patch('pipeline.time.sleep'):
                return wait_for_trace(None, execution, timeout=1)

        collected, checks = check(formatted)
        self.assertEqual(collected, source)
        self.assertEqual(checks['model_observations'], 1)
        self.assertEqual(checks['format_repair_trace'], formatted)
        for field, value in (
            ('input', None), ('input', []), ('output', None),
            ('output', {'content': final, 'tool_calls': [{'id': 'unexpected-tool'}]}),
        ):
            with self.subTest(field=field, value=value):
                changed = deepcopy(formatted)
                changed['traces'][0]['raw_trace']['observations'][0][field] = value
                with self.assertRaisesRegex(RuntimeError, '缺少实际输入输出，或使用了工具'):
                    check(changed)
        for tool in ({'type': 'TOOL', 'name': 'monitor_query'},
                     {'type': 'SPAN', 'name': 'Tool: monitor_query'}):
            with self.subTest(tool=tool):
                changed = deepcopy(formatted)
                # 保留正确的generation数量，不能让数量不符掩盖工具检查失效。
                changed['traces'][0]['raw_trace']['observations'].append({**tool, 'endTime': 'done'})
                with self.assertRaisesRegex(RuntimeError, '格式修正的原生 Trace'):
                    check(changed)

    def test_plugin_projection_reports_loss_without_changing_snapshot(self):
        from pipeline import plugin_projection, confirm_snapshot_receipts
        receipt = deepcopy(query(self.snapshot, 'dependencies', 'prod-eu',
                        self.snapshot['coverage']['start'], self.snapshot['coverage']['end'], service='checkout-api'))
        before = deepcopy(receipt)
        projected, paths = plugin_projection(receipt)
        self.assertEqual(paths, ['$[rows][0][value][upstream][0]'])
        self.assertEqual(projected['rows'][0]['value']['upstream'], ['<max-depth>'])
        self.assertEqual(receipt, before)
        self.assertEqual(receipt['rows'][0]['value']['upstream'], ['prod-eu/db-gateway'])
        execution = {'snapshot_content_sha256': digest(self.snapshot), 'receipts': [receipt]}
        confirm_snapshot_receipts(execution, self.snapshot)
        receipt['rows'][0]['value']['upstream'] = ['another-service']
        with self.assertRaisesRegex(ValueError, '完整工具回执'):
            confirm_snapshot_receipts(execution, self.snapshot)

    def test_missing_independent_basis_never_uses_rollout_answer(self):
        self.annotations['incident-001@prod-eu']['basis'] = []
        result = prepare(self.source, self.annotations, self.snapshots)
        self.assertEqual(result['ready'], [])
        self.assertEqual(len(result['pending']), 3)

    def test_langfuse_version_check_preserves_saved_receipts(self):
        from langfuse_smoke import check_versions
        old = {'items': [{'id': 'item', 'metadata': {'source': 'independent'}}]}
        latest = deepcopy(old['items'])
        latest[0]['metadata']['smoke_metadata_revision'] = 2
        before = deepcopy(latest)
        check_versions(old, deepcopy(old), latest)
        self.assertEqual(latest, before)
        newer_timestamp = deepcopy(old)
        newer_timestamp['items'][0]['updated_at'] = '2026-11-02T09:00:01Z'
        check_versions(old, newer_timestamp, latest)
        wrong = deepcopy(old)
        wrong['items'][0]['metadata']['source'] = 'changed'
        with self.assertRaisesRegex(ValueError, '固定旧时间点'):
            check_versions(old, wrong, latest)

    def test_langfuse_observation_pages_keep_remote_parent_and_root(self):
        from langfuse_io import collect
        pages = {
            None: SimpleNamespace(data=[{'id': 'tool', 'parentObservationId': 'root', 'metadata': {}}], meta=SimpleNamespace(cursor='next')),
            'next': SimpleNamespace(data=[{'id': 'root', 'parentObservationId': 'remote-context',
                'input': '{"prompt":"调查"}', 'output': '原回答', 'metadata': {'incident_id': 'incident'}}], meta=SimpleNamespace(cursor=None)),
        }
        api = SimpleNamespace(get_many=lambda **kwargs: pages[kwargs['cursor']])
        value = collect(SimpleNamespace(api=SimpleNamespace(observations=api)), ['trace'])
        self.assertEqual(value['traces'][0]['input'], {'prompt': '调查'})
        self.assertEqual(value['traces'][0]['output'], '原回答')
        self.assertEqual(len(value['traces'][0]['raw_trace']['observations']), 2)

    def test_langfuse_import_rejects_forged_tool_receipt(self):
        from langfuse_smoke import tool_records
        args = dict(kind='logs', cluster='prod-eu', start='2026-10-20T10:00:00Z', end='2026-10-20T10:10:00Z')
        receipt = query(self.snapshot, **args)
        messages = [
            {'role': 'assistant', 'tool_calls': [{'id': 'call', 'function': {
                'name': 'monitor_query', 'arguments': json.dumps(args)}}]},
            {'role': 'tool', 'tool_call_id': 'call', 'content': json.dumps(receipt)},
        ]
        execution = {'messages': messages, 'receipts': [receipt]}
        self.assertEqual(len(tool_records(execution, self.snapshot)), 1)
        messages[-1]['content'] = '{}'
        with self.assertRaisesRegex(ValueError, '工具回执'):
            tool_records(execution, self.snapshot)

    def test_native_turn_mapping_keeps_source_and_groups_one_incident(self):
        from langfuse_io import map_tasks
        source = {'origin': {'kind': 'langfuse'}, 'traces': [
            {'id': trace_id, 'input': text, 'output': '错误报告不能作为答案',
             'metadata': {'source': 'hermes'}, 'raw_trace': {'observations': [{'id': trace_id}]}}
            for trace_id, text in [('turn-1', '查生产超时'), ('turn-2', '再确认集群归属')]]}
        before = deepcopy(source)
        reference = self.source['traces'][0]
        tasks = {trace['id']: {'input': reference['input'], 'metadata': reference['metadata']}
                 for trace in source['traces']}
        mapped = map_tasks(source, tasks)
        pack = prepare(mapped, self.annotations, self.snapshots)
        self.assertEqual(source, before)
        self.assertEqual(len(pack['ready']), 1)
        self.assertEqual(pack['ready'][0]['provenance']['trace_ids'], ['turn-1', 'turn-2'])
        self.assertEqual(mapped['traces'][1]['before_task_mapping']['input'], '再确认集群归属')
        self.assertEqual(mapped['traces'][1]['raw_trace'], before['traces'][1]['raw_trace'])
        self.assertNotIn('output', pack['ready'][0]['input'])
        with self.assertRaisesRegex(ValueError, '逐一对应'):
            map_tasks(source, {'turn-1': tasks['turn-1']})
        tasks['turn-1']['expected_output'] = {'root_cause': '错误报告'}
        with self.assertRaisesRegex(ValueError, '独立标注'):
            map_tasks(source, tasks)

    def test_multi_root_trace_requires_explicit_task_mapping(self):
        from langfuse_io import collect
        roots = [{'id': key, 'parentObservationId': None, 'input': text,
                  'output': '本轮回答', 'metadata': {'source': 'hermes'}}
                 for key, text in [('r1', '初始调查'), ('r2', '补充范围')]]
        page = SimpleNamespace(data=roots, meta=SimpleNamespace(cursor=None))
        api = SimpleNamespace(get_many=lambda **kwargs: page)
        value = collect(SimpleNamespace(api=SimpleNamespace(observations=api)), ['multi-turn'])
        self.assertIsNone(value['traces'][0]['input'])
        self.assertEqual(value['traces'][0]['root_observation_ids'], ['r1', 'r2'])
        self.assertEqual(value['traces'][0]['raw_trace']['observations'], roots)
        self.assertFalse(prepare(value, self.annotations, self.snapshots)['ready'])

    def test_conflicting_rounds_are_not_independent_cases(self):
        self.source['traces'][1]['input']['prompt'] = '冲突的调查对象'
        self.assertFalse(prepare(self.source, self.annotations, self.snapshots)['ready'])

    def test_query_parameters_and_order(self):
        args = dict(kind='logs', cluster='prod-eu', start='2026-10-20T10:00:00Z', end='2026-10-20T10:10:00Z')
        first = query(self.snapshot, **args)
        self.assertEqual(query(self.snapshot, **{**args, 'cluster': 'staging-eu'})['rows'][0]['id'], 'log-staging')
        self.assertEqual(query(self.snapshot, **args), first)
        self.assertEqual(query(self.snapshot, **args, service='checkout-api')['rows'], [])
        self.assertEqual(query(self.snapshot, **{**args, 'end': '2026-10-20T10:30:00Z'})['status'], 'not_covered')

    def test_unknown_cluster_returns_scope_not_empty_rows(self):
        receipt = query(self.snapshot, 'alerts', 'prod-us', '2026-10-20T10:00:00Z', '2026-10-20T10:10:00Z')
        self.assertEqual(receipt['error'], 'not_covered')
        self.assertNotIn('rows', receipt)
        self.assertEqual(set(receipt['scope']['clusters']), {'prod-eu', 'staging-eu'})

    def test_covered_empty_cluster_is_discoverable_without_answer_fields(self):
        snapshot = deepcopy(self.snapshot)
        snapshot['rows'] = [r for r in snapshot['rows'] if r['cluster'] != 'staging-eu']
        receipt = query(snapshot, 'alerts', 'staging-eu', '2026-10-20T10:00:00Z', '2026-10-20T10:10:00Z')
        self.assertEqual(receipt['status'], 'covered')
        self.assertEqual(receipt['rows'], [])
        schema = tool_schema(snapshot)
        self.assertEqual(schema['parameters']['properties']['cluster']['enum'], ['prod-eu', 'staging-eu'])
        self.assertNotIn('excluded_cluster', json.dumps(schema))
        self.assertNotIn('root_cause', json.dumps(schema))
        self.assertNotIn('staging-eu', self.case['input']['prompt'])

    def test_manifest_is_required_and_partial_coverage_is_explicit(self):
        receipt = query(self.snapshot, 'metrics', 'staging-eu', '2026-10-20T10:00:00Z', '2026-10-20T10:10:00Z')
        self.assertEqual(receipt['status'], 'not_covered')
        self.assertNotIn('rows', receipt)
        snapshot = deepcopy(self.snapshot)
        del snapshot['manifest']
        with self.assertRaisesRegex(ValueError, 'manifest'):
            tool_schema(snapshot)
        self.assertFalse(prepare(self.source, self.annotations, {'incident-001': snapshot})['ready'])
        snapshot = deepcopy(self.snapshot)
        del snapshot['manifest']['clusters']['staging-eu']
        self.assertFalse(prepare(self.source, self.annotations, {'incident-001': snapshot})['ready'])

    def test_answer_format_remains_strict(self):
        execution = self.execution()
        execution['final_message'] = '调查完成。\n```json\n' + execution['final_message'] + '\n```'
        self.assertFalse(grade(self.case, execution)['passed'])

    def test_json_format_does_not_excuse_wrong_business_answer(self):
        from worker import OUTPUT_INSTRUCTION, request_options
        self.assertEqual(request_options()['response_format'], {'type': 'json_object'})
        for key in self.answer:
            self.assertIn(key, OUTPUT_INSTRUCTION)
        answer = {**self.answer, 'root_cause': 'staging-eu/checkout-api'}
        self.assertIn('root_cause 不符', grade(self.case, self.execution(answer))['errors'])

    def test_format_feedback_is_bounded_and_never_uses_business_grade(self):
        from worker import finish_report
        first = {'completed': True, 'final_response': '说明\n' + json.dumps(self.answer)}
        attempts = []
        def repair(text):
            attempts.append(text)
            return {'completed': True, 'final_response': '依然不是 JSON', 'api_calls': 1}
        result, record = finish_report(first, repair)
        self.assertEqual(attempts, [first['final_response']])
        self.assertFalse(record['format_valid'])
        self.assertEqual(result, first)
        self.assertFalse(record['content_preserved'])
        changed = {**self.answer, 'root_cause': 'new answer'}
        result, record = finish_report(first, lambda _: {'completed': True, 'final_response': json.dumps(changed)})
        self.assertEqual(result, first)
        self.assertFalse(record['content_preserved'])
        result, record = finish_report(first, lambda _: {'completed': True, 'final_response': json.dumps(self.answer)})
        self.assertTrue(record['content_preserved'])
        self.assertEqual(json.loads(result['final_response']), self.answer)
        wrong_but_json = {'completed': True, 'final_response': '{"root_cause": "wrong"}'}
        self.assertEqual(finish_report(wrong_but_json, repair), (wrong_but_json, None))
        interrupted = {**first, 'interrupted': True}
        self.assertEqual(finish_report(interrupted, repair), (interrupted, None))
        failed = {'completed': False, 'error': 'transport unavailable'}
        self.assertEqual(finish_report(first, lambda _: failed)[0], failed)
        self.assertEqual(len(attempts), 1)

    def test_correct_and_wrong_business_results(self):
        self.assertTrue(grade(self.case, self.execution())['passed'])
        for key, value in [('root_cause', 'staging-eu/checkout-api'), ('affected_service', 'staging-eu/checkout-api')]:
            self.assertFalse(grade(self.case, self.execution({**self.answer, key: value}))['passed'])

    def test_unread_unknown_and_forged_evidence(self):
        self.assertFalse(grade(self.case, self.execution(receipts=[]))['passed'])
        answer = {**self.answer, 'evidence_ids': self.answer['evidence_ids'] + ['missing-evidence']}
        self.assertFalse(grade(self.case, self.execution(answer))['passed'])
        receipts = deepcopy(self.receipts)
        receipts[0]['rows'][0]['value']['p99_ms'] = 1
        self.assertFalse(grade(self.case, self.execution(receipts=receipts))['passed'])

    def test_exclusion_evidence_must_be_observed_and_cited(self):
        self.assertTrue(grade(self.case, self.execution())['passed'])
        production_only = [r for r in self.receipts if r['query']['cluster'] == 'prod-eu']
        unread = grade(self.case, self.execution(receipts=production_only))
        self.assertFalse(unread['passed'])
        self.assertTrue(any('排除集群同名服务' in error for error in unread['errors']))
        answer = {**self.answer, 'evidence_ids': self.case['annotation']['required_evidence']}
        uncited = grade(self.case, self.execution(answer))
        self.assertFalse(uncited['passed'])
        self.assertTrue(any('排除集群同名服务' in error for error in uncited['errors']))

    def test_exclusion_cannot_replace_production_root_evidence(self):
        answer = {**self.answer, 'evidence_ids': self.case['annotation']['required_exclusion_evidence']}
        result = grade(self.case, self.execution(answer))
        self.assertFalse(result['passed'])
        self.assertTrue(any('目标集群' in error for error in result['errors']))

    def test_missing_or_misattributed_annotations_stay_pending(self):
        for field, value in (
            ('required_exclusion_evidence', None), ('required_exclusion_evidence', []),
            ('required_exclusion_evidence', ['alert-prod']),
            ('required_exclusion_evidence', ['log-staging', 'log-staging']),
            ('required_evidence', ['log-staging']),
        ):
            with self.subTest(field=field, value=value):
                annotations = deepcopy(self.annotations)
                annotations[self.case['id']][field] = value
                result = prepare(self.source, annotations, self.snapshots)
                self.assertFalse(result['ready'])
                self.assertTrue(any(field in reason for p in result['pending'] for reason in p['reasons']))
        snapshot = deepcopy(self.snapshot)
        next(r for r in snapshot['rows'] if r['id'] == 'log-staging')['service'] = 'another-api'
        self.assertFalse(prepare(self.source, self.annotations, {'incident-001': snapshot})['ready'])

    def test_nonempty_explanation_is_not_semantic_validation(self):
        nonempty = grade(self.case, self.execution({**self.answer, 'explanation': '这段文字不提供因果解释。'}))
        self.assertTrue(nonempty['explanation_nonempty'])
        self.assertTrue(nonempty['passed'])  # 仅字段与引用检查通过，不声称解释正确。
        self.assertEqual(nonempty['explanation_semantics'], 'not_evaluated')
        empty = grade(self.case, self.execution({**self.answer, 'explanation': '  '}))
        self.assertFalse(empty['explanation_nonempty'])
        self.assertFalse(empty['passed'])
        self.assertEqual(empty['explanation_semantics'], 'not_evaluated')

    def test_infrastructure_is_unscored(self):
        result = grade(self.case, {'status': 'infrastructure_error'})
        self.assertIsNone(result['score'])
        self.assertFalse(result['passed'])

    def test_pack_integrity_and_public_projection(self):
        runtime = HERE / 'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as tmp:
            folder = Path(tmp) / 'pack'
            pack = build(HERE / 'fixtures/rollouts.json', HERE / 'fixtures/annotations.json', HERE / 'fixtures/snapshots', folder)
            verify(pack)
            overview = inspect_pack(folder / 'pack.json')
            self.assertEqual(overview['pack_sha256'], pack['pack_sha256'])
            self.assertEqual(overview['splits'], {'dev': [self.case['id']], 'holdout': []})
            self.assertEqual(len(overview['pending']), 2)
            self.assertNotIn('expected_output', json.dumps(overview))
            public = folder / 'fixtures' / self.case['id']
            self.assertEqual({p.name for p in public.iterdir()}, {'snapshot.json', 'task.json'})
            self.assertNotIn('expected_output', ''.join(p.read_text() for p in public.iterdir()))
            self.assertNotIn('required_exclusion_evidence', ''.join(p.read_text() for p in public.iterdir()))
            previous = deepcopy(pack)
            previous['schema'] = 'oncall-pack-v1'
            previous['pack_sha256'] = digest({k: v for k, v in previous.items() if k != 'pack_sha256'})
            with self.assertRaisesRegex(ValueError, '评分语义版本不符'):
                verify(previous)
            pack['ready'][0]['input']['prompt'] = '修改过的题目'
            (folder / 'pack.json').write_text(json.dumps(pack))
            with self.assertRaises(ValueError):
                inspect_pack(folder / 'pack.json')

    def test_langfuse_mapping_and_version_roundtrip_with_test_double(self):
        # 这是传参检查，真实 SDK 与远端服务验收另列，不冒充联网。
        runtime = HERE / 'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as tmp:
            pack = build(HERE / 'fixtures/rollouts.json', HERE / 'fixtures/annotations.json', HERE / 'fixtures/snapshots', Path(tmp) / 'pack')
            now = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
            items = []

            class Client:
                def create_dataset(self, **kwargs):
                    pass

                def create_dataset_item(self, **kwargs):
                    items.append(SimpleNamespace(**kwargs, status='ACTIVE', source_observation_id=None,
                                                  dataset_id='demo', created_at=now, updated_at=now))
                    return items[-1]

                def get_dataset(self, name, *, version):
                    self.version = version
                    return SimpleNamespace(items=items)

            client = Client()
            published = publish(client, 'oncall-eval', pack)
            self.assertEqual(published['export_version_hint'], now.isoformat())
            self.assertEqual(published['items'][0]['expected_output'], self.case['annotation']['expected_output'])
            self.assertIsNone(items[0].source_trace_id)
            exported = export(client, 'oncall-eval', now.isoformat())
            self.assertEqual(client.version, now)
            recovered = restore(exported, self.snapshots, Path(tmp) / 'restored')
            verify(recovered)
            self.assertEqual(recovered['ready'][0]['annotation'], self.case['annotation'])
            self.assertEqual(recovered['ready'][0]['provenance']['dataset_version'], now.isoformat())
            legacy_export = deepcopy(exported)
            del legacy_export['items'][0]['metadata']['annotation']['required_exclusion_evidence']
            legacy = restore(legacy_export, self.snapshots, Path(tmp) / 'legacy')
            self.assertFalse(legacy['ready'])
            self.assertTrue(legacy['pending'])
            exported['items'][0]['expected_output'] = {'root_cause': 'wrong'}
            rejected = restore(exported, self.snapshots, Path(tmp) / 'rejected')
            self.assertEqual(rejected['ready'], [])
            self.assertTrue(rejected['pending'])


class DependencyTests(unittest.TestCase):
    def test_pinned_clean_worktree(self):
        commit = read(HERE / 'dependencies.json')['hermes_commit']
        with patch('engine.subprocess.check_output', side_effect=[commit + '\n', '']) as git:
            source, python = dependency()
        self.assertEqual(python, source / '.venv/bin/python')
        self.assertIn('--untracked-files=no', git.call_args.args[0])

    def test_tracked_changes_are_rejected_even_at_pinned_head(self):
        commit = read(HERE / 'dependencies.json')['hermes_commit']
        for status in (' M run_agent.py\n', 'M  run_agent.py\n', ' D run_agent.py\n'):
            with self.subTest(status=status), patch('engine.subprocess.check_output', side_effect=[commit, status]):
                with self.assertRaisesRegex(RuntimeError, '已跟踪文件存在改动'):
                    dependency()

    def test_wrong_head_is_rejected(self):
        with patch('engine.subprocess.check_output', return_value='0' * 40):
            with self.assertRaisesRegex(RuntimeError, '版本不符'):
                dependency()


if __name__ == '__main__':
    unittest.main(verbosity=2)
