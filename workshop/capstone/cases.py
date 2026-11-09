"""课程快照变体。题面、现场、独立评分依据分开供给。"""
from dataset import make_case


def case(case_id, group, *, multiple=False, interference=False, incomplete=False,
         other_incident=False):
    key = case_id.lower()
    cluster, service, upstream = f'prod-{key}', f'pay-{key}', f'db-{key}'
    item = make_case(case_id, group, cluster, service, upstream,
                     f'2026-03-{len(case_id) + 10:02d}', multiple, interference, incomplete)
    item['task'] = f'工单 {case_id}。' + item['task']
    if other_incident:
        # 业务现场有同一集群的另一事故。独立标注只描述当前工单。
        for kind, rows in item['snapshot'].items():
            if kind not in ('alerts', 'logs', 'traces'):
                continue
            for row in list(rows):
                if row['cluster'] == cluster:
                    rows.append({**row, 'record_id': row['record_id'] + '-OTHER',
                                 'incident_id': f'other-{key}',
                                 'service': 'refund-other' if kind != 'logs' else 'cache-other',
                                 **({'upstream': 'cache-other'} if kind == 'traces' else {})})
    return item


# 采用题不会进入复盘。最后一组只用于发布后审计。
ROUNDS = (
    (case('W01', 'train', multiple=True), (
        case('G11', 'validation'), case('G12', 'validation', multiple=True, interference=True),
        case('G13', 'validation', incomplete=True),
    )),
    (case('W02', 'train', multiple=True, other_incident=True), (
        case('G21', 'validation', multiple=True, interference=True),
        case('G22', 'validation', multiple=True, other_incident=True),
        case('G23', 'validation', incomplete=True),
    )),
)
AUDIT = (
    case('H01', 'holdout', multiple=True, other_incident=True, interference=True),
    case('H02', 'holdout', incomplete=True),
)
