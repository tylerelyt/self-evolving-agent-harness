#!/usr/bin/env python3
"""启动、环境准备和发请求分开；本文件不调用模型或写长期记忆。"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from common import HERE, BASE, ACTIVE, runtime, observations, save, record, api, scrub

_PROCESSES = {}


def up(on_created=None):
    if ACTIVE.exists():
        try:
            status = api('/status')
        except (OSError, RuntimeError):
            status = None
        if status:
            raise ValueError('已有本讲服务在线，请先 down；不会覆盖它的入口')
    if not os.environ.get('GLM_API_KEY'):
        raise ValueError('需要已批准的 GLM_API_KEY；无模型检查请运行 run.sh self-check')
    run = BASE / 'runs' / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
    run.mkdir(parents=True, mode=0o700)
    save(ACTIVE, {'run': str(run.relative_to(BASE))})
    os.environ['SESSION_LAB_RUNTIME'] = str(run)
    if on_created:
        on_created(run)
    token = run / 'http.token'; token.write_text(secrets.token_hex(24)); token.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    obs = observations(); obs.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy(); env.pop('BIGMODEL_API_KEY', None)
    with (obs / 'server.log').open('w') as log:
        python = os.environ.get('HERMES_PYTHON', str(HERE.parents[1] / '.deps/hermes-agent/.venv/bin/python'))
        env['PYTHONPATH'] = str(Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent'))) + os.pathsep + env.get('PYTHONPATH', '')
        process = subprocess.Popen([python, '-B', str(HERE / 'oncall_server.py'),
                                    '--port', str(port), '--runtime', str(run)],
                                   cwd=HERE, env=env, stdout=log, stderr=log, start_new_session=True)
    _PROCESSES[str(run)] = process
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
        raise ValueError('本次业务现场已经存在；重新演练请新建一次服务')
    work = runtime() / 'business'
    work.mkdir(parents=True)
    save(runtime() / 'business.json', {'directory': str(work), 'constructed': True})
    save(work / 'deployment.json', {'constructed': True, 'service': 'payment-api', 'region': 'us-east',
                                   'environment': 'prod', 'layout': 'regional', 'version': 'sample-v2',
                                   'batch_active': True, 'rollback_allowed': False})
    (work / 'payments.jsonl').touch(); (work / 'archive.jsonl').touch(); (work / 'audit.jsonl').touch()
    from payctl import add_traffic
    add_traffic(work, count=10, failing=True)
    (work / 'operations.md').write_text('所有对象与数值均为构造值。\n使用 payctl catalog 查询当前日志入口，再用 payctl logs 读取。\n本环境仅有 payment-api/us-east/prod，操作以当前部署和权限回执为准。\n')
    record(observations() / 'setup.json', {'directory': str(work), 'requests': 10, 'constructed': True})
    print('隔离现场：', work, '；构造付款10笔，其中5笔重复冲突，不连接生产。')
    return work


def send(sid, message):
    try:
        api('/sessions/' + sid)
    except RuntimeError:
        api('/sessions', {'id': sid})
    task = api('/tasks', {'session': sid, 'message': message})
    print('请求已排队：', task['id'], '会话：', sid)
    return task['id']


def wait(tid, timeout=600, *, allow_failed_source=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = api('/tasks/' + tid)
        reviews = api('/reviews?' + urlencode({'after_task': tid}))
        if (allow_failed_source and task.get('drained') and task['status'] in ('failed', 'interrupted')
                and all(r['status'] != 'running' for r in reviews)):
            print('源会话未成功；保留失败状态，继续从原始消息核查，不将它记作完成。')
            return task, reviews
        if (task['status'] in ('failed', 'interrupted') and not allow_failed_source
                and not any(r['status'] == 'running' for r in reviews)):
            raise RuntimeError('前台失败且没有仍在运行的复盘；原始错误已保存，不继续等待不存在的后台')
        if task.get('drained') and all(r['status'] != 'running' for r in reviews):
            print('前台：', task['status'], '；后台：', ', '.join(r['status'] for r in reviews))
            if task['status'] != 'completed' or any(r['status'] != 'completed' for r in reviews):
                raise RuntimeError('任务或复盘失败，原始结果已保存，不用人工记忆代替')
            print(task['result']['final_response'])
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


def down(timeout=600, run_path=None):
    # 长时间等待期间不随 active.json 切换到另一轮服务。
    os.environ['SESSION_LAB_RUNTIME'] = str(run_path or runtime())
    run = runtime()
    process_file = run / 'processes.json'
    if not process_file.exists():
        return {'service_started': False}
    meta = json.loads(process_file.read_text())['oncall']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process = _PROCESSES.get(str(run))
        if process is not None:
            process.poll()  # 回收本进程启动的子进程，避免把僵尸进程当作仍在线。
        with socket.socket() as sock:
            port_closed = sock.connect_ex(('127.0.0.1', meta['port'])) != 0
        cleanup_path = observations() / 'service-cleanup.json'
        if port_closed:
            cleanup = json.loads(cleanup_path.read_text()) if cleanup_path.exists() else {}
            verified = cleanup.get('completed') is True
            if verified and process is not None:
                process.wait(timeout=5)
            result = {**meta, 'port_closed': True, 'cleanup_verified': verified,
                      'process_exited': process.poll() is not None if process else None}
            record(observations() / 'shutdown.json', result)
            if not verified:
                raise RuntimeError('端口已关闭，但没有完整资源清理回执；不能标为正常停止')
            print('本次服务已完成资源清理并释放端口；运行记录保留。')
            return result
        try:
            api('/shutdown', {})
        except RuntimeError as e:
            if '仍有前台或后台任务' not in str(e): raise
        except OSError:
            # shutdown 已受理后，HTTP 可能先停止处理；下一轮只核查本次进程与回执。
            pass
        time.sleep(.5)
    record(observations() / 'shutdown.json', {**meta, 'cleanup_verified': False,
                                            'error': '清理等待超时；未强制中断正在执行的任务'})
    raise TimeoutError('后台尚未结束，未强制中断服务；请稍后 down')


def main():
    parser = argparse.ArgumentParser(description='第13讲常驻服务：启动、现场、请求相互独立')
    sub = parser.add_subparsers(dest='action', required=True)
    for cmd in ('up', 'setup', 'down', 'status'): sub.add_parser(cmd)
    sender = sub.add_parser('send'); sender.add_argument('--session', required=True); sender.add_argument('message')
    waiter = sub.add_parser('wait'); waiter.add_argument('task'); waiter.add_argument('--timeout', type=int, default=600)
    trace = sub.add_parser('trace'); trace.add_argument('task')
    memory = sub.add_parser('memory'); memory.add_argument('--after-task')
    lookup = sub.add_parser('lookup'); lookup.add_argument('--query', default='rollback')
    args = parser.parse_args()
    try:
        if args.action == 'up': up()
        elif args.action == 'setup': setup()
        elif args.action == 'send': send(args.session, args.message)
        elif args.action == 'wait': wait(args.task, args.timeout)
        elif args.action == 'trace': print(json.dumps(api('/tasks/' + args.task), ensure_ascii=False, indent=2))
        elif args.action == 'memory': show_memory(args.after_task)
        elif args.action == 'lookup': print(json.dumps(api('/lookup?' + urlencode({'query': args.query})), ensure_ascii=False, indent=2))
        elif args.action == 'down': down()
        elif args.action == 'status': print(api('/status'))
    except (ValueError, RuntimeError, TimeoutError, OSError) as e:
        parser.exit(1, str(e) + '\n')


if __name__ == '__main__': main()
