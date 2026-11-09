"""结课项目：从值班经历到两轮 Skill 采用。"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LESSON20 = REPO / 'examples/20-gepa-offline-optimization'
sys.path.insert(0, str(LESSON20))

from support import LESSON18, digest, read_skill, save, verify_dependency, write_skill
from runner import HermesRunner
from alert_scorer import grade, payload
from reflection import ReflectionLM, connection
from cases import AUDIT, ROUNDS
from local_model import BAD, serve, teaching_candidate


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def decision(before, after):
    """每题可比较、任一旧能力不退、至少一题进步。"""
    if not before or set(before) != set(after) or any(
        type(v) is not bool for v in (*before.values(), *after.values())
    ):
        return {'adopt': False, 'reason': 'unscored_or_incomparable'}
    regressions = [key for key in before if before[key] and not after[key]]
    gains = [key for key in before if not before[key] and after[key]]
    return {'adopt': bool(gains) and not regressions,
            'reason': 'regression' if regressions else 'gain' if gains else 'no_gain',
            'gains': gains, 'regressions': regressions}


def supplied_candidates(values):
    result = {}
    for value in values:
        round_text, separator, filename = value.partition('=')
        if not separator or round_text not in ('1', '2') or not filename:
            raise ValueError('候选格式为 --candidate 1=路径/SKILL.md')
        number = int(round_text)
        if number in result:
            raise ValueError('每轮只能提供一个候选')
        path = Path(filename).expanduser().resolve(strict=True)
        result[number] = {'path': path, 'sha256': digest(path), 'skill': read_skill(path)}
    return result


class Versions:
    def __init__(self, root, header, native_sync=False):
        self.root, self.header = root, header
        self.native_sync = native_sync
        self.pointer = root / 'official.json'

    def stage(self, body, parent, source, evidence=None):
        if not isinstance(body, str) or not body.strip() or len(body) > 16000 or any(
            token in body for token in ('\r', '${HERMES_SKILL_DIR}', '${HERMES_SESSION_ID}')
        ):
            raise ValueError('候选正文超出课程格式约定')
        sha = hashlib.sha256((self.header + body).encode()).hexdigest()
        path = self.skill(sha)
        if not path.exists():
            write_skill(path, self.header, body)
        if digest(path) != sha:
            raise ValueError('方法内容与快照指纹不符')
        record = {'sha256': sha, 'parent': parent, 'source': source, 'experience': evidence}
        save(path.parent / f'origin-{secrets.token_hex(5)}.json', record)
        return sha

    def skill(self, sha):
        if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('无效的 Skill 指纹')
        return self.root / 'versions' / sha / 'SKILL.md'

    def current(self):
        return read(self.pointer)['current']

    def select(self, sha, reason):
        if digest(self.skill(sha)) != sha:
            raise ValueError('无法选用损坏的快照')
        history = read(self.pointer)['history'] if self.pointer.exists() else []
        history.append({'from': history[-1]['to'] if history else None,
                        'to': sha, 'reason': reason})
        save(self.pointer, {'current': sha, 'history': history})

    def sync(self, receiver):
        sha = self.current()
        source = self.skill(sha)
        if digest(source) != sha:
            raise ValueError('共享端内容与正式版本指纹不符')
        receiver_root = self.root / 'receivers' / receiver
        skills = receiver_root / sha
        destination = skills / 'oncall-alert-triage/SKILL.md'
        header, body = read_skill(source)
        if self.native_sync:
            self.sync_skillclaw(sha, header, body, receiver, skills)
        else:
            write_skill(destination, header, body)
        if digest(destination) != sha:
            raise ValueError('接收端同步失败')
        save(receiver_root / 'loaded-next.json', {'sha256': sha, 'native_skillclaw': self.native_sync})
        return sha, destination

    def sync_skillclaw(self, sha, header, body, receiver, skills):
        from skillclaw.skill_hub import SkillHub

        def hub(alias):
            return SkillHub(backend='local', endpoint='', bucket='', access_key_id='',
                secret_access_key='', local_root=str(self.root / 'skillclaw-shared'),
                group_id='capstone', user_alias=alias)

        publisher = self.root / 'publisher' / sha
        write_skill(publisher / 'oncall-alert-triage/SKILL.md', header, body)
        push = hub('publisher').push_skills(str(publisher))
        remote = self.root / 'skillclaw-shared/capstone/skills/oncall-alert-triage/SKILL.md'
        if not remote.is_file() or digest(remote) != sha:
            raise RuntimeError('SkillClaw 共享目录未出现预期的正式版本')
        pull = hub(receiver).pull_skills(str(skills), mirror=True)
        save(skills / 'sync.json', {'sha256': sha, 'push': push, 'pull': pull})


def reflect(mode, reflection, body, experience):
    if mode == 'local':
        return teaching_candidate(body, experience)
    prompt = (
        '根据值班经历提出一个可复用的 Skill 正文。依据实际查询、回执和已核实的反馈，'
        '优先修订已有步骤。排除事故编号、服务名、具体答案与无根据推断。'
        '保留原有只读约束、集群过滤及 JSON 输出协议。'
        '若无足够证据，精确返回 KEEP。其余情况只返回完整 Markdown 正文，勿加 YAML 文件头。\n'
        f'\n当前正文\n{body}\n\n经历\n{json.dumps(experience, ensure_ascii=False)}'
    )
    answer = reflection(prompt).strip()
    if answer == 'KEEP':
        return None
    if answer.startswith(('---', '```')):
        raise ValueError('反思没有返回可直接使用的 Skill 正文')
    return answer + '\n'


class Project:
    def __init__(self, root, mode, model, endpoint, args):
        self.root, self.mode, self.args = root, mode, args
        self.header, body = read_skill(LESSON20 / 'skills/oncall-alert-triage/SKILL.md')
        for item in args.candidates.values():
            if item['skill'][0] != self.header:
                raise ValueError('导入候选需要沿用种子 Skill 的完整 YAML 文件头')
        self.versions = Versions(root / 'registry', self.header, args.skillclaw)
        self.seed = self.versions.stage(body, None, 'course_seed')
        self.versions.select(self.seed, 'initial')
        self.protocol = read(LESSON18 / 'alert_protocol.json')
        self.protocol.update(model=model, max_iterations=args.turns,
                             connection={'execution_endpoint': endpoint})
        save(root / 'dataset.json', {'rounds': ROUNDS, 'audit': AUDIT})
        self.runner = HermesRunner(root, self.header, self.protocol, args.sessions)
        self.reflection = ReflectionLM(model, endpoint, root / 'reflection', 2) if mode == 'live' else None
        save(root / 'protocol.json', {'mode': mode, 'model': model,
            'hermes_revision': verify_dependency(self.runner.source), 'protocol': self.protocol,
            'dataset_sha256': digest(root / 'dataset.json'),
            'scorer_sha256': digest(LESSON18 / 'alert_scorer.py'), 'session_limit': args.sessions,
            'distribution': 'native_skillhub' if args.skillclaw else 'local_files',
            'skillclaw_revision': (REPO / 'scripts/skillclaw-revision.txt').read_text().strip() if args.skillclaw else None})

    def execute(self, task, sha, skill, phase):
        if digest(skill) != sha:
            raise RuntimeError('任务选择的 Skill 与固定指纹不符')
        _, body = read_skill(skill)
        self.runner.phase = phase
        result = self.runner(task=task, skill_body=body)
        verdict = grade(task, result['execution'])
        observed = [call for call in result['tool_calls'] if call['name'] == 'skill_view'
                    and isinstance(call['receipt'], dict) and call['receipt'].get('success')
                    and call['receipt'].get('content') == self.header + body]
        if (verdict.get('score') or {}).get('overall_pass') is True and (
            not observed or not result['execution'].get('session_reopen_verified')
        ):
            raise RuntimeError('缺少 Skill 实际读取或 SessionDB 复开证据')
        proof = {'case_id': task['id'], 'phase': phase, 'chosen_sha256': sha,
                 'loaded_sha256': sha if observed else None,
                 'artifact': result['artifact_dir'], 'snapshot_sha256': result['snapshot_sha256'],
                 'session': result['execution'].get('session_id'),
                 'session_reopen_verified': result['execution'].get('session_reopen_verified'),
                 'grading': verdict}
        save(self.root / result['artifact_dir'] / 'proof.json', proof)
        return proof, result

    def evaluate(self, tasks, sha, phase, skill=None):
        return [self.execute(task, sha, skill or self.versions.skill(sha), phase)[0] for task in tasks]

    def round(self, number, work, gate, execution=None):
        store = self.versions
        receiver = f'oncall-{number}'
        parent, deployed = store.sync(receiver)
        proof, result = execution or self.execute(work, parent, deployed, 'work')
        if proof['case_id'] != work['id'] or proof['chosen_sha256'] != parent:
            raise RuntimeError('复用调查与本轮任务或正式版本不一致')
        if proof['grading']['status'] == 'infrastructure_failure':
            raise RuntimeError('值班会话未取得可复盘结果')
        # 运行与复盘分别读写，材料从落盘的记录取回。
        experience = self.root / 'experience' / f'round-{number}.json'
        save(experience, {'task': work['task'], 'case_id': work['id'], 'receiver': receiver,
             'skill_sha256': proof['loaded_sha256'], 'session': proof['session'],
             'artifact': proof['artifact'], 'final': result['report'],
             'calls': [payload(c) for c in result['tool_calls'] if c['name'] == 'terminal'],
             'feedback': proof['grading']})
        material = read(experience)
        parent_body = read_skill(deployed)[1]
        supplied = self.args.candidates.get(number)
        proposed = supplied['skill'][1] if supplied else reflect(self.mode, self.reflection, parent_body, material)
        record = {'round': number, 'receiver': receiver, 'parent': parent, 'work': proof,
                  'experience_file': str(experience.relative_to(self.root)),
                  'experience_sha256': digest(experience), 'candidate': None,
                  'decision': {'adopt': False, 'reason': 'no_candidate'}}
        if proposed is not None and proposed != parent_body:
            source = 'supplied' if supplied else 'teaching_reflection' if self.mode == 'local' else 'model_reflection'
            candidate = store.stage(proposed, parent, source,
                                    None if supplied else record['experience_file'])
            if supplied:
                record['candidate_input_sha256'] = supplied['sha256']
            before = self.evaluate(gate, parent, 'validation')
            after = self.evaluate(gate, candidate, 'validation')
            verdict = decision(scores(before), scores(after))
            record.update(candidate=candidate, baseline=before, comparison=after, decision=verdict)
            if self.mode == 'local' and not self.args.candidates and number == 1:
                # 单独的反例演练，候选仍走同一评分器和采用规则。
                bad = store.stage(proposed + '\n' + BAD + '\n', parent, 'teaching_negative_control')
                bad_runs = self.evaluate(gate, bad, 'validation')
                record['negative_control'] = {'candidate': bad, 'runs': bad_runs,
                     'decision': decision(scores(before), scores(bad_runs))}
            # 先保存验收记录，正式指针变更时已有可审计的依据。
            save(self.root / f'round-{number}.json', record)
            if verdict['adopt']:
                store.select(candidate, f'round-{number}-gate-passed')
        record['official_after'] = store.current()
        save(self.root / f'round-{number}.json', record)
        return record

    def run(self, first_execution=None):
        rounds = [self.round(number, *round_case, execution=first_execution if number == 1 else None)
                  for number, round_case in enumerate(ROUNDS, 1)]
        shadow, _ = self.execute(ROUNDS[1][0], self.seed, self.versions.skill(self.seed), 'shadow')
        final, receiver = self.versions.sync('audit')
        audit_before = self.evaluate(AUDIT, self.seed, 'holdout')
        audit_after = audit_before if final == self.seed else self.evaluate(AUDIT, final, 'holdout', receiver)
        # 终测只判断安全退步和未评分。独立终测等分允许如实呈现。
        audit_gate = decision(scores(audit_before), scores(audit_after))
        if final != self.seed and audit_gate['reason'] in ('regression', 'unscored_or_incomparable'):
            self.restore('post-adoption-audit-restore')
        rehearsal = None
        if final != self.seed:
            copy = self.root / 'recovery-rehearsal'
            shutil.copytree(self.versions.root, copy)
            restored = Versions(copy, self.header, self.args.skillclaw)
            restored.select(self.seed, 'rehearsal-restore-seed')
            sha, skill = restored.sync('oncall-recovery')
            rehearsal = self.evaluate(ROUNDS[0][1][:1], sha, 'restore', skill)[0]
        report = {'mode': self.mode, 'distribution': 'native_skillhub' if self.args.skillclaw else 'local_files',
                  'rounds': rounds, 'round_two_seed_shadow': shadow,
                  'initial_sha256': self.seed, 'audited_sha256': final,
                  'audit_before': audit_before, 'audit_after': audit_after, 'audit_decision': audit_gate,
                  'recovery_rehearsal': rehearsal, 'official_after_audit': self.versions.current(),
                  'sessions': self.runner.calls, 'reflection_requests': self.reflection.calls if self.reflection else 0}
        save(self.root / 'report.json', report)
        rows = ['# 结课项目执行记录', '', f'模式 `{self.mode}`，独立 Hermes 会话 {self.runner.calls} 次。', '',
                '| 轮次 | 值班实例 | 采用决定 | 正式版本 |', '| --- | --- | --- | --- |']
        rows += [f'| [{r["round"]}](round-{r["round"]}.json) | {r["receiver"]} | {r["decision"]["reason"]} | `{r["official_after"][:12]}` |' for r in rounds]
        rows += ['', f'留出题基线 `{scores(audit_before)}`，待审计版本 `{scores(audit_after)}`。',
                 f'审计决定 `{audit_gate["reason"]}`，最终正式版 `{report["official_after_audit"][:12]}`。',
                 f'回退演练 `{scores([rehearsal]) if rehearsal else "未触发"}`。', '',
                 '[逐题与终测记录](report.json) · [正式版本](registry/official.json) · [学习输入](experience/)']
        save(self.root / 'REPORT.md', '\n'.join(rows) + '\n')
        if self.mode == 'local' and not self.args.candidates:
            self.check_teaching_result(report)
        return report

    def restore(self, reason):
        self.versions.select(self.seed, reason)
        for name in ('oncall-1', 'oncall-2', 'audit'):
            self.versions.sync(name)

    def check_teaching_result(self, report):
        one, two = report['rounds']
        if (not one['decision']['adopt'] or not two['decision']['adopt']
            or one['negative_control']['decision']['reason'] != 'regression'
            or two['work']['loaded_sha256'] != one['candidate']
            or report['audit_decision']['reason'] != 'gain'
            or report['recovery_rehearsal']['loaded_sha256'] != self.seed
            or not all(scores([report['recovery_rehearsal']]).values())):
            raise RuntimeError('本机验收未观察到预期的采用、拒绝、继承、留出或恢复证据')


def scores(proofs):
    return {p['case_id']: (p['grading'].get('score') or {}).get('overall_pass') for p in proofs}


def main(system_runner=None):
    parser = argparse.ArgumentParser(description='巡检与 On-call 系统结课演练' if system_runner else __doc__)
    parser.add_argument('--live', action='store_true', help='使用显式 GLM 连接，实际产生模型调用')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--sessions', type=int, default=24)
    parser.add_argument('--turns', type=int, default=12)
    parser.add_argument('--skillclaw', action='store_true', help='使用课程固定 SkillClaw 的原生本地同步')
    parser.add_argument('--candidate', action='append', default=[], metavar='ROUND=SKILL_MD',
                        help='导入指定轮次的待测 Skill，使用同一验收条件')
    args = parser.parse_args()
    if args.sessions < 1 or not 5 <= args.turns <= 32:
        parser.error('会话数必须为正，单会话轮数须为 5–32')
    try:
        args.candidates = supplied_candidates(args.candidate)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.skillclaw:
        with tempfile.TemporaryDirectory(prefix='capstone-preflight-') as home:
            subprocess.run(['bash', str(REPO / 'scripts/setup_skillclaw.sh'), '--check'], check=True,
                           env={**os.environ, 'HERMES_HOME': home, 'PYTHONDONTWRITEBYTECODE': '1'})
    mode = 'live' if args.live else 'local'
    if args.live:
        model, url = connection()
        endpoint = nullcontext(url)
    else:
        model, endpoint = 'capstone-local-script', serve()
        os.environ['GLM_API_KEY'] = 'capstone-local-only'
    path = (args.out or HERE / 'runtime' / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))).resolve()
    path.mkdir(parents=True, mode=0o700)
    print(f'运行记录 {path}', flush=True)
    project = None
    try:
        with endpoint as url:
            project = Project(path, mode, model, url, args)
            report = system_runner(project) if system_runner else project.run()
        save(path / 'status.json', {'status': 'completed', 'mode': mode, 'sessions': report['sessions']})
    except BaseException as exc:
        error = {'status': 'incomplete', 'error': f'{type(exc).__name__}: {exc}'}
        if project is not None and project.versions.current() != project.seed:
            try:
                project.restore('incomplete-run-restore')
                error['restored_to'] = project.seed
            except (OSError, ValueError) as restore_error:
                error['restore_error'] = str(restore_error)
        save(path / 'status.json', error)
        raise
    print(path / ('SYSTEM_REPORT.md' if system_runner else 'REPORT.md'))


if __name__ == '__main__':
    main()
