#!/usr/bin/env python3
"""练习者 CLI：通过 HTTP 发消息、等待和观察，不直接执行 Agent。"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote
from common import api, connection, http, observations, record, runtime, scrub, memory_diff


def wait_task(tid, timeout):
    deadline = time.monotonic() + timeout
    while True:
        task = api('/tasks/' + quote(tid))
        if task['status'] not in ['queued', 'running']:
            show_task(task)
            if task['status'] != 'completed':
                raise ValueError('任务未正常完成，见任务返回；不能算调查成功')
            return task
        if time.monotonic() >= deadline:
            raise ValueError('前台等待超时；任务仍可能运行，可用 task wait 继续观察')
        time.sleep(.5)


def wait_review(tid, timeout):
    deadline = time.monotonic() + timeout
    while True:
        events = api('/reviews?after_task=' + quote(tid))
        if events and all(e['status'] != 'running' for e in events):
            show_review(events)
            if any(e['status'] != 'completed' for e in events):
                raise ValueError('后台复盘未正常完成；保留真实失败回执')
            return events
        if time.monotonic() >= deadline:
            raise ValueError('未观察到复盘完成；超时只结束等待，不补写、不重试、不停止原生线程')
        time.sleep(.5)


def show_task(task):
    print(f"任务 {task['id']} / Session {task['session']}：{task['status']}")
    print('通过 HTTP 发出的完整用户消息：\n' + task['message'])
    result = task.get('result', {})
    if result.get('final_response'):
        print('\nAgent 回复：\n' + result['final_response'])
    if task.get('error') or result.get('error'):
        print('实际失败：', task.get('error') or result.get('error'))
    print('完整输入、前台调用与回执已保存到 observations/tasks。')


def show_trace(task):
    print(f"前台工具轨迹：{task['id']}")
    for i, c in enumerate(task.get('calls', []), 1):
        print(f"  调用 {i}：{c['name']}，编号 {c['tool_call_id']}")
        print('    命令：' + str(c['args'].get('command', c['args'])))
        receipt = c.get('receipt')
        if isinstance(receipt, dict):
            print(f"    同编号回执：退出码 {receipt.get('exit_code', '无')}；工具错误 {receipt.get('error') or '无'}。")
            output = receipt.get('output', '')
            try:
                payload = json.loads(output)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and 'records' in payload:
                errors = sum(row.get('status') == 'timeout' for row in payload['records'])
                print(f"    读到 {payload['count']} 条实际日志，其中 {errors} 条超时；入口 {payload['entry']}，范围 {payload['region']}/{payload['environment']}。")
            elif isinstance(payload, dict) and 'current' in payload:
                print(f"    目录返回：当前入口 {payload['current']}；历史归档 {payload['archive']}。")
            elif isinstance(payload, dict) and 'log_entry' in payload:
                print(f"    部署返回：{payload['mode']}，统一入口 {payload['log_entry'] or '无'}，目录已上线={payload['catalog_available']}。")
            else:
                print('    返回内容：\n' + str(output or receipt)[:1200])
        else:
            print('    同编号回执：' + str(receipt))
    print('完整参数和未裁剪回执见 observations/tasks 中对应任务文件。')


def show_review(events):
    print(f'观察到原生后台复盘 {len(events)} 次。')
    for event in events:
        print(f"复盘 {event['review_id']}：{event['status']}，父 Session {event['parent_session']}，触发任务 {event['task_id']}")
        print(f"  线程 {event['thread']}，daemon={event['daemon']}；记录的是原 worker 的实际执行。")
        for c in event.get('calls', []):
            operations = c['args'].get('operations') or [c['args']]
            for op in operations:
                print(f"  模型调用：{c['name']}(action={op.get('action')}, target={c['args'].get('target', 'memory')})")
                if op.get('old_text'):
                    print('    定位旧条目：' + op['old_text'])
                if op.get('content'):
                    print('    完整新内容：' + op['content'])
            receipt = c.get('receipt')
            if isinstance(receipt, dict):
                print(f"  同 tool_call_id={c['tool_call_id']} 回执：success={receipt.get('success')}，条目数 {receipt.get('entry_count', '未知')}；{receipt.get('message') or receipt.get('error') or receipt}。")
            else:
                print(f"  同 tool_call_id={c['tool_call_id']} 回执：{receipt}")
        print('  复盘说明：' + str(event.get('result', {}).get('final_response', '未取得正常返回')))
        for warning in event.get('warnings', []):
            print('  警告：' + warning)


parser = argparse.ArgumentParser(description='操作常驻 Hermes oncall service')
sub = parser.add_subparsers(dest='cmd', required=True)
sub.add_parser('status')
p = sub.add_parser('send')
p.add_argument('message', nargs='?', default='调查这条告警并说明现场依据。')
p.add_argument('--session', required=True)
p.add_argument('--alert')
p.add_argument('--attach', action='append', default=[])
p.add_argument('--id-only', action='store_true')
p = sub.add_parser('session').add_subparsers(dest='action', required=True)
q = p.add_parser('new')
q.add_argument('--id', required=True)
q = p.add_parser('inspect')
q.add_argument('id')
q.add_argument('--memory-snapshot', action='store_true')
p = sub.add_parser('task').add_subparsers(dest='action', required=True)
for action in ['show', 'wait']:
    q = p.add_parser(action)
    q.add_argument('id')
    q.add_argument('--timeout', type=int, default=300)
p = sub.add_parser('trace')
p.add_argument('id')
p = sub.add_parser('memory').add_subparsers(dest='action', required=True)
p.add_parser('show')
q = p.add_parser('diff')
q.add_argument('--after-task', required=True)
p = sub.add_parser('review').add_subparsers(dest='action', required=True)
for action in ['show', 'wait']:
    q = p.add_parser(action)
    q.add_argument('--after-task', required=True)
    q.add_argument('--timeout', type=int, default=300)
    q.add_argument('--with-tool-receipts', action='store_true')
p = sub.add_parser('report')
p.add_argument('--session', action='append', default=[])

if __name__ == '__main__':
    args = parser.parse_args()
    try:
        if args.cmd == 'status':
            status = api('/status')
            print(f"常驻服务在线：PID {status['pid']}，模型 {status['model']}；已观察 {status['reviews']} 次后台复盘。")
            print('教学 Home：' + status['home'])
        elif args.cmd == 'session':
            result = api('/sessions', {'id': args.id}) if args.action == 'new' else api('/sessions/' + quote(args.id))
            record(observations() / 'sessions' / (args.id + '-inspect.json'), result)
            print(f"Session {result['id']}，Agent 实例 {result['agent_instance']}，启动历史 {result['history_at_start']} 条，当前历史 {result['history_length']} 条。")
            print('启动时加载的 Memory：\n' + result['memory_at_start'])
            if getattr(args, 'memory_snapshot', False):
                print('当前 Agent Memory 快照：\n' + result['memory_snapshot'])
        elif args.cmd == 'send':
            message = args.message
            if args.alert:
                message += '\n\n告警详情（实际请求统计）：\n' + Path(args.alert).read_text()
            for attachment in args.attach:
                message += '\n\n正式说明全文：\n' + Path(attachment).read_text()
                state = http(connection('paylab') + '/status')
                message += '\n现场迁移生效回执（UTC）：' + state['effective_at'] + '；当前环境：' + state['mode']
            task = api('/tasks', {'session': args.session, 'message': message})
            print(task['id'] if args.id_only else f"消息已通过 HTTP 交给常驻服务，任务编号 {task['id']}。\n完整消息：\n{message}")
        elif args.cmd == 'task':
            wait_task(args.id, args.timeout) if args.action == 'wait' else show_task(api('/tasks/' + quote(args.id)))
        elif args.cmd == 'trace':
            show_trace(api('/tasks/' + quote(args.id)))
        elif args.cmd == 'review':
            wait_review(args.after_task, args.timeout) if args.action == 'wait' else show_review(api('/reviews?after_task=' + quote(args.after_task)))
        elif args.cmd == 'memory':
            if args.action == 'show':
                print('完整 MEMORY.md：\n' + (api('/memory')['content'] or '（空）'))
            else:
                events = api('/reviews?after_task=' + quote(args.after_task))
                for event in events:
                    print(memory_diff(event.get('memory_before', ''), event.get('memory_after', '')) or '这次复盘未产生记忆差异。')
        elif args.cmd == 'report':
            from acceptance import report
            result = report(args.session)
            print(result)
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        print(scrub(f'观察/请求未完成：{e}'), file=sys.stderr)
        sys.exit(1)
