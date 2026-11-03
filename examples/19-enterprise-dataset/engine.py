"""Skill Up SessionInput/SessionResult 适配，独立进程中运行真实 Hermes。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from dataset import HERE, digest, file_digest, inside, read, save

SOURCE = HERE.parents[1] / '.deps/hermes-agent'


def dependency():
    source = Path(os.environ.get('HERMES_SRC', SOURCE)).resolve()
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != read(HERE / 'dependencies.json')['hermes_commit']:
        raise RuntimeError('Hermes 版本不符')
    changes = subprocess.check_output(
        ['git', '--no-optional-locks', '-C', str(source), 'status', '--porcelain=v1',
         '--untracked-files=no', '--ignore-submodules=none'], text=True)
    if changes.strip():
        raise RuntimeError('Hermes 已跟踪文件存在改动，拒绝运行；请使用指定提交的干净工作树')
    return source, source / '.venv/bin/python'


def profile(work, source, python, extra_roots=()):
    if not Path('/usr/bin/sandbox-exec').is_file():
        raise RuntimeError('当前隔离实现仅支持 macOS sandbox-exec，其他平台拒绝裸跑')
    quote = lambda p: json.dumps(str(Path(p).resolve()), ensure_ascii=False)
    roots = ['/System', '/usr', '/bin', '/sbin', '/Library', '/opt/homebrew', '/private/etc', '/dev',
             python.resolve().parents[1], source, work, *extra_roots]
    if any(HERE.is_relative_to(Path(p).resolve()) for p in roots):
        raise RuntimeError('依赖路径会放开题库读取，拒绝执行')
    rules = ['(version 1)', '(deny default)', '(allow process-exec process-fork signal sysctl-read mach-lookup)',
             '(allow network-outbound)', '(allow file-read-metadata)', '(allow file-map-executable)',
             '(allow file-read* (literal "/"))']
    rules += ['(allow file-read* (subpath ' + quote(p) + '))' for p in roots]
    rules += ['(allow file-read* (literal ' + quote(HERE / name) + '))' for name in ('worker.py', 'monitor.py')]
    rules += ['(allow file-read* (literal ' + quote(HERE) + '))',
              '(allow file-write* (subpath ' + quote(work / 'home') + '))',
              '(allow file-write* (literal "/dev/null"))']
    return '\n'.join(rules) + '\n'


def execute(payload, skill, forbidden, probe=False, observe=False, max_iterations=12):
    if not 1 <= max_iterations <= 50:
        raise ValueError('模型轮数上限须为1至50')
    source, python = dependency()
    workspace = inside(payload['workspace'])
    messages = payload['messages']
    if not messages or messages[-1]['role'] != 'user' or any(m['role'] not in ('user', 'assistant') or not isinstance(m['content'], str) for m in messages):
        raise ValueError('SessionInput 须为以 user 结束的文本对话')
    runtime = HERE / 'runtime/actors'
    runtime.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='actor-', dir=runtime))
    (work / 'home').mkdir()
    save(work / 'messages.json', messages)
    shutil.copyfile(workspace / 'snapshot.json', work / 'snapshot.json')
    target = work / 'skills/oncall-investigation/SKILL.md'
    target.parent.mkdir(parents=True)
    shutil.copyfile(skill, target)
    extra_roots = []
    if observe:
        sdk_python = HERE / '.deps/sdk/bin/python'
        sdk_site = subprocess.check_output([str(sdk_python), '-c',
            'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip()
        extra_roots.append(Path(sdk_site))
    profile_path = work / 'profile.sb'
    profile_path.write_text(profile(work, source, python, extra_roots))
    forbidden = [Path(p).resolve() for p in forbidden]
    if not forbidden or not all(p.is_file() for p in forbidden):
        raise ValueError('隔离探测必须针对实际存在的评分材料')
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TZ') if k in os.environ}
    if not probe:
        for key in ('GLM_API_KEY', 'GLM_BASE_URL', 'LECTURE19_ALLOW_MODEL'):
            if not os.environ.get(key):
                raise RuntimeError('须显式设置 ' + key)
            env[key] = os.environ[key]
    env.update(HOME=str(work / 'home'), HERMES_HOME=str(work / 'home/hermes'), TMPDIR=str(work / 'home'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(source), ACTOR_WORK=str(work),
               FORBIDDEN_READS=json.dumps([str(p) for p in forbidden]), EVAL_MODEL=os.environ.get('EVAL_MODEL', 'glm-5.2'),
               EVAL_MAX_ITERATIONS=str(max_iterations))
    if observe:
        for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL'):
            if not os.environ.get(key):
                raise RuntimeError('原生采集须显式设置 ' + key)
            env[key] = os.environ[key]
        env.update(PYTHONPATH=os.pathsep.join([str(source), sdk_site]),
                   LECTURE19_OBSERVE='1', LANGFUSE_EXPECTED_VERSION=read(HERE / 'dependencies.json')['langfuse'],
                   HERMES_LANGFUSE_SAMPLE_RATE='1.0', HERMES_LANGFUSE_MAX_CHARS='1000000',
                   HERMES_LANGFUSE_DEBUG='true')
    command = ['/usr/bin/sandbox-exec', '-f', str(profile_path), str(python), '-B', str(HERE / 'worker.py')]
    if probe:
        command.append('--probe')
    process = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=600)
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        raise
    (work / 'process.log').write_text(stdout + stderr)
    path = work / 'home/execution.json'
    result = read(path) if path.exists() else {'status': 'infrastructure_error', 'final_message': '', 'receipts': [], 'error': '执行进程未返回结果'}
    if process.returncode:
        result['status'] = 'infrastructure_error'
    result.update(skill_sha256=file_digest(target), snapshot_sha256=file_digest(work / 'snapshot.json'),
                  snapshot_content_sha256=digest(read(work / 'snapshot.json')),
                  actor_directory=str(work), hermes_commit=read(HERE / 'dependencies.json')['hermes_commit'])
    return result


def main():
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--skill', type=Path, required=True)
    parser.add_argument('--pack', type=Path, required=True)
    parser.add_argument('--receipts', type=Path)
    args = parser.parse_args()
    payload = read(args.input)
    try:
        result = execute(payload, args.skill, [args.pack, HERE / 'grader.py', HERE / 'fixtures/annotations.json'])
    except Exception as exc:
        result = {'status': 'infrastructure_error', 'final_message': '', 'receipts': [], 'error': type(exc).__name__ + ': ' + str(exc)}
    save(inside(Path(payload['workspace']) / 'execution.json'), result)
    if args.receipts:
        name = Path(result.get('actor_directory', '')).name or 'failed'
        save(inside(args.receipts) / (name + '.json'), result)
    save(inside(args.output), {'exit_code': 0 if result['status'] == 'completed' else 70,
                              'final_message': result['final_message'],
                              'artifacts': {'files': [{'name': 'execution.json', 'path': 'execution.json'}]}})


if __name__ == '__main__':
    main()
