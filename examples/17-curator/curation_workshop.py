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


def assess_probe(result, expected, explanation_review=None):
    raw = result.get('final_response', '')
    parsed = extract_answer(raw)
    answer = parsed['answer']
    calls = tool_calls(result.get('messages', []))
    references = [c for c in calls if c['name'] == 'skill_view'
                  and isinstance(c['args'], dict) and c['args'].get('file_path')
                  and isinstance(c['receipt'], dict) and c['receipt'].get('success') is True
                  and isinstance(c['receipt'].get('content'), str) and c['receipt']['content'].strip()]
    review = explanation_review or {}
    reviewed = (review.get('response_sha256') == hashlib.sha256(raw.encode()).hexdigest()
                and review.get('consistent') is True and bool(review.get('note')))
    needs_review = answer is None or bool(parsed['outside_text']) and not reviewed
    schema = (isinstance(answer, dict) and set(answer) == {'diagnosis', 'next_check'}
              and isinstance(answer.get('next_check'), str) and bool(answer['next_check'].strip()))
    diagnosis = isinstance(answer, dict) and answer.get('diagnosis') == expected
    content_pass = completed(result) and diagnosis and schema and bool(references)
    return {'completion_pass': completed(result), 'diagnosis_pass': bool(diagnosis),
            'references_pass': bool(references), 'answer_schema_pass': bool(schema),
            'strict_json_format_pass': parsed['strict_json_format_pass'], 'answer_source': parsed['source'],
            'answer': answer, 'explanation_review': review or None, 'needs_review': needs_review,
            'task_pass': None if needs_review else bool(content_pass)}


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
        self.loops = []; self.native = None; self.checks = {}

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

    def probe(self, kind):
        from run_agent import AIAgent
        from hermes_state import SessionDB
        from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
        tasks = {
            'pool': ('prod-eu checkout-api，09:00-09:05。请求量为平时5倍，active接近max，waiting升高；流量回落后active恢复，每个请求连接均归还。', 'capacity-pressure'),
            'leak': ('prod-eu checkout-api，10:00-10:05。请求量已恢复，active仍为max，waiting不降；异常返回的请求持有连接超过300秒，尚未检查释放代码。', 'suspected-leak')}
        evidence, expected = tasks[kind]
        db = SessionDB(self.home / ('probe-' + kind + '.db'))
        agent = None
        try:
            agent = AIAgent(model='glm-5.2', provider='custom', api_mode='chat_completions',
                api_key=os.environ['GLM_API_KEY'], base_url=os.environ['GLM_BASE_URL'],
                enabled_toolsets=['skills'], max_iterations=8, max_tokens=2048,
                request_overrides={'temperature': 0}, quiet_mode=True, skip_context_files=True,
                load_soul_identity=False, save_trajectories=False, session_id='probe-' + kind, session_db=db)
            allowed = {'skills_list', 'skill_view'}
            agent.tools = [t for t in agent.tools if t['function']['name'] in allowed]; agent.valid_tool_names = allowed
            set_thread_tool_whitelist(allowed)
            result = agent.run_conversation(user_message='构造教学现场：' + evidence + '\n自行查找并读取当前技能及相关说明。只读诊断。返回JSON：diagnosis取capacity-pressure、suspected-leak或unknown；next_check用中文说明下一步。', conversation_history=[])
            calls = tool_calls(result.get('messages', []))
            assessment = assess_probe(result, expected)
            self.checks['新入口任务-' + kind] = assessment['task_pass']
            record(self.root / ('probe-' + kind + '.json'), {'constructed': True, 'input': evidence, 'expected': expected,
                'result': result, 'calls': calls, 'assessment': assessment, 'passed': assessment['task_pass']})
            return {'assessment': assessment, 'final_response': result.get('final_response')}
        finally:
            clear_thread_tool_whitelist()
            if agent is not None:
                agent.close()
            db.close()

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
        candidates = [p for p in self.skills.rglob('references/*.md') if '.archive' not in p.parts]
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
            'broken_links': missing, 'rollback_ok': ok, 'message': message, 'safety_snapshot': str(safety)})

    def finish(self, offline=False):
        result = {'mode': 'offline-native-lifecycle' if offline else 'native-live', 'checks': self.checks,
                  'passed': bool(self.checks) and all(self.checks.values()),
                  'curation_outcome': None if self.native is None else self.native['outcome'],
                  'model_curation_executed': self.native is not None, 'constructed': True}
        record(self.root / 'result.json', result)
        record(self.root / 'REPORT.md', '# 技能库整理记录\n\n' + json.dumps(result, ensure_ascii=False, indent=2)
               + '\n\n[状态转换](transitions.json) · [恢复](recovery.json)\n')
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
        lab.prepare_review()
        try:
            lab.observe_review(curator.run_curator_review(consolidate=True, synchronous=True))
        finally:
            lab._restore_observer()
        for kind in ('pool', 'leak'):
            print(lab.probe(kind))
    lab.recover(); lab.finish(offline=args.offline)


if __name__ == '__main__':
    main()
