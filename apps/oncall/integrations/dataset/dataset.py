"""将按事故归组的 Rollout、快照和独立标注编成固定评测包。"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

from monitor import KINDS, scope, timestamp
from schema import PACK_SCHEMA, SCORER_VERSION, annotation_errors

HERE = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(path):
    path = Path(path).resolve()
    if not path.is_relative_to(HERE):
        raise ValueError('数据集输出必须位于集成模块目录内')
    return path


def prepare(source, annotations, snapshots):
    groups = defaultdict(list)
    for trace in source['traces']:
        meta = trace.get('metadata') or {}
        groups[(meta.get('incident_id'), meta.get('cluster'))].append(trace)
    ready, pending = [], []
    for (incident, cluster), traces in sorted(groups.items(), key=lambda item: str(item[0])):
        key = f'{incident}@{cluster}'
        reasons = []
        meta = traces[0].get('metadata') or {}
        annotation = annotations.get(key, {})
        snapshot_id = meta.get('snapshot_id', '')
        snapshot = snapshots.get(snapshot_id)
        if not all(isinstance(x, str) and re.fullmatch(r'[a-zA-Z0-9_-]+', x) for x in (incident, cluster)):
            reasons.append('缺少事故编号或集群')
        signatures = {digest({'input': t.get('input'), 'snapshot': (t.get('metadata') or {}).get('snapshot_id'),
                              'split': (t.get('metadata') or {}).get('split')}) for t in traces}
        if len(signatures) != 1:
            reasons.append('同一事故输入、快照或分组冲突，须人工整理完整调查')
        task = traces[0].get('input')
        if not isinstance(task, dict) or set(task) != {'prompt'} or not isinstance(task['prompt'], str) or not task['prompt'].strip():
            reasons.append('须将完整调查输入映射为仅含 prompt 的对象')
        if meta.get('split') not in ('dev', 'holdout'):
            reasons.append('未确定开发或留出分组')
        if not snapshot:
            reasons.append('缺少故障快照')
        else:
            if snapshot.get('incident_id') != incident or snapshot.get('snapshot_id') != snapshot_id:
                reasons.append('快照事故归属不符')
            if not snapshot.get('version') or snapshot.get('coverage', {}).get('complete') is not True:
                reasons.append('快照版本或完整性尚未确认')
            rows = snapshot.get('rows', [])
            try:
                covered = scope(snapshot)
                if not set(KINDS) <= set(covered['clusters'].get(cluster, [])):
                    reasons.append('目标集群的告警、指标、日志、调用链、依赖未完整覆盖')
            except (KeyError, TypeError, ValueError) as exc:
                reasons.append('快照覆盖声明无效：' + str(exc))
            ids = [r.get('id') for r in rows]
            if len(ids) != len(set(ids)) or None in ids:
                reasons.append('证据编号重复或缺失')
            try:
                start, end = (timestamp(snapshot['coverage'][k]) for k in ('start', 'end'))
                if start > end or any(not start <= timestamp(r['time']) <= end for r in rows):
                    reasons.append('快照时间范围不一致')
            except (KeyError, TypeError, ValueError):
                reasons.append('快照时间缺失或无效')
        basis = annotation.get('basis') or []
        if (annotation.get('status') != 'reviewed' or not annotation.get('reviewer') or not basis
                or any(b.get('kind') not in ('repair_record', 'incident_timeline', 'independent_review')
                       or not b.get('ref') or not b.get('text') for b in basis)):
            reasons.append('没有已复核的独立标注依据；原 Agent 输出不能补作答案')
        if snapshot:
            if annotation.get('snapshot_id') != snapshot_id or annotation.get('snapshot_version') != snapshot.get('version'):
                reasons.append('标注没有绑定本快照版本')
            reasons.extend(annotation_errors(annotation, snapshot, cluster))
        provenance = {'trace_ids': sorted({t['id'] for t in traces}), 'origin': source['origin'],
                      'rollout_sha256': digest(traces), 'skill_versions': sorted({(t.get('metadata') or {}).get('skill_version', 'unknown') for t in traces})}
        if reasons:
            pending.append({'case_id': key, 'reasons': reasons, 'provenance': provenance})
            continue
        ready.append({'id': key, 'input': task, 'snapshot': snapshot, 'annotation': annotation,
                      'split': meta['split'], 'scenario': meta.get('scenario', 'unclassified'),
                      'provenance': provenance})
    return {'schema': PACK_SCHEMA, 'ready': ready, 'pending': pending}


def build(source_path, annotations_path, snapshots_dir, output):
    return write_pack(source_path, annotations_path, snapshots_dir, inside(output))


def write_pack(source_path, annotations_path, snapshots_dir, output):
    """Write the same verified pack into a caller-owned run directory."""
    output = Path(output)
    if output.exists():
        raise FileExistsError('评测包目录已存在，请使用新目录，不能覆盖旧版本')
    snapshots = {p.stem: read(p) for p in Path(snapshots_dir).glob('*.json')}
    pack = prepare(read(source_path), read(annotations_path), snapshots)
    pack['versions'] = {'source_sha256': file_digest(source_path), 'annotations_sha256': file_digest(annotations_path),
                        'scorer_version': SCORER_VERSION, 'schema_sha256': file_digest(HERE / 'schema.py'),
                        'scorer_sha256': file_digest(HERE / 'grader.py'), 'monitor_sha256': file_digest(HERE / 'monitor.py')}
    pack['pack_sha256'] = digest(pack)
    save(output / 'pack.json', pack)
    save(output / 'pending.json', pack['pending'])
    for case in pack['ready']:
        public = output / 'fixtures' / case['id']
        save(public / 'snapshot.json', case['snapshot'])
        save(public / 'task.json', case['input'])
    return pack


def verify(pack):
    unsigned = {k: v for k, v in pack.items() if k != 'pack_sha256'}
    if digest(unsigned) != pack['pack_sha256']:
        raise ValueError('评测包内容已变化，应重新生成版本')
    if pack['schema'] != PACK_SCHEMA or pack['versions'].get('scorer_version') != SCORER_VERSION:
        raise ValueError('评测包或评分语义版本不符，不能沿用旧评分口径')
    for key, filename in [('scorer_sha256', 'grader.py'), ('monitor_sha256', 'monitor.py'), ('schema_sha256', 'schema.py')]:
        if pack['versions'][key] != file_digest(HERE / filename):
            raise ValueError('评分器、标注结构或快照查询代码已变化，应重新生成评测包并重评各版')


def inspect_pack(path):
    pack = read(path)
    verify(pack)
    return {'status': 'pack_checked', 'pack_sha256': pack['pack_sha256'], 'versions': pack['versions'],
            'splits': {split: [case['id'] for case in pack['ready'] if case['split'] == split]
                       for split in ('dev', 'holdout')},
            'pending': [{'case_id': case['case_id'], 'reasons': case['reasons']} for case in pack['pending']]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=HERE / 'fixtures/rollouts.json')
    parser.add_argument('--annotations', type=Path, default=HERE / 'fixtures/annotations.json')
    parser.add_argument('--snapshots', type=Path, default=HERE / 'fixtures/snapshots')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--out', type=Path, help='将 Rollout 编为新题包')
    mode.add_argument('--inspect', type=Path, metavar='PACK', help='只核对已有 pack.json 并列出题号和待补原因')
    args = parser.parse_args()
    if args.inspect:
        try:
            result = inspect_pack(args.inspect)
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        result = build(args.source, args.annotations, args.snapshots, args.out)
        print(json.dumps({'ready': len(result['ready']), 'pending': len(result['pending']),
                          'pack_sha256': result['pack_sha256']}, ensure_ascii=False))
