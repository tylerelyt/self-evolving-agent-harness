"""官方 SDK 收集 Trace、发布用例和按时间戳导出；不从旧输出猜答案。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.metadata
import os
from pathlib import Path
import uuid

from dataset import HERE, digest, inside, read, save, verify
from monitor import timestamp
from schema import PACK_SCHEMA, SCORER_VERSION


def client():
    if importlib.metadata.version('langfuse') != read(HERE / 'dependencies.json')['langfuse']:
        raise RuntimeError('请使用本讲固定 Langfuse SDK 环境')
    if not all(os.environ.get(k) for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')):
        raise RuntimeError('缺少 Langfuse 凭据或项目地址；没有连接外部服务')
    from langfuse import Langfuse
    return Langfuse(public_key=os.environ['LANGFUSE_PUBLIC_KEY'], secret_key=os.environ['LANGFUSE_SECRET_KEY'],
                    base_url=os.environ['LANGFUSE_BASE_URL'])


def model_json(model):
    import json
    return json.loads(model.json(by_alias=False))


def dataset_item_json(item):
    # get_dataset 返回 DatasetItemClient，不是提供 .json() 的 Pydantic 模型。
    fields = ('id', 'status', 'input', 'expected_output', 'metadata', 'source_trace_id',
              'source_observation_id', 'dataset_id', 'dataset_name', 'created_at', 'updated_at')
    result = {key: getattr(item, key) for key in fields}
    for key in ('created_at', 'updated_at'):
        result[key] = result[key].isoformat()
    result['status'] = getattr(item.status, 'value', item.status)
    return result


def collect(lf, trace_ids):
    import json
    traces = []
    def observation_dict(item):
        return item if isinstance(item, dict) else json.loads(item.json(by_alias=True))
    for trace_id in sorted(set(trace_ids)):
        observations, cursor, cursors = [], None, set()
        while True:
            options = dict(trace_id=trace_id, limit=100, cursor=cursor,
                           fields='core,basic,time,io,metadata', parse_io_as_json=False)
            page = lf.api.observations.get_many(**options)
            rows = [observation_dict(item) for item in page.data]
            keys = sorted({key for item in rows for key in (item.get('metadata') or {})})
            if keys:
                page = lf.api.observations.get_many(**options, expand_metadata=','.join(keys))
                rows = [observation_dict(item) for item in page.data]
            observations.extend(rows)
            cursor = page.meta.cursor
            if not cursor:
                break
            if cursor in cursors or len(cursors) >= 100:
                raise RuntimeError('Observations 分页没有正常结束，拒绝保存不完整记录')
            cursors.add(cursor)
        ids = {item['id'] for item in observations}
        # SDK 的显式 trace_context 可以附带一个未上传的远端父 span。
        roots = [item for item in observations if item.get('parentObservationId') not in ids]
        if not roots:
            raise LookupError('Trace 的根 Observation 尚未入库')
        # 多轮 Trace 保留全部记录，由显式业务映射确定完整题面，不猜哪一轮算任务。
        root = roots[0] if len(roots) == 1 else {}
        trace_input = root.get('input')
        if isinstance(trace_input, str):
            try:
                trace_input = json.loads(trace_input)
            except ValueError:
                pass
        traces.append({'id': trace_id, 'input': trace_input, 'output': root.get('output'),
                       'metadata': root.get('metadata') or {},
                       'root_observation_ids': [item['id'] for item in roots],
                       'raw_trace': {'source': 'observations_v2', 'observations': observations}})
    return {'origin': {'kind': 'langfuse', 'collected_at': datetime.now(timezone.utc).isoformat()}, 'traces': traces}


def map_tasks(source, tasks):
    """把业务层确认的调查题面与事故归属关联到采集记录，保留原记录。"""
    if source.get('origin', {}).get('kind') != 'langfuse':
        raise ValueError('业务映射只接受 Langfuse 采集记录，不能将构造数据伪装为在线采集')
    ids = [trace['id'] for trace in source['traces']]
    if len(ids) != len(set(ids)) or not isinstance(tasks, dict) or set(tasks) != set(ids):
        raise ValueError('映射必须与本次采集的 Trace ID 逐一对应，不能漏项或混入其他任务')
    result = deepcopy(source)
    for trace in result['traces']:
        task = tasks[trace['id']]
        if not isinstance(task, dict) or set(task) != {'input', 'metadata'}:
            raise ValueError('任务映射只接受 input 和 metadata，标准答案须另作独立标注')
        value = task['input']
        if (not isinstance(value, dict) or set(value) != {'prompt'}
                or not isinstance(value['prompt'], str) or not value['prompt'].strip()
                or not isinstance(task['metadata'], dict)):
            raise ValueError('须明确提供完整调查的 prompt 和业务 metadata')
        trace['before_task_mapping'] = {'input': deepcopy(trace.get('input')),
                                        'metadata': deepcopy(trace.get('metadata'))}
        trace['input'] = deepcopy(value)
        trace['metadata'] = {**(trace.get('metadata') or {}), **deepcopy(task['metadata'])}
    result['origin']['task_mapping_sha256'] = digest(tasks)
    return result


def publish(lf, dataset_name, pack):
    verify(pack)
    lf.create_dataset(name=dataset_name, metadata={'course_schema': PACK_SCHEMA})
    receipts = []
    for case in pack['ready']:
        item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, dataset_name + '/' + case['id']))
        metadata = {'case_id': case['id'], 'split': case['split'], 'scenario': case['scenario'],
                    'snapshot_id': case['snapshot']['snapshot_id'], 'snapshot_sha256': digest(case['snapshot']),
                    'snapshot_version': case['snapshot']['version'], 'annotation': case['annotation'],
                    'provenance': case['provenance'], 'pack_sha256': pack['pack_sha256'], 'versions': pack['versions']}
        # 教学 Trace ID 不存在于远端项目，只放 metadata，不能伪造原生 Trace 关联。
        source_trace = case['provenance']['trace_ids'][0] if case['provenance']['origin']['kind'] == 'langfuse' else None
        item = lf.create_dataset_item(dataset_name=dataset_name, id=item_id, input=case['input'],
                    expected_output=case['annotation']['expected_output'], metadata=metadata, source_trace_id=source_trace)
        receipts.append(dataset_item_json(item))
    return {'dataset_name': dataset_name, 'items': receipts, 'pending_not_uploaded': len(pack['pending']),
            'export_version_hint': max((item['updated_at'] for item in receipts), default=None)}


def export(lf, name, version):
    at = timestamp(version).astimezone(timezone.utc)
    dataset = lf.get_dataset(name=name, version=at)
    return {'schema': 'langfuse-export-v1', 'dataset_name': name, 'requested_version': at.isoformat(),
            'dataset_id': getattr(dataset, 'id', None), 'project_id': getattr(dataset, 'project_id', None),
            'exported_at': datetime.now(timezone.utc).isoformat(), 'items': [dataset_item_json(i) for i in dataset.items]}


def restore(exported, snapshots, output):
    return write_restored_pack(exported, snapshots, inside(output))


def write_restored_pack(exported, snapshots, output):
    """仅恢复标注和快照版本完整的条目；其他条目留下待处理原因。"""
    timestamp(exported['requested_version'])
    ready, pending = [], []
    for item in exported['items']:
        meta = item.get('metadata') or {}
        annotation = meta.get('annotation') or {}
        snapshot = snapshots.get(meta.get('snapshot_id'))
        if str(item.get('status', '')).upper() == 'ARCHIVED':
            continue
        reasons = []
        if not snapshot or digest(snapshot) != meta.get('snapshot_sha256'):
            reasons.append('快照缺失或内容版本不符')
        if item.get('expected_output') != annotation.get('expected_output') or not annotation.get('basis'):
            reasons.append('独立标注缺失或与远端答案不一致')
        if not meta.get('provenance') or not meta.get('versions'):
            reasons.append('缺少来源或评分版本')
        if reasons:
            pending.append({'item_id': item['id'], 'reasons': reasons})
            continue
        from dataset import prepare
        incident, cluster = meta['case_id'].split('@')
        source = {'origin': meta['provenance']['origin'], 'traces': [
            {'id': i, 'input': item['input'], 'metadata': {'incident_id': incident, 'cluster': cluster,
                'snapshot_id': meta['snapshot_id'], 'split': meta['split'], 'scenario': meta['scenario']}}
            for i in meta['provenance']['trace_ids']]}
        checked = prepare(source, {meta['case_id']: annotation}, {meta['snapshot_id']: snapshot})
        pending.extend(checked['pending'])
        for case in checked['ready']:
            case['provenance'] = {**meta['provenance'], 'dataset_name': exported['dataset_name'],
                'dataset_id': exported.get('dataset_id'), 'project_id': exported.get('project_id'),
                'dataset_version': exported['requested_version'], 'dataset_item_id': item['id'],
                'source_trace_id': item.get('source_trace_id'), 'source_observation_id': item.get('source_observation_id')}
            case['imported_versions'] = meta['versions']
            ready.append(case)
    from dataset import file_digest
    pack = {'schema': PACK_SCHEMA, 'ready': ready, 'pending': pending,
            'versions': {'export_sha256': digest(exported), 'dataset_version': exported['requested_version'],
                         'scorer_version': SCORER_VERSION, 'schema_sha256': file_digest(HERE / 'schema.py'),
                         'scorer_sha256': file_digest(HERE / 'grader.py'), 'monitor_sha256': file_digest(HERE / 'monitor.py')}}
    pack['pack_sha256'] = digest(pack)
    output = Path(output)
    if output.exists():
        raise FileExistsError('恢复目录已存在，不能覆盖旧版本')
    save(output / 'pack.json', pack)
    save(output / 'pending.json', pending)
    for case in ready:
        save(output / 'fixtures' / case['id'] / 'snapshot.json', case['snapshot'])
        save(output / 'fixtures' / case['id'] / 'task.json', case['input'])
    return pack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('collect'); p.add_argument('--trace-id', action='append', required=True)
    p = subs.add_parser('map'); p.add_argument('--source', type=Path, required=True)
    p.add_argument('--tasks', type=Path, required=True)
    p = subs.add_parser('publish'); p.add_argument('--pack', type=Path, required=True); p.add_argument('--dataset', required=True)
    p.add_argument('--apply', action='store_true', help='明确允许写入远端数据集')
    p = subs.add_parser('export'); p.add_argument('--dataset', required=True); p.add_argument('--version', required=True)
    p = subs.add_parser('restore'); p.add_argument('--export', dest='export_file', type=Path, required=True)
    p.add_argument('--snapshots', type=Path, required=True)
    for p in subs.choices.values():
        p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    output = inside(args.out)
    if output.exists():
        parser.error('输出已存在，请使用新路径')
    if args.command == 'map':
        save(output, map_tasks(read(args.source), read(args.tasks)))
        return
    if args.command == 'restore':
        restore(read(args.export_file), {p.stem: read(p) for p in args.snapshots.glob('*.json')}, output)
        return
    if args.command == 'publish' and not args.apply:
        parser.error('写入远端必须显式提供 --apply')
    lf = client()
    try:
        if args.command == 'collect':
            value = collect(lf, args.trace_id)
        elif args.command == 'publish':
            value = publish(lf, args.dataset, read(args.pack))
        else:
            value = export(lf, args.dataset, args.version)
        save(output, value)
    finally:
        lf.shutdown()


if __name__ == '__main__':
    main()
