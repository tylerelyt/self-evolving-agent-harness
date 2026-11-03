"""生成原生 Skill Up 配置；默认仅校验，--live 才允许真实模型调用。"""
import argparse
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

from dataset import HERE, digest, file_digest, inside, read, save, verify
from engine import dependency, execute
from worker import OUTPUT_PROTOCOL, request_options


def materialize(pack_path, skill, output, split):
    pack = read(pack_path)
    verify(pack)
    selected = [c for c in pack['ready'] if c['split'] == split]
    if not selected:
        raise ValueError('该分组没有可运行用例；pending 不得进入评测')
    configs = []
    for case in selected:
        fixture = pack_path.parent / 'fixtures' / case['id']
        if read(fixture / 'snapshot.json') != case['snapshot'] or read(fixture / 'task.json') != case['input']:
            raise ValueError('公开输入与评测包不一致')
        judge = output / 'judges' / (case['id'] + '.sh')
        judge.parent.mkdir(parents=True, exist_ok=True)
        judge.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-B', str(HERE / 'grader.py'),
                         '--pack', str(pack_path), '--case', case['id']]) + '\n')
        config = output / 'cases' / (case['id'] + '.yaml')
        # JSON 是 YAML 子集，保持生成器只依赖 Python 标准库。
        save(config, {'id': case['id'], 'input': case['input'], 'context': {'repo_fixture': os.path.relpath(fixture, output)},
                      'constraints': {'timeout_seconds': 650}, 'expect': {'exit_code': 0},
                      'judge': {'type': 'script', 'script_path': os.path.relpath(judge, output), 'timeout_seconds': 30}})
        configs.append(os.path.relpath(config, output))
    config = read(HERE / 'evals/eval.yaml')
    config['engine']['custom']['local'].update(command=sys.executable,
        args=['-B', str(HERE / 'engine.py'), '--input', '${input_file}', '--output', '${output_file}',
              '--pack', str(pack_path), '--skill', str(skill), '--receipts', str(output / 'receipts')])
    config['cases']['files'] = configs
    save(output / 'evals/eval.yaml', config)
    save(output / 'skill-up.config.yaml', {})
    protocol = {'pack_sha256': pack['pack_sha256'], 'skill_sha256': file_digest(skill),
                'source_hashes': {p.name: file_digest(p) for p in HERE.glob('*.py')},
                'dependencies': read(HERE / 'dependencies.json'), 'split': split,
                'case_ids': [c['id'] for c in selected], 'max_iterations': 12, 'temperature': 0,
                'max_tokens': 4096, 'model': os.environ.get('EVAL_MODEL', 'glm-5.2'),
                'execution_endpoint': os.environ.get('GLM_BASE_URL'), 'upstream_identity_verified': False,
                'output_protocol': OUTPUT_PROTOCOL, 'request_overrides': request_options()}
    save(output / 'protocol.json', protocol)
    return pack, selected


def evaluate(output, live=False):
    """同一条原生 Skill Up 执行路径，供独立重跑和端到端示例复用。"""
    binary = HERE / '.deps/skill-up'
    version = subprocess.check_output([str(binary), '--version'], text=True)
    if read(HERE / 'dependencies.json')['skill_up_version'] not in version:
        raise RuntimeError('Skill Up 版本不符，请运行 setup.py')
    command = [str(binary), '--config', str(output / 'skill-up.config.yaml')]
    env = os.environ.copy()
    env.update(TMPDIR=str(output), PYTHONDONTWRITEBYTECODE='1')
    # 上游脚本评分器使用 python3；固定为本次解释器目录，避免取到另一环境。
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
    subprocess.run(command + ['validate', str(output / 'evals/eval.yaml')], cwd=HERE, env=env, check=True)
    if live:
        dependency()
        env['LECTURE19_ALLOW_MODEL'] = '1'
        process = subprocess.Popen(command + ['run', str(output / 'evals/eval.yaml'), '--output-dir', str(output / 'results')],
                                   cwd=HERE, env=env, start_new_session=True)
        try:
            returncode = process.wait()
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            save(output / 'native-status.json', {'interrupted': True, 'score': None})
            raise
        save(output / 'native-status.json', {'returncode': returncode, 'model_run_requested': True})
        return returncode
    return 0


def main():
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', type=Path, required=True)
    parser.add_argument('--skill', type=Path, default=HERE / 'skills/oncall-investigation/SKILL.md')
    parser.add_argument('--split', choices=['dev', 'holdout'], default='dev')
    parser.add_argument('--probe', action='store_true', help='真实 Hermes 导入、技能加载和沙箱探测；不请求模型')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.live and args.probe:
        parser.error('--probe 与 --live 不能同时使用')
    if args.live and not all(os.environ.get(k) for k in ('GLM_API_KEY', 'GLM_BASE_URL')):
        parser.error('实跑必须显式设置 GLM_API_KEY、GLM_BASE_URL；不自动读取个人配置')
    pack_path, skill = args.pack.resolve(), args.skill.resolve()
    output = HERE / 'runtime' / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    pack, cases = materialize(pack_path, skill, output, args.split)
    if args.probe:
        case = cases[0]
        result = execute({'workspace': str(pack_path.parent / 'fixtures' / case['id']),
                          'messages': [{'role': 'user', 'content': case['input']['prompt']}]}, skill,
                         [pack_path, HERE / 'grader.py', HERE / 'fixtures/annotations.json'], probe=True)
        save(output / 'probe.json', result)
        print(output / 'probe.json')
        if result['status'] != 'probe_only':
            raise SystemExit('隔离或 Hermes 接线检查失败；见 probe.json 和 actor 日志')
        return
    code = evaluate(output, args.live)
    print(output if args.live else '仅完成原生配置校验，未请求模型：' + str(output))
    raise SystemExit(code)


if __name__ == '__main__':
    main()
