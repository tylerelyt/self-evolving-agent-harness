"""固定构造快照，不是生产事故；按事故划分，不在搜索时重新抽题。"""
from support import LESSON18
from alert_dataset import incident, public_input


def make_case(case_id, split, cluster, service, root, day, multiple=False,
              interference=False, incomplete=False):
    item = incident(case_id, split, multiple=multiple, interference=interference,
                    incomplete=incomplete)
    replacements = {'prod-eu': cluster, 'checkout-api': service,
                    'catalog-api': service + '-web', 'db-gateway': root,
                    '2026-01-15': day}
    def replace(value):
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
        return value
    item = replace(item)
    # 干扰记录有不同根因，不能把同名服务直接归入生产事故。
    for row in item['snapshot']['logs']:
        row.pop('role', None)  # 原始观测里不携带评分用的根因角色标注。
        if row['cluster'] == 'staging-eu':
            row.update(service=service + '-test-cache', observation='cache_refresh_timeout')
    for row in item['snapshot']['traces']:
        if row['cluster'] == 'staging-eu':
            row['upstream'] = service + '-test-cache'
    return item


SPLITS = {
    'train': [
        make_case('T01', 'train', 'prod-eu', 'checkout-api', 'db-gateway', '2026-01-15'),
        make_case('T02', 'train', 'prod-eu', 'checkout-api', 'db-gateway', '2026-01-16',
                  multiple=True, interference=True),
        make_case('T03', 'train', 'prod-eu', 'checkout-api', 'db-gateway', '2026-01-17', incomplete=True),
    ],
    'validation': [
        make_case('V01', 'validation', 'prod-us', 'orders-api', 'orders-db', '2026-02-15', interference=True),
        make_case('V02', 'validation', 'prod-us', 'orders-api', 'orders-db', '2026-02-16', multiple=True),
        make_case('V03', 'validation', 'prod-us', 'orders-api', 'orders-db', '2026-02-17', incomplete=True),
    ],
    'holdout': [
        make_case('H01', 'holdout', 'prod-ap', 'booking-api', 'booking-db', '2026-03-15'),
        make_case('H02', 'holdout', 'prod-ap', 'booking-api', 'booking-db', '2026-03-16',
                  multiple=True, interference=True),
        make_case('H03', 'holdout', 'prod-ap', 'booking-api', 'booking-db', '2026-03-17', incomplete=True),
    ],
}


def validate_splits(splits=SPLITS):
    seen_ids, seen_snapshots = set(), set()
    seen_roots = set()
    for split in ('train', 'validation', 'holdout'):
        if not splits[split]:
            raise ValueError('数据划分不能为空')
        for task in splits[split]:
            snapshot_id = task['snapshot']['snapshot_id']
            if task['id'] in seen_ids or snapshot_id in seen_snapshots or task['group'] != split:
                raise ValueError('事故跨集合重复，或划分标记不一致')
            seen_ids.add(task['id'])
            seen_snapshots.add(snapshot_id)
        roots = {t['expected']['root_cause'] for t in splits[split]} - {None}
        if roots & seen_roots:
            raise ValueError('不同划分不能复用同一根因服务')
        seen_roots.update(roots)
