"""当前第17讲：原生 Curator 整理诊断技能库，保留合并与 no-op 两种结果。"""
import argparse
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import re
import sys
import time

from common import HERE, record, completed, tool_calls
from credentials import SRC, clean_environment, load_credentials, verify_dependency


def tree(skills):
    # 比较技能包的全部字节，包括包内隐藏/二进制资源；根目录的使用统计不算正文改动。
    result = {}
    for path in sorted(skills.rglob('*')):
        relative = path.relative_to(skills)
        if len(relative.parts) < 2 or relative.parts[0] in {'.curator_backups', '.hub'}:
            continue
        if path.is_symlink():
            raise ValueError('教学技能包不得含符号链接')
        if path.is_file():
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def review_outcome(native, returned, loops, before, after):
    if (not native.get('started_at') or native.get('started_at') != returned.get('started_at')
        or native.get('llm_error') or not loops or not all(x['completed'] for x in loops)):
        return 'incomplete'
    return 'no-op' if after == before else 'changed'


def broken_links(skills):
    import re
    missing = []
    for file in skills.rglob('*.md'):
        if any(part.startswith('.') for part in file.relative_to(skills).parts):
            continue
        for link in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', file.read_text()):
            if '://' in link:
                continue
            target = (file.parent / link).resolve()
            if not target.is_relative_to(skills.resolve()) or not target.is_file():
                missing.append({'source': str(file.relative_to(skills)), 'target': link})
    return missing


def extract_answer(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('JSON 含重复键')
            value[key] = item
        return value
    def decode(text):
        value = json.loads(text, object_pairs_hook=unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('非法 JSON 常量')))
        if not isinstance(value, dict):
            raise ValueError('回答必须是 JSON 对象')
        return value
    raw = raw if isinstance(raw, str) else ''
    try:
        return {'answer': decode(raw), 'source': 'json', 'strict_json_format_pass': True, 'outside_text': ''}
    except ValueError:
        pass
    fences = re.findall(r'(?m)^\s*(?:`{3,}|~{3,})[^\n]*$', raw)
    block = re.search(r'(?ms)^[ \t]*```json[ \t]*\n(.*?)^[ \t]*```[ \t]*(?:\n|$)', raw)
    if len(fences) == 2 and block:
        try:
            return {'answer': decode(block.group(1)), 'source': 'single-json-block',
                    'strict_json_format_pass': False,
                    'outside_text': (raw[:block.start()] + raw[block.end():]).strip()}
        except ValueError:
            pass
    return {'answer': None, 'source': 'ambiguous-or-invalid', 'strict_json_format_pass': False,
            'outside_text': raw}


def assess_probe(result, kind):
    from diagnostics import assess
    return assess(result, kind, extract_answer(result.get('final_response', '')))


class CurationLab:
    def __init__(self):
        if any(n in sys.modules for n in ('run_agent', 'agent.curator', 'tools.skill_usage')):
            raise RuntimeError('Hermes 已在当前进程加载；请重启 Notebook 内核后开始新的独立练习')
        verify_dependency()  # 只核对固定依赖；不安装、不更新。
        self.root = HERE / 'runtime' / ('curator-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        self.home = self.root / 'home'; self.home.mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        self.skills = self.home / 'skills'; self.skills.mkdir()
        os.environ.update(HERMES_HOME=str(self.home), TERMINAL_CWD=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        clean_environment()
        sys.path.insert(0, str(SRC))
        import hermes_cli.env_loader
        hermes_cli.env_loader.load_hermes_dotenv = lambda **kwargs: []
        import yaml
        config = {'model': {'provider': 'glm', 'default': 'glm-5.2', 'max_tokens': 2048},
                  'auxiliary': {'curator': {'provider': 'glm', 'model': 'glm-5.2', 'base_url': os.environ['GLM_BASE_URL']}},
                  'curator': {'enabled': True, 'consolidate': True, 'prune_builtins': True,
                              'stale_after_days': 30, 'archive_after_days': 90},
                  'memory': {'memory_enabled': False, 'user_profile_enabled': False, 'nudge_interval': 0},
                  'skills': {'creation_nudge_interval': 0}, 'compression': {'enabled': False}}
        (self.home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True))
        self.loops = []; self.native = None; self.checks = {}; self.probes = {}

    def seed(self):
        from tools.skill_manager_tool import skill_manage
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin
        from tools import skill_usage
        origin = set_current_write_origin('background_review')
        try:
            for directory in sorted((HERE / 'materials').iterdir()):
                result = json.loads(skill_manage(action='create', name=directory.name, content=(directory / 'SKILL.md').read_text()))
                if not result.get('success'):
                    raise RuntimeError(str(result))
                for file in directory.rglob('*'):
                    if file.is_file() and file.name != 'SKILL.md':
                        result = json.loads(skill_manage(action='write_file', name=directory.name,
                            file_path=str(file.relative_to(directory)), file_content=file.read_text()))
                        if not result.get('success'):
                            raise RuntimeError(str(result))
        finally:
            reset_current_write_origin(origin)
        skill_usage.set_pinned('disaster-recovery', True)
        self.clock = datetime.now(timezone.utc)
        with skill_usage._usage_file_lock():
            usage = skill_usage.load_usage()
            for name, days in [('connection-pool-triage', 1), ('connection-leak-investigation', 45),
                               ('disaster-recovery', 120), ('retired-db-debug', 120)]:
                stamp = (self.clock - timedelta(days=days)).isoformat()
                for key in ('created_at', 'last_used_at', 'last_viewed_at', 'last_patched_at', 'last_activity_at'):
                    usage[name][key] = stamp
                usage[name]['state'] = 'active'
            if not skill_usage.save_usage(usage):
                raise RuntimeError('活动记录未保存')
        self.protected = (self.skills / 'disaster-recovery/SKILL.md').read_bytes()
        record(self.root / 'seed.json', {'constructed': True, 'files': tree(self.skills), 'usage': usage})

    def observe_transitions(self, counts):
        from tools import skill_usage
        states = {n: skill_usage.get_record(n)['state'] for n in
                  ('connection-pool-triage', 'connection-leak-investigation', 'disaster-recovery', 'retired-db-debug')}
        self.checks['生命周期与保护'] = states == {'connection-pool-triage': 'active', 'connection-leak-investigation': 'stale',
                                                   'disaster-recovery': 'active', 'retired-db-debug': 'archived'}
        record(self.root / 'transitions.json', {'counts': counts, 'states': states, 'model_requests': 0, 'constructed_clock': True})
        print(states)

    def prepare_review(self):
        load_credentials()
        from agent import curator_backup
        from curator_read_adapter import CuratorReadAdapter
        from run_agent import AIAgent
        self.before = tree(self.skills)
        self.backup = curator_backup.snapshot_skills(reason='connection-diagnostics-before-curation')
        if self.backup is None:
            raise RuntimeError('未取得原生快照，不启动整理')
        self.adapter = CuratorReadAdapter(self.skills); self.adapter.install()
        original = AIAgent.run_conversation
        owner = self
        def observed(agent, *args, **kwargs):
            from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
            allowed = {'skills_list', 'skill_view', 'skill_manage'}
            # 课程调用上限，不改变原生整理提示或预设合并结果。
            agent.max_iterations = 20
            agent.iteration_budget.max_total = 20
            agent.max_tokens = 2048
            agent.tools = [t for t in agent.tools if t['function']['name'] in allowed]
            agent.valid_tool_names = {t['function']['name'] for t in agent.tools}
            set_thread_tool_whitelist(allowed)
            try:
                result = original(agent, *args, **kwargs)
                owner.loops.append({'result': result, 'calls': tool_calls(result.get('messages', [])),
                                    'completed': completed(result), 'model_requests': agent._api_call_count,
                                    'prompt_tokens': agent.session_prompt_tokens,
                                    'completion_tokens': agent.session_completion_tokens})
                return result
            finally:
                clear_thread_tool_whitelist()
        AIAgent.run_conversation = observed
        self._restore_observer = lambda: setattr(AIAgent, 'run_conversation', original)

    def observe_review(self, returned):
        from agent import curator
        self._restore_observer()
        report = Path(curator.load_state()['last_report_path'])
        native = json.loads(((report if report.is_dir() else report.parent) / 'run.json').read_text())
        after = tree(self.skills)
        self.native = {'return': returned, 'report': native, 'before': self.before, 'after': after,
                       'loops': self.loops, 'outcome': review_outcome(native, returned, self.loops, self.before, after),
                       'course_read_bridge': self.adapter.events, 'course_tool_allowlist': ['skills_list', 'skill_view', 'skill_manage'],
                       'course_limits': {'max_iterations': 20, 'max_tokens': 2048}}
        record(self.root / 'curation.json', self.native)
        if self.native['outcome'] == 'incomplete':
            raise RuntimeError('原生整理未完整完成；原始结果已保存，不算成功或 no-op')
        self.checks['应急手册未改'] = (self.skills / 'disaster-recovery/SKILL.md').read_bytes() == self.protected
        self.checks['活动技能引用可达'] = not broken_links(self.skills)
        print('原生整理结果：', self.native['outcome'])
        print(native.get('llm_final', ''))

    def probe(self, kind, phase='after'):
        from run_agent import AIAgent
        from hermes_state import SessionDB
        from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
        from diagnostics import case, prompt, register_query, probe_tools
        kind = {'pool': 'capacity'}.get(kind, kind)
        if phase not in ('before', 'after') or kind not in ('capacity', 'leak'):
            raise ValueError('请选择 before/after 和 capacity/leak')
        if (phase, kind) in self.probes:
            raise ValueError('本项已有运行；请保留原记录，在新运行中重新比较')
        load_credentials()
        register_query(kind)
        session = 'probe-' + phase + '-' + kind
        db = SessionDB(self.home / (session + '.db'))
        agent = None
        try:
            agent = AIAgent(model='glm-5.2', provider='custom', api_mode='chat_completions',
                api_key=os.environ['GLM_API_KEY'], base_url=os.environ['GLM_BASE_URL'],
                enabled_toolsets=['skills', 'curation_diagnostics'], max_iterations=12, max_tokens=2048,
                request_overrides={'temperature': 0}, quiet_mode=True, skip_context_files=True,
                load_soul_identity=False, save_trajectories=False, session_id=session, session_db=db)
            allowed = {'skills_list', 'skill_view', 'diagnostic_query'}
            agent.tools = probe_tools(); agent.valid_tool_names = allowed
            set_thread_tool_whitelist(allowed)
            result = agent.run_conversation(user_message=prompt(kind), conversation_history=[])
            assessment = assess_probe(result, kind)
            entry = {'constructed': True, 'phase': phase, 'input': prompt(kind), 'snapshot': case(kind),
                'session_id': session, 'session_db': str(self.home / (session + '.db')),
                'protocol': {'model': 'glm-5.2', 'base_url': os.environ['GLM_BASE_URL'], 'temperature': 0,
                             'max_iterations': 12, 'max_tokens': 2048},
                'result': result, 'calls': tool_calls(result.get('messages', [])), 'assessment': assessment,
                'passed': assessment['task_pass']}
            self.probes[phase, kind] = entry
            record(self.root / (session + '.json'), entry)
            if phase == 'after':
                self.compare_probe(kind)
            return {'session_id': session, 'assessment': assessment, 'final_response': result.get('final_response')}
        finally:
            clear_thread_tool_whitelist()
            if agent is not None:
                agent.close()
            db.close()

    def compare_probe(self, kind):
        before, after = (self.probes.get((phase, kind)) for phase in ('before', 'after'))
        if before is None or after is None:
            raise ValueError('能力保留对照需要整理前后同题独立会话')
        comparable = (before['protocol'] == after['protocol'] and before['input'] == after['input']
                      and before['assessment']['snapshot_sha256'] == after['assessment']['snapshot_sha256']
                      and before['session_id'] != after['session_id'])
        retained = comparable and before['passed'] is True and after['passed'] is True
        self.checks['整理前后能力保留-' + kind] = retained
        comparison = {'kind': kind, 'comparable': comparable, 'before_session': before['session_id'],
                      'after_session': after['session_id'], 'before_passed': before['passed'],
                      'after_passed': after['passed'], 'retained': retained,
                      'skill_reads': {phase: [call['args'] for call in item.get('calls', []) if call['name'] == 'skill_view']
                                     for phase, item in (('before', before), ('after', after))},
                      'rule_locations': {phase: [entry['resource'] for entry in item['assessment']['rule_receipts']]
                                         for phase, item in (('before', before), ('after', after))},
                      'snapshot_sha256': before['assessment']['snapshot_sha256'],
                      'changed_checks': {name: [before['assessment']['checks'][name], value]
                         for name, value in after['assessment']['checks'].items()
                         if before['assessment']['checks'][name] != value},
                      'claim': '同题能力保留检查；单次运行不证明模型收益或更容易发现入口'}
        record(self.root / ('comparison-' + kind + '.json'), comparison)
        return comparison

    def recover(self):
        from agent import curator_backup
        from tools import skill_usage
        # no-op 时也有先前生命周期归档的技能可恢复，不强迫模型合并。
        archived = skill_usage.list_archived_skill_names()
        restored = None
        if archived:
            restored = {'name': archived[0], 'result': skill_usage.restore_skill(archived[0])}
            if not restored['result'][0]:
                raise RuntimeError(str(restored))
        def linked_resources():
            targets = set()
            for file in self.skills.rglob('*.md'):
                relative = file.relative_to(self.skills)
                if any(part.startswith('.') for part in relative.parts) or relative.parts[0] == 'disaster-recovery':
                    continue
                for link in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', file.read_text()):
                    if '://' in link:
                        continue
                    target = (file.parent / link).resolve()
                    if target.is_relative_to(self.skills.resolve()) and target.is_file() and target != file:
                        targets.add(target)
            return sorted(targets)
        candidates = linked_resources()
        preparation = None
        if not candidates:
            # A valid consolidation can inline all details. Restore the known
            # initial library before the separate missing-resource rehearsal.
            ok, message, _ = curator_backup.rollback(self.backup.name)
            preparation = {'restored_initial_library': ok, 'reason': 'No linked external resources remain', 'message': message}
            if not ok:
                raise RuntimeError('无法准备独立的支持资源恢复演练')
            candidates = linked_resources()
        if not candidates:
            raise RuntimeError('当前技能库没有可用于失联演练的参考文件，不能虚构演练成功')
        damaged = candidates[0]; saved = damaged.read_bytes(); damaged.unlink()
        missing = broken_links(self.skills)
        try:
            ok, message, safety = curator_backup.rollback(self.backup.name)
        except BaseException:
            damaged.parent.mkdir(parents=True, exist_ok=True); damaged.write_bytes(saved)
            raise
        self.checks['发现支持文件失联'] = bool(missing)
        self.checks['原生快照恢复正文'] = bool(ok and tree(self.skills) == self.before)
        record(self.root / 'recovery.json', {'constructed_failure': True, 'single_restore': restored,
            'preparation': preparation, 'damaged_resource': str(damaged.relative_to(self.skills)),
            'broken_links': missing, 'rollback_ok': ok, 'message': message, 'safety_snapshot': str(safety)})

    def finish(self, offline=False):
        result = {'mode': 'offline-native-lifecycle' if offline else 'native-live', 'checks': self.checks,
                  'passed': bool(self.checks) and all(self.checks.values()),
                  'curation_outcome': None if self.native is None else self.native['outcome'],
                  'model_curation_executed': self.native is not None, 'constructed': True}
        record(self.root / 'result.json', result)
        record(self.root / 'REPORT.md', '# 技能库整理记录\n\n' + json.dumps(result, ensure_ascii=False, indent=2)
               + '\n\n[状态转换](transitions.json) · [恢复](recovery.json)\n'
               + ('\n[容量同题对照](comparison-capacity.json) · [泄漏同题对照](comparison-leak.json)\n' if not offline else ''))
        print('记录：', self.root / 'REPORT.md')
        if not result['passed']:
            raise RuntimeError('本次有未通过项，保留记录，不自动修饰模型结果')
        return result


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--offline', action='store_true'); args = parser.parse_args()
    lab = CurationLab(); lab.seed()
    from agent import curator, curator_backup
    lab.observe_transitions(curator.apply_automatic_transitions(now=lab.clock))
    if args.offline:
        lab.before = tree(lab.skills); lab.backup = curator_backup.snapshot_skills(reason='offline-recovery')
        if lab.backup is None:
            raise RuntimeError('未取得原生快照，停止恢复演练')
    else:
        for kind in ('capacity', 'leak'):
            print(lab.probe(kind, phase='before'))
        lab.prepare_review()
        try:
            lab.observe_review(curator.run_curator_review(consolidate=True, synchronous=True))
        finally:
            lab._restore_observer()
        for kind in ('capacity', 'leak'):
            print(lab.probe(kind, phase='after'))
    lab.recover(); lab.finish(offline=args.offline)


if __name__ == '__main__':
    main()
