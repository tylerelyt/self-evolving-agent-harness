#!/usr/bin/env python3
"""真实教学支付 HTTP 服务；环境切换不访问 Hermes、任务或记忆。"""
import argparse
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common import now, observations, runtime, save, scrub

class EntryRetired(Exception):
    pass


SCOPES = [('us-east', 'prod'), ('eu-west', 'staging')]


class PayLab:
    def __init__(self):
        self.lock = threading.RLock()
        self.root = runtime() / 'paylab'
        self.ready = (self.root / 'state.json').exists()
        if self.ready:
            self.state = json.loads((self.root / 'state.json').read_text())

    def setup(self):
        with self.lock:
            if not self.ready:
                temp = Path(tempfile.mkdtemp(prefix='hermes-paylab-', dir='/tmp'))
                self.root.symlink_to(temp, target_is_directory=True)
                for folder in ['logs/legacy', 'catalog', 'alerts', 'config']:
                    (self.root / folder).mkdir(parents=True, exist_ok=True)
                (self.root / 'logs/legacy/payment-api-prod.jsonl').touch()
                with sqlite3.connect(self.root / 'orders.sqlite3') as db:
                    db.execute('CREATE TABLE orders (order_id TEXT PRIMARY KEY, request_id TEXT, region TEXT, environment TEXT, amount INTEGER, status TEXT, timestamp_utc TEXT)')
                self.state = {'mode': 'legacy', 'effective_at': now(), 'monitor_collected_at': now(),
                              'scopes': SCOPES, 'downstream_delay_ms': 5, 'timeout_ms': 60}
                self.ready = True
                save(self.root / 'state.json', self.state)
                save(self.root / 'config/payment.json', {'service': 'payment-api', 'timeout_ms': 60})
            return self.status()

    def status(self):
        return {'ready': self.ready, **(self.state if self.ready else {}),
                'directory': str(self.root.resolve()) if self.ready else None}

    def switch(self, mode):
        if mode not in ['legacy', 'regional']:
            raise ValueError('环境只支持 legacy / regional')
        with self.lock:
            if mode != self.state['mode']:
                if mode == 'regional':
                    catalog = {}
                    for region, env in SCOPES:
                        (self.root / f'logs/regional/payment-api/{region}/{env}').mkdir(parents=True, exist_ok=True)
                        (self.root / f'logs/regional/payment-api/{region}/{env}/v2.jsonl').touch()
                        catalog[f'payment-api/{region}/{env}'] = {
                            'current': f'logs://payment-api/{region}/{env}/v2',
                            'archive': f'archive://payment-api/{region}/{env}/legacy'}
                    save(self.root / 'catalog/regional.json', catalog)
                self.state.update(mode=mode, effective_at=now())
                save(self.root / 'state.json', self.state)
            return self.status()

    def scope(self, q):
        service, region, env = q.get('service'), q.get('region'), q.get('env')
        if service != 'payment-api' or (region, env) not in SCOPES:
            raise ValueError('不存在该服务/区域/环境部署')
        return service, region, env

    def query(self, path, q):
        with self.lock:
            if path == '/status':
                return self.status()
            if not self.ready:
                raise ValueError('请先 lab setup')
            if path == '/services':
                if q.get('service') != 'payment-api':
                    raise ValueError('不存在该服务')
                return {'service': 'payment-api', 'mode': self.state['mode'], 'scopes': SCOPES,
                        'log_entry': 'payment-api-prod' if self.state['mode'] == 'legacy' else None,
                        'catalog_available': self.state['mode'] == 'regional', 'timestamps': 'UTC',
                        'timeout_ms': self.state['timeout_ms'], 'downstream_delay_ms': self.state['downstream_delay_ms']}
            service, region, env = self.scope(q)
            if path == '/catalog':
                if self.state['mode'] != 'regional':
                    raise ValueError('Service Catalog 尚未上线')
                return {'service': service, 'region': region, 'environment': env,
                        **json.loads((self.root / 'catalog/regional.json').read_text())[f'{service}/{region}/{env}']}
            if path == '/alert':
                return json.loads((self.root / f'alerts/{region}-{env}.json').read_text())
            if path != '/logs':
                raise ValueError('未知查询接口')
            entry = q['entry']
            if entry == 'payment-api-prod':
                if self.state['mode'] != 'legacy':
                    raise EntryRetired('统一入口已退役，请按服务/区域/环境查 Service Catalog')
                file = self.root / 'logs/legacy/payment-api-prod.jsonl'
            elif entry == f'archive://{service}/{region}/{env}/legacy' and self.state['mode'] == 'regional':
                file = self.root / 'logs/legacy/payment-api-prod.jsonl'
            elif entry == f'logs://{service}/{region}/{env}/v2' and self.state['mode'] == 'regional':
                file = self.root / f'logs/regional/{service}/{region}/{env}/v2.jsonl'
            else:
                raise ValueError('入口无效、版本不存在或与服务/区域/环境不匹配')
            def instant(value):
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError('日志时段必须包含时区，例如 UTC 的 Z 或 +00:00')
                return parsed
            since = instant(q['since']) if q.get('since') else None
            until = instant(q['until']) if q.get('until') else None
            if since and until and since > until:
                raise ValueError('日志开始时间不能晚于结束时间')
            rows = [json.loads(x) for x in file.read_text().splitlines() if x.strip()]
            rows = [x for x in rows if x['region'] == region and x['environment'] == env
                    and (since is None or instant(x['timestamp_utc']) >= since)
                    and (until is None or instant(x['timestamp_utc']) <= until)]
            return {'entry': entry, 'service': service, 'region': region, 'environment': env,
                    'count': len(rows), 'records': rows}

    def payment(self, data):
        # 延迟确实发生；持锁跨写盘，使切换与在途付款形成明确先后顺序。
        with self.lock:
            service, region, env = self.scope(data)
            delay = self.state['downstream_delay_ms']
            started = time.monotonic()
            time.sleep(delay / 1000)
            duration = round((time.monotonic() - started) * 1000, 2)
            failed = duration >= self.state['timeout_ms']
            row = {'timestamp_utc': now(), 'service': service, 'region': region, 'environment': env,
                   'request_id': 'req-' + uuid.uuid4().hex[:12], 'order_id': 'order-' + uuid.uuid4().hex[:12],
                   'duration_ms': duration, 'status': 'timeout' if failed else 'paid',
                   'error': 'downstream timeout' if failed else None}
            with sqlite3.connect(self.root / 'orders.sqlite3') as db:
                db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?)',
                           (row['order_id'], row['request_id'], region, env, data.get('amount', 100), row['status'], row['timestamp_utc']))
            file = (self.root / 'logs/legacy/payment-api-prod.jsonl' if self.state['mode'] == 'legacy'
                    else self.root / f'logs/regional/{service}/{region}/{env}/v2.jsonl')
            with file.open('a') as f:
                f.write(json.dumps(row) + '\n')
            return row

    def traffic(self, data):
        if data['profile'] not in ['normal', 'timeout'] or not 1 <= data['requests'] <= 100:
            raise ValueError('profile 应为 normal/timeout，requests 为 1—100')
        with self.lock:
            self.state['downstream_delay_ms'] = 95 if data['profile'] == 'timeout' else 5
            save(self.root / 'state.json', self.state)
            # 与 /pay 使用同一真实处理路径；lab 在客户端逐笔请求 /pay。
            return self.status()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def dispatch(self):
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        at = now()
        try:
            if self.command == 'GET':
                if parsed.path == '/health':
                    result = {'healthy': True, 'pid': os.getpid(), 'run': runtime().name}
                else:
                    result = self.server.lab.query(parsed.path, params)
            else:
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or '{}')
                if parsed.path != '/pay' and self.headers.get('Authorization') != 'Bearer ' + (runtime() / 'http.token').read_text().strip():
                    raise ValueError('管理操作需要本地课程令牌')
                if parsed.path == '/shutdown':
                    result = {'stopping': True}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif parsed.path == '/setup':
                    result = self.server.lab.setup()
                elif parsed.path == '/env':
                    result = self.server.lab.switch(data['mode'])
                elif parsed.path == '/profile':
                    result = self.server.lab.traffic(data)
                elif parsed.path == '/pay':
                    result = self.server.lab.payment(data)
                elif parsed.path == '/alert':
                    self.server.lab.scope(data)
                    save(self.server.lab.root / f"alerts/{data['region']}-{data['env']}.json", data)
                    result = data
                else:
                    raise ValueError('未知管理接口')
            code = 200
        except EntryRetired as e:
            code, result = 410, {'error': str(e)}
        except (ValueError, KeyError, FileNotFoundError, AttributeError) as e:
            code, result = 400, {'error': str(e)}
        body = json.dumps(result, ensure_ascii=False).encode()
        if parsed.path in ['/services', '/catalog', '/logs']:
            event = {'at': at, 'path': parsed.path, 'params': params, 'code': code,
                     'count': result.get('count'), 'response': result}
            with self.server.lab.lock:
                with (observations() / 'business-queries.jsonl').open('a') as f:
                    f.write(json.dumps(scrub(event), ensure_ascii=False) + '\n')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = dispatch


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.lab = PayLab()
    server.serve_forever()
