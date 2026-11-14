"""Scoped teaching observations and evidence checks for skill-library curation."""
from copy import deepcopy
import hashlib
import json
import re

from common import completed, tool_calls


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


PLANS = {
    'capacity': {'action': 'inspect_capacity_and_slow_queries',
                 'checks': ['peak_concurrency_vs_limit', 'slow_query_duration'],
                 'verification': ['returned_connections_at_recovery', 'waiting_clears_after_load_drop']},
    'leak': {'action': 'inspect_release_paths',
             'checks': ['normal_return', 'exception', 'early_return'],
             'verification': ['success_request_returns_connection', 'failed_request_returns_connection',
                              'active_returns_to_baseline', 'waiting_recovers']},
}
RULES = {
    'capacity': {'schema': 'connection-diagnostic-rule-v1', 'capability': 'capacity',
                 'when': {'demand_exceeds_limit': True, 'connections_return_after_peak': True},
                 'diagnosis': 'capacity-pressure', 'next_check': PLANS['capacity']},
    'leak': {'schema': 'connection-diagnostic-rule-v1', 'capability': 'leak',
             'when': {'connections_return_after_peak': False, 'long_held_error_requests': True},
             'diagnosis': 'suspected-leak', 'next_check': PLANS['leak']},
}


def case(kind):
    if kind not in RULES:
        raise ValueError('Unknown diagnostic case')
    scope = {'cluster': 'prod-eu', 'service': 'checkout-api',
             'window': '09:00-09:05' if kind == 'capacity' else '10:00-10:05'}
    rows = {
        'metrics': [
            {'id': kind + '-peak', 'phase': 'peak', 'demand_connections': 120 if kind == 'capacity' else 20,
             'active': 80, 'max': 80, 'waiting': 24},
            {'id': kind + '-recovery', 'phase': 'load_recovered', 'demand_connections': 20,
             'active': 6 if kind == 'capacity' else 80, 'baseline_active': 6,
             'max': 80, 'waiting': 0 if kind == 'capacity' else 24}],
        'holders': [{'id': kind + '-holders', 'error_requests_held_over_300s': 0 if kind == 'capacity' else 3,
                     'all_completed_requests_returned': kind == 'capacity'}],
        'release_paths': [{'id': kind + '-code-coverage', 'status': 'not_collected',
                          'covered_paths': [], 'reason': 'Release-path code has not been captured'}],
    }
    return {'kind': kind, 'scope': scope, 'sections': rows}


def query(kind, section):
    fixture = case(kind)
    if section not in fixture['sections']:
        return {'success': False, 'error': 'Unknown observation section'}
    return {'success': True, 'constructed': True, 'scope': fixture['scope'],
            'section': section, 'records': deepcopy(fixture['sections'][section])}


def register_query(kind):
    from tools.registry import registry
    case(kind)
    registry.register(name='diagnostic_query', toolset='curation_diagnostics',
        schema={'name': 'diagnostic_query',
                'description': '读取当前任务固定范围内的构造现场；release_paths 未覆盖时明确返回 not_collected。',
                'parameters': {'type': 'object', 'properties': {'section': {'type': 'string',
                    'enum': ['metrics', 'holders', 'release_paths']}}, 'required': ['section'], 'additionalProperties': False}},
        handler=lambda args, **kwargs: json.dumps(query(kind, args['section']), ensure_ascii=False),
        check_fn=lambda: True)


def probe_tools():
    from tools import skills_tool  # Register the pinned native read tools.
    from tools.registry import registry
    allowed = {'skills_list', 'skill_view', 'diagnostic_query'}
    # Compact tool discovery can defer custom tools. Pin these three actual
    # registry schemas so the read-only probe can invoke its query directly.
    tools = registry.get_definitions(allowed, quiet=True)
    if {tool['function']['name'] for tool in tools} != allowed:
        raise RuntimeError('Diagnostic probe tools are incomplete')
    return tools


def prompt(kind):
    fixture = case(kind)
    return ('构造教学诊断任务，范围：' + json.dumps(fixture['scope'], ensure_ascii=False) +
            '。自行从当前技能库找到诊断入口并读取相关规则；用 diagnostic_query 获取本次现场，'
            '不能从技能名称推断当前故障。只读，不执行处置。只返回 JSON 对象：'
            'diagnosis 为 capacity-pressure、suspected-leak 或 unknown；'
            'judgments 包含 demand_exceeds_limit、connections_return_after_peak、release_failure_confirmed 三个布尔值；'
            'evidence_ids 为实际查询回执中的编号数组；next_check 包含 action、checks、verification，'
            '按实际读取的诊断规则给出可执行检查与结果验证。缺少释放代码时不能确认释放失败。')


def read_rules(calls):
    """Rules may move, be renamed or be inlined into SKILL.md."""
    found = []
    for call in calls:
        receipt = call.get('receipt')
        if (call['name'] != 'skill_view' or not isinstance(receipt, dict)
                or receipt.get('success') is not True or not isinstance(receipt.get('content'), str)):
            continue
        for body in re.findall(r'```json\s*\n(.*?)\n```', receipt['content'], re.S):
            try:
                rule = json.loads(body)
            except ValueError:
                continue
            if isinstance(rule, dict) and rule.get('schema') == 'connection-diagnostic-rule-v1':
                found.append({'rule': rule, 'tool_call_id': call['tool_call_id'], 'resource': call.get('args')})
    return found


def plan_matches(plan, expected):
    return (isinstance(plan, dict) and set(plan) == {'action', 'checks', 'verification'}
            and plan.get('action') == expected['action']
            and all(isinstance(plan.get(key), list) and all(isinstance(item, str) for item in plan[key])
                    and len(plan[key]) == len(set(plan[key])) and set(plan[key]) == set(expected[key])
                    for key in ('checks', 'verification')))


def rule_matches(rule, expected):
    when = rule.get('when')
    return (all(rule.get(key) == expected[key] for key in ('schema', 'capability', 'diagnosis'))
            and isinstance(when, dict) and set(when) == set(expected['when'])
            and all(type(when[key]) is bool and when[key] == value for key, value in expected['when'].items())
            and plan_matches(rule.get('next_check'), expected['next_check']))


def assess(result, kind, parsed):
    calls = tool_calls(result.get('messages', []))
    fixture = case(kind)
    reads = read_rules(calls)
    rules = [entry for entry in reads if rule_matches(entry['rule'], RULES[kind])]
    identifiers = [call['tool_call_id'] for call in calls]
    receipts = [message.get('tool_call_id') for message in result.get('messages', []) if message.get('role') == 'tool']
    paired = (bool(identifiers) and all(isinstance(item, str) and item for item in identifiers)
              and all(isinstance(item, str) and item for item in receipts)
              and len(identifiers) == len(set(identifiers)) and sorted(identifiers) == sorted(receipts)
              and all(call['name'] in {'skills_list', 'skill_view', 'diagnostic_query'} for call in calls))
    observed, query_errors, queried = {}, [], set()
    for call in calls:
        if call['name'] != 'diagnostic_query':
            continue
        args = call.get('args')
        section = args.get('section') if isinstance(args, dict) and set(args) == {'section'} else None
        if section not in fixture['sections'] or call.get('receipt') != query(kind, section):
            query_errors.append(call['tool_call_id'])
            continue
        queried.add(section)
        observed.update({row['id']: row for row in call['receipt']['records']})
    answer = parsed.get('answer')
    schema = (isinstance(answer, dict) and set(answer) == {'diagnosis', 'judgments', 'evidence_ids', 'next_check'}
              and isinstance(answer.get('judgments'), dict) and isinstance(answer.get('next_check'), dict))
    expected_judgments = {'demand_exceeds_limit': kind == 'capacity',
                          'connections_return_after_peak': kind == 'capacity', 'release_failure_confirmed': False}
    required_sections = {'metrics', 'holders'} | ({'release_paths'} if kind == 'leak' else set())
    required_ids = {row['id'] for section in required_sections for row in fixture['sections'][section]}
    citations = answer.get('evidence_ids') if isinstance(answer, dict) else None
    citations_ok = (isinstance(citations, list) and all(isinstance(item, str) for item in citations)
                    and len(citations) == len(set(citations)) and required_ids <= set(citations)
                    and set(citations) <= observed.keys())
    judgments = answer.get('judgments') if isinstance(answer, dict) else None
    judgments_ok = (isinstance(judgments, dict) and set(judgments) == set(expected_judgments)
                    and all(type(judgments[key]) is bool and judgments[key] == value
                            for key, value in expected_judgments.items()))
    plan = answer.get('next_check') if isinstance(answer, dict) else None
    plan_ok = plan_matches(plan, PLANS[kind])
    checks = {'completed': completed(result), 'answer_schema': bool(schema),
              'readonly_calls_uniquely_paired': paired,
              'related_rule_read': bool(rules), 'current_queries_match': bool(queried) and not query_errors,
              'required_sections_observed': required_sections <= queried, 'actual_citations': bool(citations_ok),
              'necessary_judgments': bool(judgments_ok),
              'diagnosis': isinstance(answer, dict) and answer.get('diagnosis') == RULES[kind]['diagnosis'],
              'verifiable_next_check': bool(plan_ok)}
    needs_review = answer is None or bool(parsed.get('outside_text'))
    return {'checks': checks, 'task_pass': None if needs_review else all(checks.values()),
            'needs_review': needs_review, 'answer': answer, 'rule_receipts': reads,
            'query_errors': query_errors, 'snapshot_sha256': digest(fixture), 'input_sha256': digest(prompt(kind)),
            'strict_json_format_pass': parsed['strict_json_format_pass'], 'answer_source': parsed['source'],
            'explanation_semantics': 'not_evaluated', 'verification_executed': False}
