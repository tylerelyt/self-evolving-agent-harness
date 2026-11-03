"""两个真实 Hermes 会话经原生 Client Proxy 汇入一组，再运行一次 workflow。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import socket
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from skillclaw_lab.runtime import connection, evolve_once, hub, install_signal_handlers, load_run, make_config, run_task, save, sha
from skillclaw_lab.observations import snapshot_for, verify_archive, verify_task
from skillclaw_lab.compat import structured_tool_status_compat


def collect(run_dir: Path, alias: str, role: str) -> dict:
    from skillclaw.api_server import SkillClawAPIServer
    client = run_dir / 'clients' / alias
    client.mkdir(parents=True)
    skills = client / 'skills'
    save(client / 'pull.json', hub(run_dir, alias).pull_skills(str(skills), mirror=True))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    proxy = SkillClawAPIServer(make_config(run_dir, alias, skills, port))
    with structured_tool_status_compat() as compatibility:
        save(client / 'compatibility.json', compatibility)
        proxy.start()
        try:
            if not proxy.wait_until_ready(30):
                raise RuntimeError('Client Proxy 没有启动。')
            result = run_task(client / 'task', skills / 'timeout-triage/SKILL.md', 'pool', role,
                              f'http://127.0.0.1:{port}/v1')
        finally:
            # 分类适配覆盖原生归档完成为止，不手工拼装或上传轨迹。
            proxy.stop()
            if proxy._thread:
                proxy._thread.join(timeout=65)
                if proxy._thread.is_alive():
                    raise RuntimeError('代理尚未完成归档，停止，不把未上传会话当作已收集。')
    archive = run_dir / 'shared' / json.loads((run_dir / 'run.json').read_text())['group_id']
    session = archive / 'sessions' / (result['session_id'] + '.json')
    if not session.is_file():
        raise RuntimeError(f'没有找到归档会话：{session}')
    record = json.loads(session.read_text())
    retained = run_dir / 'collected' / session.name
    retained.parent.mkdir(exist_ok=True)
    shutil.copyfile(session, retained)
    messages = json.loads((client / 'task/messages.json').read_text())
    checks = verify_archive(record, messages, result['session_id'], alias)
    return {'alias': alias, 'session_file': str(retained.relative_to(run_dir)),
            'compatibility': compatibility,
            'archive_checks': checks,
            'native_skill_view_verified': result['native_skill_view_verified'],
            'tool_calls': result['calls'], 'answer': result['answer']}


def verify_collection(directory: Path, results: list[dict]) -> dict:
    if len(results) != 2 or {r['alias'] for r in results} != {'instance-a', 'instance-b'}:
        raise RuntimeError('需要两个不同实例的完整会话；不演化部分收集结果。')
    group = load_run(directory)['group_id']
    seed_sha = sha(directory / 'seed/timeout-triage/SKILL.md')
    ids, checks = [], {}
    for item in results:
        alias = item['alias']
        task = directory / 'clients' / alias / 'task'
        result = json.loads((task / 'result.json').read_text())
        messages = json.loads((task / 'messages.json').read_text())
        trace = verify_task(messages, result['calls'], 'pool', result['answer'])
        if (not result.get('completed') or result.get('loaded_skill_sha256') != seed_sha
                or seed_sha not in trace['loaded_skill_hashes']
                or set(result.get('available_tools', [])) != set(snapshot_for('pool'))
                or not all(trace[k] for k in ('receipt_messages_match', 'snapshot_receipts_match',
                                             'readonly_tools_only', 'final_answer_matches'))):
            raise RuntimeError(f'{alias} 的快照回执、旧技能读取或同等工具权限未通过核对。')
        session_id = result['session_id']
        archive = directory / 'collected' / (session_id + '.json')
        queued = directory / 'shared' / group / 'sessions' / archive.name
        if not queued.is_file() or sha(archive) != sha(queued):
            raise RuntimeError('待演化会话与原样副本不一致或已被消费；不会重新上传。')
        checks[alias] = verify_archive(json.loads(archive.read_text()), messages, session_id, alias)
        marker = task.parent / 'compatibility.json'
        checks[alias]['compatibility'] = json.loads(marker.read_text()) if marker.is_file() else None
        ids.append(session_id)
    queued_ids = {p.stem for p in (directory / 'shared' / group / 'sessions').glob('*.json')}
    if len(set(ids)) != 2 or queued_ids != set(ids):
        raise RuntimeError('会话 ID 不独立或共享队列混入了其他会话。')
    save(directory / 'collection-checks.json', checks)
    if not all(check['usable_for_evolution'] for check in checks.values()):
        raise RuntimeError('原生归档的错误摘要与成功回执冲突；保留原记录，停止演化。见 collection-checks.json。')
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, help='新建运行目录，不覆盖已有目录')
    parser.add_argument('--publish-mode', choices=('direct', 'validated'), default='direct')
    parser.add_argument('--collect-only', action='store_true', help='只收集两段真实会话，暂不启动演化')
    parser.add_argument('--evolve-only', action='store_true', help='对 --run-dir 中已有材料运行一次原生演化')
    args = parser.parse_args()
    _, _, model = connection()
    suffix = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    directory = (args.run_dir or HERE / 'output' / suffix).resolve()
    if args.collect_only and args.evolve_only:
        parser.error('--collect-only 和 --evolve-only 不能一起使用')
    if args.evolve_only:
        if not args.run_dir:
            parser.error('--evolve-only 需要 --run-dir')
        results = json.loads((directory / 'collection.json').read_text())
        if (directory / 'evolution-attempt.json').exists() or (directory / 'evolution.json').exists():
            raise RuntimeError('本目录已有演化尝试；不覆盖失败记录或重复消费原会话。')
    else:
        directory.mkdir(parents=True, exist_ok=False)
        save(directory / 'run.json', {'format': 'skillclaw-course-v1', 'group_id': 'oncall-' + suffix,
                                  'publish_mode': args.publish_mode, 'model': model,
                                  'hermes_revision': (HERE.parents[1] / 'scripts/hermes-revision.txt').read_text().strip(),
                                  'skillclaw_revision': (HERE.parents[1] / 'scripts/skillclaw-revision.txt').read_text().strip(),
                                  'scenario': '教学构造的只读现场；模型回答和工具调用由 Hermes 实际生成'})
        shutil.copytree(HERE / 'seed', directory / 'seed')
    old = directory / 'seed/timeout-triage/SKILL.md'
    print(f'本轮目录：{directory}', flush=True)
    if not args.evolve_only:
        save(directory / 'seed-push.json', hub(directory, 'seed').push_skills(str(directory / 'seed')))
        results = []
        for alias, role in (('instance-a', 'source-a'), ('instance-b', 'source-b')):
            results.append(collect(directory, alias, role))
            save(directory / 'collection.json', results)
            print(f'{alias} 已完成，实际工具调用：{len(results[-1]["tool_calls"])}', flush=True)
    verify_collection(directory, results)
    if args.collect_only:
        print('已收集并核对两段会话；未启动演化。兼容层标志见 collection-checks.json。', flush=True)
        return
    save(directory / 'evolution-attempt.json', {'started_at': time.time(), 'source_sessions': 2})
    summary = evolve_once(directory)
    save(directory / 'evolution.json', summary)
    manifest = json.loads((directory / 'run.json').read_text())
    current = directory / 'shared' / manifest['group_id'] / 'skills/timeout-triage/SKILL.md'
    save(directory / 'result.json', {
        'publish_mode': manifest['publish_mode'], 'source_sessions': len(results),
        'seed_sha256': sha(old), 'shared_sha256': sha(current) if current.exists() else None,
        'shared_content_changed': current.exists() and sha(old) != sha(current),
        'collection_checks': json.loads((directory / 'collection-checks.json').read_text()),
        'evolution': summary,
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary.get('had_processing_error'):
        raise RuntimeError('演化处理失败，原始输出见 debug/；本轮不能接续第22讲。')
    print('请检查实际修订；未修改或进入待验证状态都不是发布成功。')


if __name__ == '__main__':
    install_signal_handlers()
    main()
