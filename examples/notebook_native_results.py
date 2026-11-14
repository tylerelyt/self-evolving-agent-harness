"""Collect one explicitly identified native exercise attempt, including failures."""
import json
from pathlib import Path
import re
import time
from uuid import uuid4

ARTIFACTS = ('version.json', 'investigation.json', 'review.json', 'review-process.json',
             'approval.json', 'adoption.json', 'future-beta.json', 'future-west.json',
             'future-single.json', 'failure.json', 'result.json', 'cleanup.json', 'audit.json')


def start_run(lesson, validation_id=None, run_dir=None):
    lesson = Path(lesson).resolve()
    base = lesson / 'runtime'
    base.mkdir(parents=True, exist_ok=True)
    run = Path(run_dir) if run_dir else base / ('native-' + str(time.time_ns()) + '-' + uuid4().hex[:8])
    if run.is_symlink() or run.resolve().parent != base.resolve():
        raise ValueError('Use a fresh direct child of this exercise runtime directory')
    run = run.resolve()
    run.mkdir(mode=0o700, exist_ok=False)
    marker = run / '.launch.tmp'
    marker.write_text(json.dumps({'schema_version': 1, 'attempt_id': run.name,
        'validation_run_id': validation_id, 'path': 'native-live'}))
    marker.replace(run / 'launch.json')
    return run


def collect_run(run, validation_id, returncode, process_error=None):
    run = Path(run)
    result = {'path': 'native-live', 'attempt_id': run.name, 'validation_run_id': validation_id,
              'run': str(run), 'returncode': returncode, 'process_error': process_error,
              'status': 'not-started', 'artifacts': {}, 'read_errors': {}, 'attempt_started': False}
    launch = run / 'launch.json'
    if run.is_symlink() or launch.is_symlink():
        raise ValueError('Attempt identity cannot use symbolic links')
    if not launch.is_file():
        result['status'] = 'failed' if returncode is not None or process_error else 'not-started'
        return result
    identity = json.loads(launch.read_text())
    if (identity.get('schema_version') != 1 or identity.get('path') != 'native-live'
            or identity.get('attempt_id') != run.name or identity.get('validation_run_id') != validation_id):
        raise ValueError('Native attempt identity mismatch')
    result['attempt_started'] = True
    for name in ARTIFACTS:
        path = run / name
        if path.is_symlink():
            raise ValueError('Native observation artifact cannot leave its attempt directory')
        if path.is_file():
            try:
                result['artifacts'][name] = json.loads(path.read_text())
            except (ValueError, OSError) as error:
                result['read_errors'][name] = type(error).__name__ + ': ' + str(error)
    final = result['artifacts'].get('result.json', {})
    status = final.get('status') if isinstance(final, dict) else None
    evidence = all(isinstance(result['artifacts'].get(name), dict) and result['artifacts'][name]
                   for name in ('investigation.json', 'review.json'))
    result['native_record_files_present'] = bool(evidence)
    if returncode == 0 and not process_error and not result['read_errors'] and evidence and status in ('passed', 'no-update', 'not-adopted'):
        result['status'] = status
    else:
        result['status'] = 'failed'
    result['available_traces'] = [name for name in ('subprocess.log', 'review.log', 'job.json', 'hermes/state.db')
                                  if (run / name).is_file() and not (run / name).is_symlink()]
    return result


def save_observation(lesson, result):
    run = Path(result['run'])
    if (run / 'launch.json').is_file():
        (run / 'observation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    identity = result['validation_run_id']
    if identity is not None:
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity):
            raise ValueError('Invalid validation identity')
        target = Path(lesson) / 'validation' / ('notebook-' + identity + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2))


def show_observation(result):
    """Show partial original records; missing stages remain explicitly missing."""
    print('本次状态：', result['status'], '；进程退出码：', result['returncode'])
    if result.get('process_error'):
        print('进程异常：', result['process_error'])
    for name in ('investigation.json', 'review.json', 'failure.json', 'result.json', 'cleanup.json'):
        if name not in result['artifacts']:
            print(name + '：本次尚未取得')
    for name, value in result['artifacts'].items():
        print('\n' + name + '：\n' + json.dumps(value, ensure_ascii=False, indent=2))
    review = result['artifacts'].get('review.json', {})
    before, after = (review.get('before', {}), review.get('after', {})) if isinstance(review, dict) else ({}, {})
    if isinstance(before, dict) and isinstance(after, dict):
        import difflib
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                print('\n本次文件差异：', name)
                print(''.join(difflib.unified_diff(str(before.get(name, '')).splitlines(True),
                    str(after.get(name, '')).splitlines(True), fromfile='before', tofile='after')))
    if result['read_errors']:
        print('尚不能解析的本次文件：', result['read_errors'])
    print('本次原始轨迹文件：', result.get('available_traces', []))
