#!/usr/bin/env python3
"""启动、环境准备和发请求分开；本文件不调用模型或写长期记忆。"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from common import HERE, BASE, ACTIVE, runtime, observations, save, record, api, now, scrub


def up(resume=False):
    if ACTIVE.exists():
        try:
            status = api('/status')
        except (OSError, RuntimeError):
            status = None
        if status:
            raise ValueError('已有本讲服务在线，请先 down；不会覆盖它的入口')
    if not os.environ.get('GLM_API_KEY'):
        raise ValueError('需要 GLM_API_KEY；无凭据回放请设置 LAB_VALIDATION_RUN_ID 后运行 demo')
    run = runtime() if resume else BASE / 'runs' / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
    run.mkdir(parents=True, exist_ok=resume)
    save(ACTIVE, {'run': str(run.relative_to(BASE))})
    os.environ['SESSION_LAB_RUNTIME'] = str(run)
    token = run / 'http.token'; token.write_text(secrets.token_hex(24)); token.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    obs = observations(); obs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy(); env.pop('BIGMODEL_API_KEY', None)
    with (obs / 'server.log').open('a') as log:
        python = os.environ.get('HERMES_PYTHON', str(HERE.parents[1] / '.deps/hermes-agent/.venv/bin/python'))
        env['PYTHONPATH'] = str(Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent'))) + os.pathsep + env.get('PYTHONPATH', '')
        process = subprocess.Popen([python, '-B', str(HERE / 'oncall_server.py'),
                                    '--port', str(port), '--runtime', str(run)],
                                   cwd=HERE, env=env, stdout=log, stderr=log, start_new_session=True)
    save(run / 'processes.json', {'oncall': {'pid': process.pid, 'port': port}})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('服务启动失败，详情见 ' + str(obs / 'server.log'))
        try:
            status = api('/status'); print('服务已在线：', scrub(status)); return status
        except (OSError, RuntimeError):
            time.sleep(.25)
    process.terminate(); process.wait(timeout=10)
    raise TimeoutError('健康检查超时，已停止本次进程')


def setup():
    if (runtime() / 'business.json').exists():
        raise ValueError('本次现场已经存在；重新演练请新建服务')
    work = Path(tempfile.mkdtemp(prefix='hermes-oncall-memory-', dir='/private/tmp'))
    save(runtime() / 'business.json', {'directory': str(work), 'constructed': True})
    save(work / 'state.json', {x: {'fresh': False, 'paused': True, 'field': 'amount_v1', 'expected': 'amount_v1'} for x in ('A', 'B')})
    (work / 'audit.jsonl').touch()
    from shared_store import schema, backend, SCOPE
    import sqlite3
    schema()
    manual = {'id': 'manual-report-v1', 'scope': SCOPE, 'kind': 'manual', 'version': 'v1',
              'source': {'instance': '教学手册', 'session': None, 'time': now()},
              'content': '报表未更新时先查采集与入库，再核对上游字段和当前映射。旧案例须逐项核对当前条件。', 'constructed': True}
    records = [manual,
               {**manual, 'id': 'manual-other-scope', 'scope': 'inventory-prod-v1', 'content': '其他业务报表资料，当前实例不可读。'},
               {**manual, 'id': 'manual-old-version', 'scope': 'report-prod-v0', 'version': 'v0', 'content': '旧版本报表资料，不能套用v1。'}]
    with sqlite3.connect(backend()) as db:
        db.executemany('INSERT INTO records VALUES (?,?,?,?)', [(r['id'], r['scope'], r['kind'], json.dumps(r,ensure_ascii=False,sort_keys=True)) for r in records])
    record(observations() / 'setup.json', {'directory': str(work), 'constructed': True, 'manuals': records})
    print('独立/tmp教学现场与共享SQLite已准备；实例、字段、版本和数值均为构造值。')
    return work


def change(kind='different'):
    from shared_store import business
    work = business()
    state = json.loads((work / 'state.json').read_text())
    if kind == 'different':
        state['B'] = {'fresh': False, 'paused': False, 'field': 'amount_v2', 'expected': 'amount_v1'}
    elif kind == 'timeout':
        state['A'] = {'fresh': False, 'paused': True, 'field': 'amount_v1', 'expected': 'amount_v1'}
    elif kind == 'write-fail':
        state['B'] = {'fresh': False, 'paused': False, 'field': 'amount_v3', 'expected': 'amount_v2'}
    else:
        raise ValueError('未知构造环境变化')
    save(work / 'state.json', state)
    record(observations() / ('change-' + kind + '.json'), {'kind': kind, 'state': state, 'constructed': True})
    print('现场已改变：', kind, '；未修改任何记忆。')
    return state


def send(sid, message, instance='A'):
    try:
        api('/sessions/' + sid)
    except RuntimeError:
        api('/sessions', {'id': sid, 'instance': instance})
    task = api('/tasks', {'session': sid, 'message': message})
    print('请求已排队：', task['id'], '会话：', sid)
    return task['id']


def wait(tid, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = api('/tasks/' + tid)
        reviews = api('/reviews?' + urlencode({'after_task': tid}))
        if task['status'] in ('failed', 'interrupted') and not any(r['status'] == 'running' for r in reviews):
            raise RuntimeError('前台失败且没有仍在运行的复盘；原始错误已保存，不继续等待不存在的后台')
        if task['status'] not in ('queued', 'running') and reviews and all(r['status'] != 'running' for r in reviews):
            print('前台：', task['status'], '；后台：', ', '.join(r['status'] for r in reviews))
            if task['status'] != 'completed' or any(r['status'] != 'completed' for r in reviews):
                raise RuntimeError('任务或复盘失败，原始结果已保存，不用人工记忆代替')
            print(task['result']['final_response'])
            provider = api('/provider')
            record(observations() / 'provider-checkpoints' / (tid + '.json'), provider)
            print('共享同步回执：', [(r['id'], r['status'], r['error']) for r in provider['pending']])
            return task, reviews
        time.sleep(.5)
    raise TimeoutError('等待超时；服务继续保留，不能把超时当作任务未执行')


def show_memory(tid=None):
    current = api('/memory')['content']
    if tid:
        for e in api('/reviews?' + urlencode({'after_task': tid})):
            print('修订前：\n' + (e['memory_before'] or '（空）'))
            print('Memory 调用与回执：', json.dumps([c for c in e.get('calls', []) if c['name'] == 'memory'], ensure_ascii=False, indent=2))
            print('修订后：\n' + (e['memory_after'] or '（空）'))
            print(e['diff'] or '本次未修改。')
    else:
        print(current or '（空）')
    return current


def down(timeout=600):
    # 长时间等待期间不随 active.json 切换到另一轮服务。
    os.environ['SESSION_LAB_RUNTIME'] = str(runtime())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = api('/shutdown', {})
            meta = json.loads((runtime() / 'processes.json').read_text())['oncall']
            for _ in range(100):
                with socket.socket() as sock:
                    if sock.connect_ex(('127.0.0.1', meta['port'])) != 0:
                        record(observations() / 'shutdown.json', {**result, **meta, 'port_closed': True})
                        print('本次服务已停止，端口已释放；日志和临时现场保留。'); return
                time.sleep(.1)
            raise TimeoutError('停止已受理，但端口尚未释放')
        except RuntimeError as e:
            if '仍有前台或后台任务' not in str(e): raise
        time.sleep(.5)
    raise TimeoutError('后台尚未结束，未强制中断服务；请稍后 down')


def main():
    parser = argparse.ArgumentParser(description='第14讲常驻服务：启动、现场、请求相互独立')
    sub = parser.add_subparsers(dest='action', required=True)
    for cmd in ('up', 'setup', 'down', 'status', 'provider', 'restart'): sub.add_parser(cmd)
    changer = sub.add_parser('change'); changer.add_argument('kind', choices=('different', 'timeout', 'write-fail'), nargs='?', default='different')
    fault = sub.add_parser('fault'); fault.add_argument('kind', choices=('off', 'read-timeout', 'write-fail'))
    sender = sub.add_parser('send'); sender.add_argument('--instance', choices=('A', 'B'), default='A'); sender.add_argument('--session', required=True); sender.add_argument('message')
    waiter = sub.add_parser('wait'); waiter.add_argument('task'); waiter.add_argument('--timeout', type=int, default=600)
    trace = sub.add_parser('trace'); trace.add_argument('task')
    memory = sub.add_parser('memory'); memory.add_argument('--after-task')
    lookup = sub.add_parser('lookup'); lookup.add_argument('--query', default='rollback')
    args = parser.parse_args()
    try:
        if args.action == 'up': up()
        elif args.action == 'setup': setup()
        elif args.action == 'change': change(args.kind)
        elif args.action == 'fault': api('/faults', {'read_timeout': args.kind == 'read-timeout', 'write_fail': args.kind == 'write-fail'})
        elif args.action == 'provider': print(json.dumps(api('/provider'), ensure_ascii=False, indent=2))
        elif args.action == 'restart': down(); up(resume=True)
        elif args.action == 'send': send(args.session, args.message, args.instance)
        elif args.action == 'wait': wait(args.task, args.timeout)
        elif args.action == 'trace': print(json.dumps(api('/tasks/' + args.task), ensure_ascii=False, indent=2))
        elif args.action == 'memory': show_memory(args.after_task)
        elif args.action == 'lookup': print(json.dumps(api('/lookup?' + urlencode({'query': args.query})), ensure_ascii=False, indent=2))
        elif args.action == 'down': down()
        elif args.action == 'status': print(api('/status'))
    except (ValueError, RuntimeError, TimeoutError, OSError) as e:
        parser.exit(1, str(e) + '\n')


if __name__ == '__main__': main()
