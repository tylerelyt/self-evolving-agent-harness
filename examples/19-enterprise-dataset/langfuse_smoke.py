"""上传既有真实 Hermes 轨迹，以独立 pack-v3 标注重评分；不调用模型。

在本讲目录使用 .deps/sdk/bin/python -B langfuse_smoke.py --help 查看必填输入。
环境变量：LANGFUSE_BASE_URL、LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY。
本脚本不读取 .env。只创建独立测试 dataset 和新的本地输出目录，不覆盖原记录。
退出码 0 表示导入、版本与恢复核查通过，不表示原回答通过业务评分。

span 起止为导入时间，不能用于模型或工具性能分析。原始逐调用耗时未知；
只保留原评测报告已有的整例耗时，不补造 token、成本或 generation 记录。
"""
import argparse
from copy import deepcopy
from datetime import timezone
import importlib.metadata
import json
from pathlib import Path
import time
import uuid

from dataset import HERE, digest, file_digest, inside, read, save, verify
from grader import grade
import langfuse_io as io
from monitor import query, timestamp


SDK_VERSION = read(HERE / 'dependencies.json')['langfuse']
TIMING = {'span_timestamps': 'import_time', 'original_observation_duration_ms': None,
          'latency_interpretation': '导入耗时不是模型或工具时延；原始逐调用起止和耗时未知'}
ITEM_CONTENT = ('id', 'status', 'input', 'expected_output', 'metadata',
                'source_trace_id', 'source_observation_id', 'dataset_id', 'dataset_name')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tool_records(execution, snapshot):
    """按实际消息配对，不按预设查询顺序补造调用。"""
    pending, seen, records = {}, set(), []
    for message in execution['messages']:
        for call in message.get('tool_calls') or []:
            require(message['role'] == 'assistant' and call['id'] not in seen, '工具调用角色或编号无效')
            require(call['function']['name'] == 'monitor_query', '存在本脚本不支持的工具')
            seen.add(call['id'])
            pending[call['id']] = call['function']
        if message['role'] != 'tool':
            continue
        call = pending.pop(message['tool_call_id'], None)
        require(call is not None, '工具回执没有对应调用')
        arguments = json.loads(call['arguments'])
        receipt = json.loads(message['content'])
        require(query(snapshot, **arguments) == receipt, '工具回执与查询参数或快照不符')
        records.append({'call_id': message['tool_call_id'], 'arguments': arguments,
                        'arguments_json': call['arguments'], 'output': message['content'],
                        'receipt': receipt})
    require(not pending and records, '工具调用缺少回执或轨迹为空')
    require([r['receipt'] for r in records] == execution['receipts'], '消息和归档回执不一致')
    return records


def load_archive(execution_path, run, pack_path, original_pack_path):
    execution, pack, original = (read(p) for p in (execution_path, pack_path, original_pack_path))
    verify(pack)
    protocol = read(run / 'protocol.json')
    report = read(run / 'results/iteration-1/result.json')
    require(len(report['case_results']) == 1, '此验证入口只处理单题归档')
    result = report['case_results'][0]
    case = next(c for c in pack['ready'] if c['id'] == result['case_id'])
    old_case = next(c for c in original['ready'] if c['id'] == case['id'])
    require(digest({k: v for k, v in original.items() if k != 'pack_sha256'})
            == original['pack_sha256'] == protocol['pack_sha256'], '原运行与原评测包不匹配')
    require(case['input'] == old_case['input'] and case['snapshot'] == old_case['snapshot'],
            '重评分只能改变评分口径，不能改变原题面或快照')
    require(result['prompt'] == case['input']['prompt'] and result['response'] == execution['final_message'],
            '原运行报告与执行记录的输入或输出不符')
    require(execution['model_called'] is True and execution['status'] == 'completed', '不是已完成的真实模型执行')
    require(execution['messages'][-1]['role'] == 'assistant'
            and execution['messages'][-1]['content'] == execution.get('investigation_final_message', execution['final_message']),
            '调查消息与归档输出不符')
    repair = execution.get('format_repair')
    if repair and repair.get('content_preserved'):
        require(repair['messages'][-1]['content'] == execution['final_message'], '格式修正消息与最终输出不符')
    require([m['content'] for m in execution['messages'] if m['role'] == 'user']
            == [execution.get('submitted_prompt', case['input']['prompt'])], '消息中的题面不一致')
    original_scores = [json.loads(a['evidence']) for a in result['grading']['assertion_results']
                       if a['text'].startswith('script:')]
    require(len(original_scores) == 1, '原运行必须有唯一脚本评分记录')
    current_grade = grade(case, execution)
    sources = {'execution': execution_path, 'run_result': run / 'results/iteration-1/result.json',
               'run_protocol': run / 'protocol.json', 'original_pack': original_pack_path, 'annotation_pack': pack_path}
    source_hashes = {key: file_digest(path) for key, path in sources.items()}
    incident_id, cluster = case['id'].split('@')
    metadata = {'operation': 'archived_real_trajectory_regrade', 'new_model_calls': 0,
                'incident_id': incident_id, 'cluster': cluster, 'split': case['split'],
                'scenario': case['scenario'], 'snapshot_id': case['snapshot']['snapshot_id'],
                'business_data': 'constructed_public_fixture_not_production_incident',
                'run_id': run.name, 'source_hashes': source_hashes,
                'original_pack_sha256': original['pack_sha256'], 'annotation_pack_sha256': pack['pack_sha256'],
                'original_model': protocol['model'], 'original_api_calls': execution['api_calls'],
                'original_run_start': report['start_time'], 'original_run_end': report['end_time'],
                'original_case_duration_ms': result['duration_ms'], 'original_status': result['status'],
                'original_grade': original_scores[0], 'original_scorer_version': original['versions'].get('scorer_version'),
                'annotation_origin': 'independent_reviewed_teaching_annotation_in_pack_v3', **TIMING}
    return {'execution': execution, 'pack': pack, 'case': case, 'metadata': metadata,
            'tools': tool_records(execution, case['snapshot']), 'local_grade': current_grade}


def upload_execution(lf, archive, trace_id):
    """只用 SDK 的 OpenTelemetry 入口；不使用旧 ingestion.batch。"""
    execution, case = archive['execution'], archive['case']
    root = lf.start_observation(name='lesson19-archived-hermes', as_type='agent',
        trace_context={'trace_id': trace_id}, input=case['input'], output=execution['final_message'],
        metadata={**archive['metadata'], 'archived_messages': execution['messages'],
                  'format_repair': execution.get('format_repair')})
    manifest = {'trace_id': trace_id, 'root_id': root.id, 'tools': []}
    try:
        root.set_trace_io(input=case['input'], output=execution['final_message'])
        for index, record in enumerate(archive['tools']):
            observation = root.start_observation(name='monitor_query', as_type='tool',
                input=record['arguments'], output=record['receipt'],
                level='WARNING' if 'error' in record['receipt'] else 'DEFAULT',
                metadata={**TIMING, 'sequence': index, 'original_call_id': record['call_id'],
                          'original_arguments_json': record['arguments_json'],
                          'original_output_text': record['output']})
            observation.end()
            manifest['tools'].append({'id': observation.id, 'call_id': record['call_id']})
    finally:
        root.end()
    lf.flush()
    return manifest


def wait_for(label, fetch, ready, deadline, interval):
    """统一截止时间；只重试可暂时恢复的读取错误，不重试写操作。"""
    from httpx import TransportError
    from langfuse.api.core.api_error import ApiError
    attempts, last = 0, '尚不可见或内容未齐'
    while time.monotonic() < deadline:
        attempts += 1
        try:
            value = fetch()
            if ready(value):
                return value
        except ApiError as exc:
            if exc.status_code not in (404, 429, 500, 502, 503, 504):
                raise
            last = 'HTTP ' + str(exc.status_code)
        except TransportError as exc:
            last = type(exc).__name__
        except LookupError as exc:
            last = str(exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    raise TimeoutError(f'{label}：轮询 {attempts} 次后超时（{last}）；未确认成功')


def trace_ready(collected, archive, manifest):
    trace = collected['traces'][0]
    if trace['input'] != archive['case']['input'] or trace['output'] != archive['execution']['final_message']:
        return False
    observations = {o['id']: o for o in trace['raw_trace']['observations']}
    if set(observations) != {manifest['root_id'], *(t['id'] for t in manifest['tools'])}:
        return False
    root = observations[manifest['root_id']]
    if root.get('output') != archive['execution']['final_message'] or root.get('type') != 'AGENT':
        return False
    for item, record in zip(manifest['tools'], archive['tools'], strict=True):
        observation = observations[item['id']]
        if (json.loads(observation['input']) != record['arguments'] or json.loads(observation['output']) != record['receipt']
                or observation.get('parentObservationId') != manifest['root_id']
                or observation.get('type') != 'TOOL'
                or (observation.get('metadata') or {}).get('original_call_id') != record['call_id']):
            return False
    return True


def linked_pack(archive, collected):
    pack = deepcopy(archive['pack'])
    case = deepcopy(archive['case'])
    case['provenance'] = {'origin': collected['origin'], 'trace_ids': [collected['traces'][0]['id']],
        'rollout_sha256': digest(collected['traces']), 'fixture_provenance': case['provenance'],
        'archive': archive['metadata']}
    pack['ready'] = [case]
    pack['versions']['annotation_pack_sha256'] = archive['pack']['pack_sha256']
    pack.pop('pack_sha256')
    pack['pack_sha256'] = digest(pack)
    return pack


def item_content(item):
    return deepcopy({key: item.get(key) for key in ITEM_CONTENT})


def check_versions(old, old_again, latest):
    # v4 按版本恢复内容，但 updated_at 仍可能是条目最近一次修改的时间。
    require([item_content(i) for i in old_again['items']] == [item_content(i) for i in old['items']],
            '固定旧时间点的导出内容发生变化')
    require(len(latest) == 1, '测试 dataset 不再只有本次条目')
    before, after = item_content(old['items'][0]), item_content(latest[0])
    require(after['metadata'].pop('smoke_metadata_revision', None) == 2, '最新版未出现第二版标记')
    require(after == before, '第二版除 metadata 标记以外的内容发生变化')


def roundtrip(lf, archive, output, timeout, interval):
    deadline = time.monotonic() + timeout
    trace_id = uuid.uuid4().hex
    name = 'lesson19-smoke-' + trace_id
    # 随机新名称仍检查一次，避免 create_dataset 的 upsert 覆盖已存在的测试集。
    from langfuse.api import NotFoundError
    try:
        lf.get_dataset(name=name)
    except NotFoundError:
        pass
    else:
        raise ValueError('测试 dataset 已存在，拒绝复用')
    save(output / 'identity.json', {'trace_id': trace_id, 'dataset_name': name, 'source': archive['metadata']})
    manifest = upload_execution(lf, archive, trace_id)
    save(output / 'observations.json', manifest)
    collected = wait_for('Trace 与全部工具回执入库', lambda: io.collect(lf, [trace_id]),
                         lambda value: trace_ready(value, archive, manifest), deadline, interval)
    save(output / 'collected.json', collected)
    pack = linked_pack(archive, collected)
    save(output / 'linked-pack.json', pack)
    published = io.publish(lf, name, pack)
    save(output / 'published.json', published)
    require(len(published['items']) == 1, '发布条目数量不符')
    item = published['items'][0]
    # 用服务器返回的时间，避免本机与服务端时钟偏差；固定后不再移动时间点。
    version = timestamp(item['updated_at']).astimezone(timezone.utc).isoformat()
    old = wait_for('固定第一版导出', lambda: io.export(lf, name, version),
        lambda value: len(value['items']) == 1 and item_content(value['items'][0]) == item_content(item),
        deadline, interval)
    save(output / 'export-v1.json', old)
    snapshots = {archive['case']['snapshot']['snapshot_id']: archive['case']['snapshot']}
    first = io.restore(old, snapshots, output / 'restored-v1')

    revised_metadata = {**item['metadata'], 'smoke_metadata_revision': 2}
    revision = lf.create_dataset_item(dataset_name=name, id=item['id'], input=item['input'],
        expected_output=item['expected_output'], metadata=revised_metadata,
        source_trace_id=item['source_trace_id'], source_observation_id=item.get('source_observation_id'))
    save(output / 'revision-v2.json', io.model_json(revision))
    require(revision.updated_at > timestamp(version), '两版时间点没有分开，不能验证版本隔离')
    latest = wait_for('第二版 metadata 入库',
        lambda: [io.dataset_item_json(i) for i in lf.get_dataset(name=name).items],
        lambda items: len(items) == 1 and items[0]['metadata'].get('smoke_metadata_revision') == 2,
        deadline, interval)
    old_again = wait_for('重读固定第一版', lambda: io.export(lf, name, version),
                        lambda value: [item_content(i) for i in value['items']]
                        == [item_content(i) for i in old['items']], deadline, interval)
    check_versions(old, old_again, latest)
    save(output / 'latest-v2.json', latest)
    save(output / 'export-v1-again.json', old_again)
    restored = io.restore(old_again, snapshots, output / 'restored-v1-again')
    verify(restored)
    require(first['ready'] == restored['ready'] and not first['pending'] and not restored['pending'],
            '旧时间点恢复内容不一致或仍有待标注项')
    require(len(restored['ready']) == 1, '恢复题数不符')
    case = restored['ready'][0]
    require(all(case[key] == archive['case'][key] for key in ('input', 'snapshot', 'annotation')),
            '恢复的题面、快照或独立标注与 pack-v3 不同')
    require(case['provenance']['source_trace_id'] == trace_id, '恢复后丢失真实 Trace 关联')
    result = grade(case, archive['execution'])
    require(result == archive['local_grade'], '远端往返改变了原执行的评分')
    report = {'integration_passed': True, 'operation': 'archived_real_trajectory_regrade',
              'new_model_calls': 0, 'sdk_version': SDK_VERSION, 'trace_id': trace_id,
              'dataset_name': name, 'dataset_version': version, 'tool_observations': len(manifest['tools']),
              'metadata_only_revision_verified': True, 'old_version_restore_identical': True,
              'original_grade': archive['metadata']['original_grade'], 'regrade': result, **TIMING}
    save(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution', type=Path, required=True, help='原始 Hermes execution.json')
    parser.add_argument('--run', type=Path, required=True, help='对应的单题 SkillUp 运行目录')
    parser.add_argument('--pack', type=Path, required=True, help='独立标注及当前评分口径的评测包')
    parser.add_argument('--original-pack', type=Path, required=True, help='原运行实际使用的评测包')
    parser.add_argument('--out', type=Path, default=HERE / ('runtime/langfuse-smoke-' + uuid.uuid4().hex[:12]))
    parser.add_argument('--wait-seconds', type=float, default=1200, help='全部读取轮询共用的截止时间，最多 1800 秒')
    parser.add_argument('--poll-seconds', type=float, default=3)
    parser.add_argument('--apply', action='store_true', help='明确允许写入 Langfuse；不会调用模型')
    args = parser.parse_args()
    if not args.apply:
        parser.error('远端写入需要 --apply；--help 和离线测试不会访问实例')
    require(0 < args.wait_seconds <= 1800 and 0 < args.poll_seconds <= 30, '轮询时间参数超出上限')
    require(importlib.metadata.version('langfuse') == SDK_VERSION, '必须使用 langfuse==' + SDK_VERSION)
    output = inside(args.out)
    require(not output.exists(), '输出目录已存在，不能覆盖')
    for protected in (args.run, args.execution.parent, args.pack.parent, args.original_pack.parent):
        require(not output.is_relative_to(protected.resolve()), '输出不能位于原运行或评测包内')
    archive = load_archive(args.execution, args.run, args.pack, args.original_pack)
    files = {p for p in args.run.rglob('*') if p.is_file()} | {args.execution, args.pack, args.original_pack}
    hashes = {p: file_digest(p) for p in files}
    lf = io.client()
    try:
        report = roundtrip(lf, archive, output, args.wait_seconds, args.poll_seconds)
        require(all(file_digest(p) == value for p, value in hashes.items()), '原始文件在验证期间发生变化')
        report['original_files_unchanged'] = True
        save(output / 'report.json', report)
        print(json.dumps({'out': str(output), **report}, ensure_ascii=False))
    finally:
        lf.shutdown()


if __name__ == '__main__':
    main()
