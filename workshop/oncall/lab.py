#!/usr/bin/env python3
"""教学进程与流量管理；不碰 Agent 记忆或复盘计数。"""
import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from common import ACTIVE, BASE, HERE, api, connection, http, now, observations, record, runtime, save, scrub, pay_admin

PYTHON = Path(os.environ.get('HERMES_PYTHON', HERE.parents[1] / '.deps/hermes-agent/.venv/bin/python'))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def up(args):
    from dotenv import dotenv_values
    if not dotenv_values(args.credentials_env).get('GLM_API_KEY'):
        raise ValueError('指定凭据文件缺少 GLM_API_KEY；不使用 BIGMODEL_API_KEY')
    if (runtime() / 'processes.json').exists() and not args.new_run:
        for kind in ['oncall', 'paylab']:
            try:
                http(connection(kind) + ('/health' if kind == 'paylab' else '/status'),
                     token=(runtime() / 'http.token').read_text().strip() if kind == 'oncall' else None)
            except OSError:
                continue
            raise ValueError('已有教学进程，请先 lab down；不会覆盖正在运行的环境')
    run = 'runs/run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    root = BASE / run
    root.mkdir(parents=True)
    save(ACTIVE, {'run': run})
    observations().mkdir(parents=True)
    token = secrets.token_urlsafe(32)
    (root / 'http.token').write_text(token)
    (root / 'http.token').chmod(0o600)
    ports = {kind: free_port() for kind in ['paylab', 'oncall']}
    meta = {}
    env = os.environ.copy()
    env.pop('BIGMODEL_API_KEY', None)
    env['ONCALL_RUNTIME'] = str(root)
    try:
        for kind in ['paylab', 'oncall']:
            script = 'paylab.py' if kind == 'paylab' else 'oncall_server.py'
            cmd = [str(PYTHON), '-B', str(HERE / script), '--port', str(ports[kind])]
            if kind == 'oncall':
                cmd += ['--credentials-env', str(Path(args.credentials_env).expanduser()),
                        '--model', args.model, '--base-url', args.base_url]
            with (observations() / (kind + '-server.log')).open('a') as out:
                proc = subprocess.Popen(cmd, env=env, cwd=HERE, stdout=out, stderr=out, start_new_session=True)
            meta[kind] = {'pid': proc.pid, 'port': ports[kind], 'script': script}
            save(root / 'processes.json', meta)
            deadline = time.monotonic() + 60
            while True:
                if proc.poll() is not None:
                    raise ValueError(f'{kind} 启动失败，见 observations 的服务日志')
                try:
                    http(f'http://127.0.0.1:{ports[kind]}/' + ('health' if kind == 'paylab' else 'status'),
                         token=token if kind == 'oncall' else None)
                    break
                except (RuntimeError, OSError):
                    if time.monotonic() >= deadline:
                        raise ValueError(f'{kind} 健康检查超时')
                    time.sleep(.3)
    except Exception:
        for item in meta.values():
            try:
                os.kill(item['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise
    print('常驻服务已就绪。健康检查未调用模型，不代表模型凭据已通过鉴权。')
    for kind, item in meta.items():
        print(f"  {kind}: PID {item['pid']}，HTTP 127.0.0.1:{item['port']}")
    print('  HERMES_HOME:', scrub(str(root / 'hermes')))
    print('  观察记录:', scrub(str(observations())))


def down(args):
    meta = json.loads((runtime() / 'processes.json').read_text())
    deadline = time.monotonic() + (args.timeout if args.wait_reviews else 0)
    while True:
        try:
            api('/shutdown', {})
            break
        except OSError:
            break
        except RuntimeError as e:
            if time.monotonic() >= deadline:
                record(observations() / 'shutdown.json', {'at': now(), 'status': 'not-stopped', 'reason': str(e)})
                raise ValueError('前台/复盘尚未结束：保留常驻服务；等待超时不会强杀线程')
            time.sleep(.5)
    for kind, item in meta.items():
        if kind == 'paylab':
            try:
                health = http(connection('paylab') + '/health')
                if health['pid'] != item['pid'] or health['run'] != runtime().name:
                    raise ValueError('支付端口已被其他进程使用，未停止')
                pay_admin('/shutdown', {})
            except OSError:
                pass
        # HTTP 健康入口与本地令牌确认归属；等退出后再报告收尾。
        until = time.monotonic() + 10
        while time.monotonic() < until:
            try:
                http(connection(kind) + ('/health' if kind == 'paylab' else '/status'),
                     token=(runtime() / 'http.token').read_text().strip() if kind == 'oncall' else None)
            except OSError:
                break
            time.sleep(.2)
        else:
            raise ValueError(f'{kind} 收尾仍在运行，保留进程，请稍后 lab down')
    record(observations() / 'shutdown.json', {'at': now(), 'status': 'stopped'})
    print('已停止本次教学服务，订单、日志、会话和观察记录均保留。')


def traffic(args):
    base = connection('paylab')
    scope = {'service': args.service, 'region': args.region, 'env': args.env}
    pay_admin('/profile', {**scope, 'profile': args.profile, 'requests': args.requests})
    since = now()
    rows = [http(base + '/pay', {**scope, 'amount': 100}) for _ in range(args.requests)]
    until = now()
    errors = sum(r['status'] == 'timeout' for r in rows)
    state = http(base + '/status')
    alert = {**scope, 'since': since, 'until': until, 'requests': len(rows),
             'failures': errors, 'failure_rate': errors / len(rows),
             'max_duration_ms': max(r['duration_ms'] for r in rows), 'generated_at': until,
             'source': '教学支付服务实际请求与延迟统计'}
    if args.region == 'us-east' and args.env == 'prod':
        alert['monitor_reference'] = {'entry': 'payment-api-prod',
            'source': '迁移前监控配置采样，仅供核对来源，不保证当前有效',
            'collected_at': state['monitor_collected_at']}
    pay_admin('/alert', alert)
    record(observations() / 'traffic' / f'{args.region}-{args.env}-{time.time_ns()}.json',
           {'alert': alert, 'actual_payments': rows})
    print(f"已实际发送 {len(rows)} 笔虚构付款；{errors} 笔超时，最高耗时 {alert['max_duration_ms']} ms。")
    print(f'日志时段（UTC）：{since} 至 {until}')
    print(f'告警文件：runtime 活动环境/paylab/alerts/{args.region}-{args.env}.json')


parser = argparse.ArgumentParser(description='管理常驻教学服务与真实支付现场')
sub = parser.add_subparsers(dest='cmd', required=True)
p = sub.add_parser('up')
p.add_argument('--new-run', action='store_true', help='调试时明确创建独立环境，保留已有环境与进程')
p.add_argument('--credentials-env', default=str(Path.home() / '.hermes/.env'))
p.add_argument('--model', default=os.environ.get('GLM_MODEL', 'glm-5.2'))
p.add_argument('--base-url', default=os.environ.get('GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4'))
p = sub.add_parser('down')
p.add_argument('--wait-reviews', action='store_true')
p.add_argument('--timeout', type=int, default=300)
sub.add_parser('status')
sub.add_parser('setup')
p = sub.add_parser('env').add_subparsers(dest='action', required=True).add_parser('use')
p.add_argument('mode', choices=['legacy', 'regional'])
p = sub.add_parser('traffic')
p.add_argument('--service', default='payment-api')
p.add_argument('--region', required=True)
p.add_argument('--env', required=True)
p.add_argument('--profile', choices=['normal', 'timeout'], required=True)
p.add_argument('--requests', type=int, default=12)
p = sub.add_parser('alert').add_subparsers(dest='action', required=True).add_parser('latest')
p.add_argument('--region', required=True)
p.add_argument('--env', required=True)

if __name__ == '__main__':
    args = parser.parse_args()
    try:
        if args.cmd == 'up':
            up(args)
        elif args.cmd == 'down':
            down(args)
        elif args.cmd == 'traffic':
            traffic(args)
        else:
            base = connection('paylab')
            if args.cmd == 'setup':
                result = pay_admin('/setup', {})
                print('临时支付部署已就绪：', result['directory'])
                print('日志与订单在 /tmp；切换命令：./lab env use regional')
            elif args.cmd == 'env':
                result = pay_admin('/env', {'mode': args.mode})
                record(observations() / ('environment-' + args.mode + '.json'), result)
                print(f"环境已原子切换为 {result['mode']}，生效时间 {result['effective_at']}；未访问 Agent 记忆或历史。")
            elif args.cmd == 'alert':
                result = http(base + '/alert?' + urlencode({'service': 'payment-api', 'region': args.region, 'env': args.env}))
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                result = {'paylab': http(base + '/status'), 'oncall': api('/status')}
                print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError) as e:
        print(scrub(f'实验管理未完成：{e}'), file=sys.stderr)
        sys.exit(1)
