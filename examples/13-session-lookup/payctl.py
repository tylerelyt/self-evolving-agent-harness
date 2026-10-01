#!/usr/bin/env python3
"""构造支付平台：每项回执来自临时目录中的实际状态和操作。"""
import argparse
import fcntl
import json
from pathlib import Path
from common import runtime, now, save


def operate(action, **args):
    pointer = runtime() / 'business.json'
    if not pointer.exists():
        raise ValueError('业务现场尚未 setup，请先在另一个终端执行 run.sh setup')
    work = Path(json.loads(pointer.read_text())['directory'])
    with (work / 'platform.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((work / 'deployment.json').read_text())
        entry = 'legacy-payments' if state['layout'] == 'legacy' else 'regional-payments'
        base = {'constructed': True, 'at': now(), 'service': 'payment-api',
                'region': 'us-east', 'environment': 'prod', 'version': state['version']}
        if action == 'describe':
            visible = {k: v for k, v in state.items() if k != 'rollback_allowed'}
            result = {**base, **visible, 'documentation': str(work / 'operations.md')}
        elif action == 'catalog':
            result = ({**base, 'status': 'not_available', 'reason': '此部署尚无目录查询，统一入口见操作说明'}
                      if state['layout'] == 'legacy' else {**base, 'entry': entry, 'archive': 'incident-archive'})
        elif action == 'logs':
            selected = args.get('entry')
            if selected == 'legacy-payments' and state['layout'] != 'legacy':
                result = {**base, 'status': 'gone', 'code': 410, 'reason': '统一入口已退役，请查询目录'}
            elif selected not in (entry, 'incident-archive'):
                result = {**base, 'status': 'not_found', 'code': 404}
            else:
                file = 'archive.jsonl' if selected == 'incident-archive' else 'payments.jsonl'
                result = {**base, 'status': 'ok', 'rows': [json.loads(x) for x in (work / file).read_text().splitlines()]}
        elif action == 'rollback':
            if not state['rollback_allowed']:
                result = {**base, 'status': 'denied', 'task_created': False,
                          'reason': '当前身份没有执行权限，未创建发布任务', 'requested_version': args.get('version')}
            else:
                state['version'] = args['version']; save(work / 'deployment.json', state)
                result = {**base, 'version': state['version'], 'status': 'completed', 'task_created': True}
        elif action == 'pause':
            if args.get('batch') != 'repeat-pay':
                raise ValueError('不存在这个构造批任务')
            state['batch_active'] = False
            save(work / 'deployment.json', state)
            # 简化模型：重复付款停止后，后续构造请求不再出现该类冲突。
            add_traffic(work, count=10, failing=False)
            result = {**base, 'status': 'completed', 'batch': 'repeat-pay', 'batch_active': False}
        else:
            raise ValueError('未知操作')
        event = {'at': now(), 'action': action, 'args': args, 'result': result}
        with (work / 'audit.jsonl').open('a') as out:
            out.write(json.dumps(event, ensure_ascii=False) + '\n')
        return result


def add_traffic(work, count, failing):
    with (work / 'payments.jsonl').open('a') as out:
        for i in range(count):
            out.write(json.dumps({'constructed': True, 'at': now(), 'request': 'sample-' + str(i),
                                  'status': 409 if failing and i % 2 == 0 else 200,
                                  'reason': '重复付款请求' if failing and i % 2 == 0 else '付款成功'}, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser(description='构造支付平台，只操作本讲 runtime 内的独立现场；所有数值为演示值。')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('describe', help='查询实际部署与操作说明位置')
    sub.add_parser('catalog', help='按本例 payment-api/us-east/prod 查询当前入口和归档')
    logs = sub.add_parser('logs', help='读取目录返回的日志入口'); logs.add_argument('entry')
    rollback = sub.add_parser('rollback', help='提交回滚请求，平台执行权限校验'); rollback.add_argument('--version', required=True)
    pause = sub.add_parser('pause', help='暂停明确获准的构造批任务'); pause.add_argument('batch')
    args = vars(parser.parse_args()); action = args.pop('action')
    try:
        print(json.dumps(operate(action, **args), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as e:
        parser.exit(1, str(e) + '\n')


if __name__ == '__main__':
    main()
