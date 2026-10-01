#!/usr/bin/env python3
"""构造现场工具：所有结果读取独立/tmp状态，处置条件由工具校验。"""
import argparse
import fcntl
import json
import uuid
from common import save, now
from shared_store import business, SCOPE


def operate(action, instance, scope=SCOPE):
    if instance not in ('A', 'B'):
        raise ValueError('只有构造实例A和B')
    work = business()
    with (work / 'platform.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((work / 'state.json').read_text())
        current = state[instance]
        base = {'constructed': True, 'event_id': uuid.uuid4().hex, 'at': now(),
                'action': action, 'instance': instance, 'scope': SCOPE, 'version': 'v1'}
        if scope != SCOPE:
            result = {**base, 'status': 'denied', 'reason': '超出本实例业务范围'}
        elif action == 'report':
            result = {**base, 'fresh': current['fresh'], 'collected': True, 'status': 'ok'}
        elif action == 'ingestion':
            result = {**base, 'paused': current['paused'], 'status': 'ok'}
        elif action == 'upstream':
            result = {**base, 'field': current['field'], 'expected': current['expected'], 'status': 'ok'}
        elif action == 'resume':
            if not current['paused']:
                result = {**base, 'status': 'denied', 'reason': '入库正常，不能按旧案例恢复任务'}
            else:
                current['paused'] = False
                current['fresh'] = current['field'] == current['expected']
                result = {**base, 'status': 'completed', 'paused': False, 'fresh': current['fresh']}
        elif action == 'map-field':
            if current['paused'] or current['field'] == current['expected']:
                result = {**base, 'status': 'denied', 'reason': '当前条件不满足字段修正'}
            else:
                current['expected'] = current['field']; current['fresh'] = True
                result = {**base, 'status': 'completed', 'expected': current['expected'], 'fresh': True}
        else:
            raise ValueError('未知业务操作')
        save(work / 'state.json', state)
        with (work / 'audit.jsonl').open('a') as out:
            out.write(json.dumps({'action': action, 'instance': instance, 'result': result}, ensure_ascii=False) + '\n')
        return result


def main():
    parser = argparse.ArgumentParser(description='教学报表现场；所有实例名、字段和数值均为构造值。')
    parser.add_argument('action', choices=('report', 'ingestion', 'upstream', 'resume', 'map-field'))
    parser.add_argument('--instance', choices=('A', 'B'), required=True)
    parser.add_argument('--scope', default=SCOPE)
    args = parser.parse_args()
    try:
        print(json.dumps(operate(**vars(args)), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
