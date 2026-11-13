"""评测包版本及独立标注的结构与证据归属，不判断自然语言解释。"""

PACK_SCHEMA = 'oncall-pack-v3'
SCORER_VERSION = 'oncall-evidence-v3'


def annotation_errors(annotation, snapshot, target_cluster):
    expected = annotation.get('expected_output')
    if (not isinstance(expected, dict)
            or set(expected) != {'root_cause', 'affected_service', 'excluded_cluster'}
            or not all(isinstance(v, str) and v for v in expected.values())):
        return ['标准答案字段缺失或无效']
    affected_cluster, separator, service = expected['affected_service'].partition('/')
    root_cluster, root_separator, root_service = expected['root_cause'].partition('/')
    errors = []
    if (not separator or not service or affected_cluster != target_cluster
            or not root_separator or not root_service or root_cluster != target_cluster):
        errors.append('根因与受影响服务必须属于目标集群')
    if expected['excluded_cluster'] == target_cluster:
        errors.append('排除集群不能是目标集群')
    rows = snapshot['rows']
    valid_root = {r['id'] for r in rows if r.get('cluster') == target_cluster}
    valid_exclusion = {r['id'] for r in rows if r.get('cluster') == expected['excluded_cluster']
                       and r.get('service') == service}
    for field, valid, label in (
        ('required_evidence', valid_root, '目标集群根因、链路和影响范围'),
        ('required_exclusion_evidence', valid_exclusion, '排除集群同名服务'),
    ):
        required = annotation.get(field)
        if (not isinstance(required, list) or not required
                or any(not isinstance(i, str) or not i for i in required)
                or len(required) != len(set(required)) or not set(required) <= valid):
            errors.append(field + ' 必须明确列出不重复、归属正确的' + label + '证据编号')
    return errors
