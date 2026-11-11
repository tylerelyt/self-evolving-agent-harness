"""接通在线事件演练与同一次调查触发的学习闭环。"""
from capstone import main, save, ROUNDS, payload
from incident_runtime import IncidentRuntime


def rejected(callback):
    try:
        callback()
    except ValueError:
        return True
    raise AssertionError('演练动作本应被拒绝')


def run(project):
    root = project.root
    db_path = root / 'incidents.sqlite3'
    online = IncidentRuntime(db_path)
    task = ROUNDS[0][0]
    incident = task['id']
    cluster = task['snapshot']['alerts'][0]['cluster']
    scope = cluster + '/payment/prod'
    operation = incident + '-mitigation'
    healthy = online.observe('window-01', incident, scope, 100, 1)
    sparse = online.observe('window-02', incident, scope, 4, 4)
    assert not online.report()['incidents']
    alert = online.observe('window-03', incident, scope, 100, 20)
    duplicate = online.observe('window-03', incident, scope, 100, 20)
    online.observe('webhook-01', incident, scope, 100, 20, source='webhook')
    assert len(online.report()['incidents']) == 1 and duplicate['deduplicated']
    sha, skill = project.versions.sync('oncall-1')
    online.start(incident, sha)
    first = project.execute(task, sha, skill, 'work')
    proof, result = first
    answer = proof['grading'].get('answer') or {}
    rows = [row for call in result['tool_calls'] if call['name'] == 'terminal'
            for row in payload(call).get('records', [])
            if row['cluster'] == cluster and row['incident_id'] == incident]
    upstream = answer.get('root_cause')
    if (proof['loaded_sha256'] != sha or answer.get('status') != 'confirmed' or not upstream
            or not any(row['kind'] == 'log' and row['service'] == upstream
                       and row['observation'] == 'database_connections_exhausted' for row in rows)
            or not any(row['kind'] == 'trace' and row['upstream'] == upstream for row in rows)):
        save(project.root / 'incident-lifecycle.json', online.report())
        raise RuntimeError('现场回执不足以提交演练处置，保留调查状态供值班工程师接手')
    online.request_action(incident, operation, proof['artifact'] + '/proof.json')
    early_denied = rejected(lambda: online.execute(incident, operation, scope))
    wrong_scope_denied = rejected(lambda: online.approve(incident, operation, 'different-scope', 'lab-duty-a'))
    online.approve(incident, operation, scope, 'lab-duty-a')
    receipt = online.execute(incident, operation, scope)
    assert online.execute(incident, operation, scope) == receipt
    online.observe('window-04', incident, scope, 100, 12, source='verification')
    online.observe('window-05', incident, scope, 100, 1, source='verification')
    close_denied = rejected(lambda: online.close(incident, 'lab-duty-a'))
    online.handoff(incident, 'lab-duty-b', '再核对一个连续健康的观测窗口')
    online = IncidentRuntime(db_path)
    assert online.report()['incidents'][0]['state'] == 'VERIFYING'
    online.observe('window-06', incident, scope, 100, 1, source='verification')
    online.close(incident, 'lab-duty-b')
    closed = online.report()
    assert closed['incidents'][0]['state'] == 'CLOSED'
    assert sum(e['kind'] == 'action_executed' for e in closed['events']) == 1
    save(root / 'incident-lifecycle.json', {**closed, 'case': 'constructed',
         'checks': {'healthy': healthy, 'low_sample': sparse, 'alert': alert,
                    'duplicate': duplicate, 'unapproved_denied': early_denied,
                    'wrong_scope_denied': wrong_scope_denied, 'premature_close_denied': close_denied}})
    learning = project.run(first_execution=first)
    assert learning['rounds'][0]['work']['session'] == proof['session']
    rows = ['# 巡检与 On-call 系统演练', '',
            '观测和处置使用构造夹具。调查使用真实 Hermes 会话。', '',
            '| 路径 | 观察 |', '| --- | --- |',
            '| 定时巡检 | 正常和低样本窗口不创建事件，异常窗口进入值班流程 |',
            '| 告警接入 | 重复投递去重，巡检与推送观测归入同一事件 |',
            f'| 调查 | [查询与版本证据]({proof["artifact"]}/proof.json) |',
            '| 处置 | 拒绝未审批与错范围操作，已批准动作重复请求返回同一回执 |',
            '| 恢复复核 | 动作后仍异常。两个连续健康窗口经独立读取后才允许关闭 |',
            '| 交班 | 第二班复开同一数据库，继续未完成的验证 |',
            '| 复盘 | 同一个调查会话进入第一轮学习，版本验收与回退见学习报告 |', '',
            '[在线事件与回执](incident-lifecycle.json) · [学习侧验收](REPORT.md)']
    save(root / 'SYSTEM_REPORT.md', '\n'.join(rows) + '\n')
    return learning


if __name__ == '__main__':
    main(system_runner=run)
