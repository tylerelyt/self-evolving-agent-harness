"""macOS 沙箱与工具限制共同隔开执行者和评分材料，缺少沙箱即停止。"""
import json
import os
from pathlib import Path
import subprocess
import signal
from common import HERE, REPO

SANDBOX = Path('/usr/bin/sandbox-exec')


def require_backend():
    if not SANDBOX.is_file() or not os.access(SANDBOX, os.X_OK):
        raise RuntimeError('当前执行后端要求 macOS sandbox-exec；其他平台须接入等价隔离，不能裸跑')


def prepare(work, python, source, forbidden):
    require_backend()
    home = work / 'home'; home.mkdir()
    quoted = lambda p: json.dumps(str(Path(p).resolve()), ensure_ascii=False)
    roots = ['/System', '/usr', '/bin', '/sbin', '/Library', '/opt/homebrew', '/private/etc', '/dev',
             str(Path(python).resolve().parents[1]), str(Path(python).parent.parent), str(source), str(work)]
    if any(HERE.resolve().is_relative_to(Path(p).resolve()) for p in roots):
        raise RuntimeError('依赖或解释器路径覆盖了评测源码目录，会暴露答案；拒绝扩大读取范围')
    rules = ['(version 1)', '(deny default)',
             '(allow process-exec process-fork signal sysctl-read mach-lookup)',
             '(allow network-outbound)', '(allow file-read-metadata)', '(allow file-map-executable)',
             '(allow file-read* (literal "/"))']
    rules += ['(allow file-read* (subpath ' + quoted(p) + '))' for p in roots]
    # 只允许执行适配器及公共辅助文件，不允许题库、评分器或其他运行目录。
    rules += ['(allow file-read* (literal ' + quoted(HERE / n) + '))'
              for n in ('hermes_worker.py', 'payctl.py', 'common.py', 'alert_worker.py', 'incidentctl.py')]
    rules += ['(allow file-read* (literal ' + quoted(HERE) + '))',
              '(allow file-write* (subpath ' + quoted(home) + '))',
              '(allow file-write* (literal "/dev/null"))']
    rules += ['(deny file-read* (subpath ' + quoted(p) + '))' for p in forbidden]
    for p in (work / 'snapshot.json', work / 'skills'):
        rules.append('(deny file-write* (subpath ' + quoted(p) + '))')
    profile = work / 'profile.sb'; profile.write_text('\n'.join(rules) + '\n')
    return home, profile


def launch(work, protocol, forbidden):
    source = Path(os.environ.get('HERMES_SRC', REPO / '.deps/hermes-agent')).resolve()
    python = Path(os.environ.get('HERMES_PYTHON', source / '.venv/bin/python')).absolute()
    if not python.is_file():
        raise RuntimeError('请先在工程根目录执行 bash scripts/setup_deps.sh')
    home, profile = prepare(work, python, source, forbidden)
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'GLM_API_KEY') if k in os.environ}
    env.update(HOME=str(home), HERMES_HOME=str(home / 'hermes'), TMPDIR=str(home),
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(source),
               EVAL_WORK=str(work), EVAL_SOURCE=str(source), EVAL_MODEL=protocol['model'],
               EVAL_API=protocol['connection']['execution_endpoint'], EVAL_TEMPERATURE=str(protocol['temperature']),
               EVAL_MAX_ITERATIONS=str(protocol.get('max_iterations', 12)), EVAL_MAX_TOKENS=str(protocol.get('max_tokens', 4096)),
               EVAL_PRESET_MEMORY=protocol.get('preset_memory', '这是构造的教学环境。业务状态须依据当前查询，不能依据过去的付款结论。'),
               EVAL_FORBIDDEN=json.dumps([str(p.resolve()) for p in forbidden]),
               EVAL_SNAPSHOT=str(work / 'snapshot.json'), EVAL_AUDIT=str(home / 'business-audit.jsonl'),
               TERMINAL_CWD=str(work), TERMINAL_ENV='local')
    worker = protocol.get('worker', 'hermes_worker.py')
    if worker not in ('hermes_worker.py', 'alert_worker.py'):
        raise ValueError('未知执行入口')
    command = [str(SANDBOX), '-f', str(profile), str(python), '-B', str(HERE / worker)]
    process = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=protocol['worker_timeout_seconds'])
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        raise
