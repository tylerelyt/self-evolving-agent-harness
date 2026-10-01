"""Hermes 解释器内的分幕执行器；只通过本机管道传递结果。"""
import contextlib
import io
import json
import sys
from common import scrub, record
from demo import Story
import atexit
import signal

story = None
finished = False


def cleanup():
    if story is not None and not finished and not story.offline:
        import lab
        try:
            lab.down(timeout=30)
        except Exception as exc:
            record(story.out / 'cleanup-failure.json', {'error_type': type(exc).__name__,
                   'note': '未确认本次服务关闭，须按本次运行目录单独核查；未误报清理成功。'})


atexit.register(cleanup)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
for line in sys.stdin:
    command = json.loads(line)['action']
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if command == 'prepare':
                story = Story()
                result = {'observations': str(story.out), 'runtime': str(story.run)}
            elif command in ('baseline', 'incident', 'audit', 'finish'):
                result = getattr(story, command)()
                if command == 'finish':
                    finished = True
            else:
                raise ValueError('未知分幕动作')
        response = {'ok': True, 'output': output.getvalue(), 'result': result}
    except Exception as exc:
        response = {'ok': False, 'output': output.getvalue(), 'error': f'{type(exc).__name__}: {exc}'}
        if story is not None:
            record(story.out / 'failure.json', {'error': response['error'], 'source_hashes': story.version})
    print(json.dumps(scrub(response), ensure_ascii=False), flush=True)
    if command == 'finish':
        break
