"""真实 Hermes + 原生 skill_view + 参数驱动的只读教学快照。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skillclaw_lab.runtime import connection, install_signal_handlers, readonly_skill_toolset, save, sha
from skillclaw_lab.observations import query_snapshot, snapshot_for, tool_events, verify_task


def main():
    directory = Path(sys.argv[1]).resolve()
    request = json.loads((directory / 'input.json').read_text())
    home = directory / 'home'
    home.mkdir()
    os.environ.update(HERMES_HOME=str(home), XDG_CACHE_HOME=str(home / 'cache'),
                      PYTHON_DOTENV_DISABLED='1')
    (home / 'config.yaml').write_text('''tools:
  tool_search:
    enabled: false
memory:
  memory_enabled: false
  user_profile_enabled: false
skills:
  creation_nudge_interval: 0
agent:
  environment_probe: false
sessions:
  write_json_snapshots: false
curator:
  enabled: false
''')
    source = Path(request['skill'])
    skill = home / 'skills/timeout-triage/SKILL.md'
    skill.parent.mkdir(parents=True)
    shutil.copyfile(source, skill)
    text = skill.read_text(encoding='utf-8')
    initial_sha = sha(skill)
    snapshot = snapshot_for(request['case'])
    save(directory / 'snapshot.json', snapshot)
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from tools.registry import registry

    calls = []
    lock = threading.Lock()
    # A/B 权限一致；不能用缺少工具伪造旧方法的局限。
    available = list(snapshot)
    skill_toolset = readonly_skill_toolset()

    def handler(name):
        def query(args, **context):
            result = query_snapshot(snapshot, name, args)
            with lock:
                calls.append({'tool': name, 'arguments': args, 'result': result})
                save(directory / 'calls.json', calls)
            return json.dumps(result, ensure_ascii=False)
        return query

    for name in available:
        registry.register(
            name=name, toolset='oncall_snapshot', description='查询指定服务、区域、环境的只读教学快照',
            schema={'name': name, 'description': '读取只读教学快照，不操作生产系统。',
                    'parameters': {'type': 'object', 'properties': {
                        'service': {'type': 'string'}, 'region': {'type': 'string'},
                        'environment': {'type': 'string'}},
                        'required': ['service', 'region', 'environment'], 'additionalProperties': False}},
            handler=handler(name),
        )
    key, _, model = connection()
    session = 'oncall-' + uuid.uuid4().hex
    database = SessionDB(home / 'state.db')
    instructions = request['instructions']
    agent = AIAgent(
        model=model, provider='glm', api_key=key, base_url=request['base_url'],
        api_mode='chat_completions', session_id=session, session_db=database,
        max_iterations=8, max_tokens=2200, enabled_toolsets=[skill_toolset, 'oncall_snapshot'],
        skip_memory=True, skip_context_files=True, save_trajectories=False, quiet_mode=True,
        request_overrides={'temperature': 0, 'extra_body': {'thinking': {'type': 'disabled'}},
                           'extra_headers': {'X-Session-Id': session, 'X-Turn-Type': 'main'}},
        ephemeral_system_prompt=instructions['system'],
    )
    agent.skip_background_review = True
    try:
        result = agent.run_conversation(user_message=instructions['user'])
        messages = agent._session_messages
        save(directory / 'messages.json', messages)
        events = tool_events(messages)
        loaded = False
        for event in events:
            value = event['result']
            if event['tool'] == 'skill_view' and isinstance(value, dict):
                loaded |= bool(event['arguments'].get('name') == 'timeout-triage'
                               and value.get('success') is True
                               and text == value.get('raw_content', value.get('content')))
        trace = verify_task(messages, calls, request['case'], result.get('final_response'))
        complete = bool(result.get('completed') and not result.get('failed')
                        and not result.get('interrupted') and trace['final_answer_matches'])
        save(directory / 'result.json', {
            'session_id': session, 'case': request['case'], 'role': request['role'],
            'task_protocol': instructions['version'],
            'model': model, 'completed': complete,
            'answer': result.get('final_response'), 'calls': calls,
            'loaded_skill_sha256': sha(skill), 'native_skill_view_verified': loaded,
            'skill_unchanged': initial_sha == sha(skill), 'available_tools': available,
            'trace_checks': {k: v for k, v in trace.items() if k != 'events'},
            'api_calls': result.get('api_calls'),
        })
        if not complete or not loaded or initial_sha != sha(skill) or not all(
                trace[k] for k in ('receipt_messages_match', 'snapshot_receipts_match', 'readonly_tools_only')):
            raise RuntimeError('未完成回答或尚未确认原生 skill_view 读取，不能宣称已使用 Skill。')
    finally:
        agent.close()
        database.close()


if __name__ == '__main__':
    install_signal_handlers()
    main()
