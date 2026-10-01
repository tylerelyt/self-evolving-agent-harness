"""最小构造题库：输入、固定业务快照与独立预期分开保存。"""
from incidentctl import COMMAND_HELP


def incident(case_id, group, multiple=False, interference=False, incomplete=False):
    cluster = 'prod-eu'
    affected = ['catalog-api', 'checkout-api'] if multiple else ['checkout-api']
    def row(kind, suffix, **fields):
        return {'record_id': f'{case_id}-{suffix}', 'cluster': cluster, 'incident_id': case_id,
                'time_window': '2026-01-15T09:00:00Z/2026-01-15T09:05:00Z', 'kind': kind, **fields}
    alerts = [row('alert', 'A' + str(i), service=s, symptom='request_timeout') for i, s in enumerate(affected)]
    logs = [] if incomplete else [row('log', 'L', service='db-gateway', observation='database_connections_exhausted', role='root')]
    traces = [] if incomplete else [row('trace', 'T' + str(i), service=s, upstream='db-gateway', relation='timeout_from_upstream') for i, s in enumerate(affected)]
    required = [r['record_id'] for r in alerts + logs + traces]
    if interference:
        for collection in (alerts, logs, traces):
            collection.extend([{**r, 'record_id': r['record_id'] + '-STAGING', 'cluster': 'staging-eu',
                                'incident_id': 'unrelated-staging', 'newer': True} for r in list(collection)])
    return {'id': case_id, 'group': group,
            'task': f'调查 {cluster} 的 checkout-api 在 2026-01-15 09:00至09:05 UTC 的超时告警。' +
                    ('同一故障还出现 catalog-api 超时，请给出共同根因与全部受影响服务。' if multiple else '给出根因及受影响服务。'),
            'snapshot': {'snapshot_id': 'constructed-' + case_id, 'constructed': True, 'alerts': alerts, 'logs': logs, 'traces': traces},
            'expected': {'cluster': cluster, 'root_cause': None if incomplete else 'db-gateway',
                         'affected_services': affected, 'status': 'insufficient' if incomplete else 'confirmed'},
            'required_citations': required}


CASES = [incident('A01', 'development'), incident('A02', 'development', multiple=True),
         incident('A03', 'holdout', interference=True), incident('A04', 'holdout', incomplete=True)]


def public_input(item):
    return {'task': item['task'], 'system':
        '这是构造的只读告警诊断练习。先自行发现并读取适用的 Skill，再通过 ./incidentctl 查询。'
        + COMMAND_HELP +
        '只依据当前回执；信息不全时保留未知。返回一个 JSON 对象，字段语义如下：'
        'cluster 是目标集群ID；root_cause 是根因服务ID（查询记录中的 service 原值，不是故障描述），无法确认则为null；'
        'affected_services 是目标集群和时间窗口内实际出现告警记录的服务ID数组，不是所有依赖服务。'
        '根因服务仅在自身也出现告警时才计入该数组；'
        'status 为 confirmed 或 insufficient；citations 是实际查询所得的依据记录ID数组；'
        'explanation 用简短中文说明故障机制、上下游因果及仍需确认之处。'
        '优先输出纯JSON，不加额外解释或代码围栏；诊断内容与纯JSON格式分别检查。不改变方法、记忆或业务。'}
