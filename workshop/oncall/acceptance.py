"""逐项核对真实任务、业务 HTTP 查询与原生后台回执；不补造成功。"""
import json
from datetime import datetime
from common import api, observations, record, runtime, memory_operations


def task_alert(task):
    """读取实际发送给 Agent 的告警，不从固定演示地址推断本次查询目标。"""
    _, separator, payload = task.get('message', '').partition('告警详情（实际请求统计）：')
    if not separator:
        return None
    try:
        alert, _ = json.JSONDecoder().raw_decode(payload.lstrip())
        return alert if isinstance(alert, dict) else None
    except ValueError:
        return None


def current_log_query_matches(queries, alert):
    if not alert:
        return False
    scope = {key: alert.get(key) for key in ('service', 'region', 'env')}
    if not all(scope.values()):
        return False

    def instant(value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('查询时段缺少时区')
        return parsed

    try:
        start, end = instant(alert['since']), instant(alert['until'])
        if start > end:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    relevant = [q for q in queries if q.get('path') in ('/catalog', '/logs')]
    if not relevant or relevant[0]['path'] != '/catalog':
        return False
    if any(q.get('params', {}).get('entry') == 'payment-api-prod' for q in relevant):
        return False
    latest_catalog = None
    for query in relevant:
        params = query.get('params', {})
        if query.get('code') != 200 or any(params.get(key) != value for key, value in scope.items()):
            continue
        if query['path'] == '/catalog':
            latest_catalog = query
            continue
        if not latest_catalog or latest_catalog['at'] >= query['at']:
            continue
        current = latest_catalog.get('response', {}).get('current')
        if not current or params.get('entry') != current or not (query.get('count', 0) or 0) > 0:
            continue
        try:
            if instant(params['since']) <= start <= end <= instant(params['until']):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def report(sessions):
    root = observations()
    tasks = sorted([json.loads(p.read_text()) for p in (root / 'tasks').glob('*.json')], key=lambda t: t['created_at'])
    if sessions:
        tasks = [t for t in tasks if t['session'] in sessions]
    offline = False
    try:
        reviews = api('/reviews')
        memory = api('/memory')['content']
    except OSError:
        offline = True
        reviews = [json.loads(p.read_text()) for p in (root / 'reviews').glob('*.json')]
        file = runtime() / 'hermes/memories/MEMORY.md'
        memory = file.read_text() if file.exists() else ''
    queries_file = root / 'business-queries.jsonl'
    queries = [json.loads(x) for x in queries_file.read_text().splitlines()] if queries_file.exists() else []
    inspect = {}
    for sid in dict.fromkeys(t['session'] for t in tasks):
        if offline:
            file = root / 'sessions' / (sid + '-final.json')
            if not file.exists():
                file = root / 'sessions' / (sid + '-inspect.json')
            inspect[sid] = json.loads(file.read_text()) if file.exists() else {}
        else:
            inspect[sid] = api('/sessions/' + sid)
        record(root / 'sessions' / (sid + '-final.json'), inspect[sid])
    checks = []

    def mark(label, condition, detail, links):
        checks.append({'item': label, 'status': '通过' if condition else '未通过或未取得完整记录',
                       'basis': detail, 'records': links})

    if len(tasks) >= 3:
        t1, t2, t3 = tasks[:3]
        r1 = [r for r in reviews if r['task_id'] == t1['id']]
        r2 = [r for r in reviews if r['task_id'] == t2['id']]
        old = r1[-1].get('memory_after', '') if r1 else ''
        new = r2[-1].get('memory_after', '') if r2 else ''
        calls1 = memory_operations([c for r in r1 for c in r.get('calls', [])])
        saved1 = any(isinstance(c.get('receipt'), dict) and c['receipt'].get('success') is True for c in calls1)
        calls2 = memory_operations([c for r in r2 for c in r.get('calls', [])])
        replacements = [c for c in calls2 if c['args'].get('action') == 'replace'
                        and isinstance(c.get('receipt'), dict) and c['receipt'].get('success') is True]
        unique = all(sum(c['args'].get('old_text', '') in entry for entry in old.split('\n§\n')) == 1 for c in replacements)
        q1 = [q for q in queries if t1.get('started_at', '') <= q['at'] <= t1.get('ended_at', '')]
        q2 = [q for q in queries if t2.get('started_at', '') <= q['at'] <= t2.get('ended_at', '')]
        q3 = [q for q in queries if t3.get('started_at', '') <= q['at'] <= t3.get('ended_at', '')]
        discovery = ('payment-api-prod' not in t1['message'] and any(q['path'] == '/services' for q in q1)
                     and any(q['path'] == '/logs' and q['params'].get('entry') == 'payment-api-prod'
                             and q.get('count', 0) > 0 for q in q1) and bool(old.strip()) and saved1)
        attempted = any(q['path'] == '/logs' and q['params'].get('entry') == 'payment-api-prod' for q in q2)
        migration_file = root / 'environment-regional.json'
        business = migration_file.exists() and any(q['path'] == '/catalog' and q['code'] == 200 for q in q2)
        # 旧入口退役由 run.sh 的只读现场检查记录证明；不要求模型撞旧入口。
        migration_check = root / 'migration-check.json'
        switch_check = json.loads(migration_check.read_text()) if migration_check.exists() else {}
        mark('现场切换是否成立', business and switch_check.get('old_entry_http_410') and switch_check.get('memory_unchanged')
             and switch_check.get('same_oncall_pid'), f"旧入口真实410={switch_check.get('old_entry_http_410')}；Memory未被切换改变={switch_check.get('memory_unchanged')}；常驻PID未变={switch_check.get('same_oncall_pid')}。", ['environment-regional.json', 'migration-check.json'])
        front_ok = all(t['status'] == 'completed' and not any(isinstance(c.get('receipt'), dict) and c['receipt'].get('success') for c in memory_operations(t.get('calls', []))) for t in tasks[:3])
        front_ok &= discovery and any(q['path'] == '/logs' and q.get('count', 0) > 0 and q['code'] == 200 for q in q2)
        mark('前台调查是否完成', front_ok, '巡检发现统一入口并读到真实日志；两次告警完成且前台无成功 memory 写入。', ['tasks/', 'business-queries.jsonl'])
        relevant2 = [q for q in q2 if q['path'] in ['/catalog', '/logs']]
        old_first = bool(relevant2 and relevant2[0]['path'] == '/logs' and relevant2[0]['params'].get('entry') == 'payment-api-prod')
        branch = ('先试旧入口再查目录' if old_first else '直接查目录；后续核对旧入口') if attempted else '直接查目录（允许的真实分支）'
        checks.append({'item': '是否尝试旧入口', 'status': branch,
                       'basis': '以第四幕实际 HTTP 查询为准；两种轨迹均成立。', 'records': ['business-queries.jsonl']})
        native = all(events and len(events) == 1 and events[0]['thread'] == 'bg-review' and events[0]['daemon']
                     and events[0]['status'] == 'completed' for events in [r1, r2])
        mark('原生后台是否触发', native, f'巡检 {len(r1)} 次，迁移告警 {len(r2)} 次；以真实 worker 与模型返回为准。', ['reviews/'])
        mark('后台是否成功 replace', bool(replacements) and unique, f'成功 replace {len(replacements)} 次，old_text 唯一定位：{unique}。', ['reviews/'])
        changed = bool(new.strip()) and new != old
        mark('长期记忆是否发生修订', changed, '原生复盘前后的记忆全文不同；是否保留有效方法、是否混淆适用范围，需要阅读全文判断，不能靠入口名称是否出现判定。', ['memory/', 'reviews/'])
        s2, s3 = inspect[t2['session']], inspect[t3['session']]
        snapshot_ok = (bool(old.strip()) and old.strip() in s2['memory_at_start']
                       and s2['memory_snapshot'] == s2['memory_at_start']
                       and old.strip() in (s2.get('cached_prompt') or ''))
        mark('旧快照是否保持', snapshot_ok, 'migration Agent 启动加载第一次实际保存的完整记忆，未压缩/未重建时快照及缓存提示仍含该内容；不要求特定措辞。', ['sessions/', 'memory/'])
        loaded = (s3['history_at_start'] == 0 and s3['memory_at_start'] != s2['memory_at_start']
                  and bool(new.strip()) and new.strip() in s3['memory_at_start']
                  and s3['agent_instance'] != s2['agent_instance'] and '正式说明全文' not in t3['message'])
        mark('新会话是否加载', loaded, '独立 Agent、零条启动历史、实际加载第二次复盘后的完整记忆，无迁移附件。', ['sessions/', 'tasks/', 'memory/'])
        behavior = current_log_query_matches(q3, task_alert(t3))
        mark('新会话查询是否正确', behavior, '按本次告警的服务、区域、环境先查目录，再读目录实际返回的当前入口，查询时段覆盖告警且取得日志；无旧入口调用。', ['business-queries.jsonl', 'tasks/'])
    else:
        for label in ['现场切换是否成立', '前台调查是否完成', '是否尝试旧入口', '原生后台是否触发', '后台是否成功 replace', '长期记忆是否发生修订', '旧快照是否保持', '新会话是否加载', '新会话查询是否正确']:
            mark(label, False, f'仅取得 {len(tasks)} 个任务，五幕记录不完整。', ['tasks/', 'reviews/'])
    result = {'checks': checks, 'task_ids': [t['id'] for t in tasks], 'review_ids': [r['review_id'] for r in reviews],
              'model': tasks[0].get('result', {}).get('model', '未取得模型返回') if tasks else '未调用', 'offline_report': offline, 'memory_final': memory,
              'memory_content_automatically_scored': False,
              'memory_changed_after_new_session': len(tasks) >= 3 and memory != new,
              'complete': len(tasks) >= 3 and all(c['status'] == '通过' for c in checks if c['item'] != '是否尝试旧入口')}
    record(root / 'acceptance.json', result)
    rows = ['# 本次五幕逐项验收', '', f"模型：{result['model']}。机制链路检查通过：{'是' if result['complete'] else '否'}。", '', '| 项目 | 实际标记 | 核查依据 |', '| --- | --- | --- |']
    rows += [f"| {c['item']} | {c['status']} | {c['basis']} |" for c in checks]
    rows += ['', '所有标记都指向本目录的 [任务](tasks/)、[原生复盘](reviews/)、[记忆前后及差异](memory/)、[会话快照](sessions/)、[业务 HTTP 查询](business-queries.jsonl) 和 [逐步日志](run.log)。', '', '付款现场为虚构业务；这些记录不证明生产高可用、全天运行，也不能单次归因全部行为改善。']
    rows += ['', '以上检查确认执行与使用过程，不自动评价记忆内容是否正确。请逐条阅读修订前后全文，检查有效信息是否被误删，临时结论是否被写成长期规则。']
    if result['memory_changed_after_new_session']:
        rows += ['', '新会话使用之后，后台再次修改了记忆。需要继续检查最后一次差异；新会话查对日志，不等于最后保存的记忆也正确。']
    text = '\n'.join(rows) + '\n'
    record(root / 'REPORT.md', text)
    return text
