"""查询只读故障现场；不保存或重放某个 Agent 的调用顺序。"""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

KINDS = ('alerts', 'metrics', 'logs', 'traces', 'dependencies')


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('时间必须带时区')
    return result


def scope(snapshot):
    """覆盖范围由采集时的 manifest 声明，不能从非空记录倒推。"""
    clusters = snapshot.get('manifest', {}).get('clusters')
    if not isinstance(clusters, dict) or not clusters:
        raise ValueError('快照缺少显式的集群覆盖 manifest')
    for cluster, kinds in clusters.items():
        if (not isinstance(cluster, str) or not cluster or not isinstance(kinds, list) or not kinds
                or any(kind not in KINDS for kind in kinds) or len(kinds) != len(set(kinds))):
            raise ValueError('manifest 须按集群列出已完整采集的数据类型')
    coverage = snapshot['coverage']
    if coverage.get('complete') is not True or timestamp(coverage['start']) > timestamp(coverage['end']):
        raise ValueError('快照覆盖范围尚未确认')
    if any(row['cluster'] not in clusters or row['kind'] not in clusters[row['cluster']]
           for row in snapshot['rows']):
        raise ValueError('快照记录超出 manifest 声明的覆盖范围')
    return {'start': coverage['start'], 'end': coverage['end'], 'clusters': deepcopy(clusters)}


def query(snapshot, kind, cluster, start, end, service=None):
    if kind not in KINDS or not cluster:
        raise ValueError('须指定合法数据类型与集群')
    begin, finish = timestamp(start), timestamp(end)
    if begin > finish:
        raise ValueError('查询开始时间不能晚于结束时间')
    available = scope(snapshot)
    receipt = {'snapshot_id': snapshot['snapshot_id'], 'snapshot_version': snapshot['version'],
               'query': dict(kind=kind, cluster=cluster, start=start, end=end, service=service),
               'scope': available}
    if (kind not in available['clusters'].get(cluster, [])
            or not timestamp(available['start']) <= begin <= finish <= timestamp(available['end'])):
        return {**receipt, 'status': 'not_covered', 'error': 'not_covered',
                'message': '快照未覆盖所请求的集群、数据类型或时间范围，不能据此排除故障。请查看 scope。'}
    rows = [row for row in snapshot['rows'] if row['kind'] == kind
            and row['cluster'] == cluster and (service is None or row['service'] == service)
            and begin <= timestamp(row['time']) <= finish]
    return {**receipt, 'status': 'covered', 'rows': rows}


SCHEMA = {
    'name': 'monitor_query',
    'description': '查询固定历史快照中的告警、指标、日志、调用链或依赖。所有时间需带时区；必须指定集群，可按服务过滤。cluster 枚举来自快照覆盖 manifest。回执 scope 列出可查询范围；not_covered 不表示没有异常，covered 且 rows 为空只表示覆盖范围内没有匹配记录。',
    'parameters': {'type': 'object', 'properties': {
        'kind': {'type': 'string', 'enum': list(KINDS)}, 'cluster': {'type': 'string'},
        'service': {'type': 'string'}, 'start': {'type': 'string'}, 'end': {'type': 'string'}},
        'required': ['kind', 'cluster', 'start', 'end'], 'additionalProperties': False}}


def tool_schema(snapshot):
    schema = deepcopy(SCHEMA)
    schema['parameters']['properties']['cluster']['enum'] = sorted(scope(snapshot)['clusters'])
    return schema


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('kind', choices=KINDS)
    for name in ('cluster', 'start', 'end'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--service')
    args = vars(parser.parse_args())
    snapshot = json.loads(args.pop('snapshot').read_text())
    print(json.dumps(query(snapshot, **args), ensure_ascii=False, indent=2))
