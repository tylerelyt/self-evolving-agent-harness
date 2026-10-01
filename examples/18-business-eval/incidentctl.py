"""执行端只读查询快照，不导入题库和参考答案。"""
import json
import os
from pathlib import Path
import shlex
import sys

COMMAND_HELP = ('工作目录已设置好；直接调用 ./incidentctl alerts|logs|traces CLUSTER SERVICE。'
                '每次只执行一条查询，无需也不允许 cd；禁止 &&、分号、管道、重定向、后台执行或更换 workdir。'
                'SERVICE=all 返回所选集群的全部服务；只读，不执行其他 shell 命令。')


def readonly_command(args):
    words = shlex.split(args.get('command', ''))
    if (args.get('background') or args.get('workdir') not in (None, '', os.environ.get('EVAL_WORK'))
        or len(words) != 4 or words[0] != './incidentctl' or words[1] not in ('alerts', 'logs', 'traces')
        or not all(w and all(c.isalnum() or c in '-_' for c in w) for w in words[2:])):
        raise PermissionError('只允许 ./incidentctl alerts|logs|traces CLUSTER SERVICE 单项只读查询')
    return shlex.join(words)


def query(snapshot, action, cluster, service):
    return {'constructed': True, 'snapshot_id': snapshot['snapshot_id'], 'action': action,
            'cluster_filter': cluster, 'service_filter': service,
            'records': [r for r in snapshot[action] if (cluster == 'all' or r['cluster'] == cluster)
                        and (service == 'all' or r['service'] == service)]}


def main():
    words = ['./incidentctl', *sys.argv[1:]]
    readonly_command({'command': shlex.join(words)})
    result = query(json.loads(Path(os.environ['EVAL_SNAPSHOT']).read_text()), *words[1:])
    with Path(os.environ['EVAL_AUDIT']).open('a') as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
