#!/usr/bin/env python3
"""Agent 可用的只读业务 CLI；所有查询均通过真实 HTTP。"""
import argparse
import json
import sys
from urllib.parse import urlencode
from common import connection, http

parser = argparse.ArgumentParser(description='只读查询教学支付部署、目录与真实日志')
subs = parser.add_subparsers(dest='kind', required=True)
services = subs.add_parser('services').add_subparsers(dest='action', required=True).add_parser('describe')
services.add_argument('service')
catalog = subs.add_parser('catalog').add_subparsers(dest='action', required=True).add_parser('lookup')
logs = subs.add_parser('logs').add_subparsers(dest='action', required=True).add_parser('read')
logs.add_argument('entry')
for p in [catalog, logs]:
    p.add_argument('--service', required=True)
    p.add_argument('--region', required=True)
    p.add_argument('--env', required=True)
logs.add_argument('--since', help='UTC ISO-8601 开始时间')
logs.add_argument('--until', help='UTC ISO-8601 结束时间')
args = parser.parse_args()
q = {k: v for k, v in vars(args).items() if k not in ['kind', 'action'] and v is not None}
try:
    print(json.dumps(http(connection('paylab') + '/' + args.kind + '?' + urlencode(q)), ensure_ascii=False, indent=2))
except (RuntimeError, OSError, ValueError) as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
