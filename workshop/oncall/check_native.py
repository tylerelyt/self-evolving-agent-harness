#!/usr/bin/env python3
"""无模型现场检查：真实订单、退役回执、组合键校验、归档与只读认证。"""
import json
import sqlite3
import tempfile
from urllib.parse import urlencode
from common import HERE, api, connection, http, observations, record, runtime, pay_admin

checks = []

def check(name, condition, details):
    checks.append({'item': name, 'ok': bool(condition), 'details': details})
    assert condition, name


def error(path, fragment):
    try:
        http(connection('paylab') + path)
    except RuntimeError as e:
        check(path, fragment in str(e), str(e))
    else:
        raise AssertionError('预期失败却成功：' + path)


pay_admin('/setup', {})
check('现场实际在临时目录', str((runtime() / 'paylab').resolve()).startswith(('/tmp/', '/private/tmp/')), str((runtime() / 'paylab').resolve()))
pay_admin('/profile', {'profile': 'normal', 'requests': 3})
rows = [http(connection('paylab') + '/pay', {'service': 'payment-api', 'region': 'us-east', 'env': 'prod'}) for _ in range(3)]
q = '?service=payment-api&region=us-east&env=prod'
logs = http(connection('paylab') + '/logs' + q + '&entry=payment-api-prod')
with sqlite3.connect(runtime() / 'paylab/orders.sqlite3') as db:
    count = db.execute('SELECT count(*) FROM orders').fetchone()[0]
check('付款处理实际产生订单和日志', logs['count'] == 3 and count == 3, {'orders': count, 'logs': logs['count']})
point = rows[0]['timestamp_utc'].replace('+00:00', 'Z')
window = http(connection('paylab') + '/logs?' + urlencode({'service': 'payment-api', 'region': 'us-east', 'env': 'prod', 'entry': 'payment-api-prod', 'since': point, 'until': point}))
check('UTC Z 与 +00:00 时段等价且含边界', window['count'] == 1, window['count'])
error('/logs' + q + '&entry=payment-api-prod&since=2026-10-11T00:00:00Z&until=2026-10-10T00:00:00Z', 'HTTP 400')
error('/catalog' + q, '尚未上线')
pid = api('/status')['pid']
before = api('/memory')['content']
pay_admin('/env', {'mode': 'regional'})
error('/logs' + q + '&entry=payment-api-prod', 'HTTP 410')
error('/logs' + q + '&entry=logs://payment-api/eu-west/staging/v2', 'HTTP 400')
error('/logs' + q + '&entry=logs://payment-api/us-east/prod/v1', 'HTTP 400')
error('/catalog?service=other&region=us-east&env=prod', 'HTTP 400')
error('/catalog?service=payment-api&region=us-east&env=staging', 'HTTP 400')
cat = http(connection('paylab') + '/catalog' + q)
archive = http(connection('paylab') + '/logs' + q + '&entry=' + cat['archive'])
check('归档保留迁移前实际日志', archive['count'] == 3, archive['count'])
pay_admin('/profile', {'profile': 'timeout', 'requests': 2})
rows = [http(connection('paylab') + '/pay', {'service': 'payment-api', 'region': 'us-east', 'env': 'prod'}) for _ in range(2)]
new = http(connection('paylab') + '/logs' + q + '&entry=' + cat['current'])
check('区域日志记录真实下游延迟超时', new['count'] == 2 and all(r['status'] == 'timeout' and r['duration_ms'] >= 60 for r in rows), rows)
check('切换不触碰常驻进程、记忆与复盘', api('/status')['pid'] == pid and api('/memory')['content'] == before and api('/reviews') == [], {'pid_unchanged': True, 'memory_unchanged': True})
try:
    http(connection() + '/status', token='wrong-test-token')
except RuntimeError as e:
    check('HTTP 课程入口真实认证失败', 'HTTP 401' in str(e), str(e))
else:
    raise AssertionError('未校验令牌')
record(observations() / 'native-checks.json', checks)
print(f'无模型现场检查：{len(checks)} 项均成立；不据此判断模型复盘行为。')
