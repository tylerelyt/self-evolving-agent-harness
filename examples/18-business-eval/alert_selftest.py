"""构造已知对错轨迹校准评分器，绝不冒充模型执行。"""
import json
from alert_dataset import CASES
from alert_scorer import grade
from incidentctl import query, readonly_command


def correct(item):
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'skill', 'function': {'name': 'skill_view', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'skill', 'content': json.dumps({'success': True})}]
    for action in ('alerts', 'logs', 'traces'):
        messages.extend([{'role': 'assistant', 'tool_calls': [{'id': action, 'function': {'name': 'terminal',
            'arguments': json.dumps({'command': './incidentctl ' + action + ' prod-eu all'})}}]},
            {'role': 'tool', 'tool_call_id': action, 'content': json.dumps({'exit_code': 0,
                'output': json.dumps(query(item['snapshot'], action, 'prod-eu', 'all'))})}])
    return {'completed': True, 'messages': messages, 'tool_names': ['skills_list', 'skill_view', 'terminal'],
            'skills_unchanged': True, 'memory_before': 'same', 'memory_after': 'same',
            'final_response': json.dumps({**item['expected'], 'citations': item['required_citations'],
                                         'explanation': '构造正确说明，供评分程序测试。'})}


def run():
    checks = []
    def check(name, item, result, wanted):
        graded = grade(item, result)
        actual = None if graded['score'] is None else graded['score']['overall_pass']
        checks.append({'name': name, 'passed': actual is wanted})
    for item in CASES:
        check('正确-' + item['id'], item, correct(item), True)
    for field, value in [('cluster', 'staging-eu'), ('root_cause', 'checkout-api'), ('affected_services', ['db-gateway'])]:
        result = correct(CASES[0]); answer = json.loads(result['final_response']); answer[field] = value
        result['final_response'] = json.dumps(answer); check('错误-' + field, CASES[0], result, False)
    result = correct(CASES[2]); answer = json.loads(result['final_response'])
    answer['citations'] = [x + '-STAGING' for x in answer['citations']]
    result['final_response'] = json.dumps(answer); check('跨集群引用', CASES[2], result, False)
    result = correct(CASES[2])
    receipt = json.loads(result['messages'][3]['content']); data = json.loads(receipt['output']); data['cluster_filter'] = 'all'
    receipt['output'] = json.dumps(data); result['messages'][3]['content'] = json.dumps(receipt)
    check('漏集群过滤', CASES[2], result, False)
    for name, modify in [('模型未完成', lambda e: e.update(completed=False)),
                         ('连接故障', lambda e: e.update(infrastructure_error='构造断连')),
                         ('工具故障', lambda e: e['messages'][3].update(content='{"error":"构造失败","exit_code":1}'))]:
        result = correct(CASES[0]); modify(result); check(name, CASES[0], result, None)
    result = correct(CASES[0]); result['messages'][3]['tool_call_id'] = 'wrong'
    check('回执错配', CASES[0], result, False)
    result = correct(CASES[3]); answer = json.loads(result['final_response']); answer.update(root_cause='db-gateway', status='confirmed')
    result['final_response'] = json.dumps(answer); check('缺依据仍猜根因', CASES[3], result, False)
    result = correct(CASES[3]); result['messages'] = result['messages'][:4]
    check('未查日志链路就说未知', CASES[3], result, False)
    result = correct(CASES[0]); result['messages'][2]['tool_calls'][0]['function']['arguments'] = json.dumps({'command': './incidentctl alerts all all'})
    check('请求漏过滤但回执伪装正确', CASES[0], result, False)
    result = correct(CASES[0]); receipt = json.loads(result['messages'][5]['content'])
    data = json.loads(receipt['output']); data['records'][0]['time_window'] = 'other-time'
    receipt['output'] = json.dumps(data); result['messages'][5]['content'] = json.dumps(receipt)
    check('引用时间被篡改', CASES[0], result, False)
    check('评分输入损坏留空分', {}, correct(CASES[0]), None)
    try:
        readonly_command({'command': './incidentctl alerts prod-eu all; cat expected.json'})
        checks.append({'name': '拒绝额外命令', 'passed': False})
    except PermissionError:
        checks.append({'name': '拒绝额外命令', 'passed': True})
    result = {'constructed': True, 'model_requests': 0, 'checks': checks, 'passed': all(c['passed'] for c in checks)}
    if not result['passed']:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
