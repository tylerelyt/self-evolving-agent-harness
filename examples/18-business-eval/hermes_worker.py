"""每题每版的真实 Hermes 会话；不导入评分器或标准答案。"""
import contextlib
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

from common import save, tree


READONLY_TOOLS = frozenset({'skills_list', 'skill_view', 'terminal'})


def restrict_tools(agent):
    # toolset 还包含 process、skill_manage；执行钩子的白名单不会删掉模型看到的定义。
    agent.tools = [t for t in agent.tools if t['function']['name'] in READONLY_TOOLS]
    agent.valid_tool_names = {t['function']['name'] for t in agent.tools}
    if agent.valid_tool_names != READONLY_TOOLS:
        raise RuntimeError('只读工具未全部加载，停止评测')


def readonly_command(args):
    allowed = {'order': 3, 'channel': 3, 'local': 3, 'notification': 3, 'mapping': 4, 'mapping-any': 3}
    words = shlex.split(args.get('command', ''))
    if (args.get('background') or args.get('workdir') not in (None, '', os.environ.get('EVAL_WORK'))
        or not (words == ['./payctl', '--help'] or
                len(words) >= 2 and words[0] == './payctl' and words[1] in allowed
                and len(words) == allowed[words[1]]
                and all(w and all(c.isalnum() or c in '-_' for c in w) for w in words[2:]))):
        raise PermissionError('仅允许当前目录的单项 ./payctl 只读查询，无任意路径或 shell 操作')
    return shlex.join(words)


def invoke(terminal_description=None):
    work = Path(os.environ['EVAL_WORK']); home = Path(os.environ['HERMES_HOME'])
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('memory:\n  memory_enabled: true\n  user_profile_enabled: false\n  nudge_interval: 0\n  flush_min_turns: 999\nskills:\n  creation_nudge_interval: 0\ncompression:\n  enabled: false\ntools:\n  tool_search:\n    enabled: false\nterminal:\n  backend: local\n')
    # 方法目录只读，不让原生使用统计写进候选正文；统计留在本次 Home。
    (home / 'skills').symlink_to(work / 'skills', target_is_directory=True)
    (home / 'memories').mkdir()
    memory = os.environ.get('EVAL_PRESET_MEMORY', '这是构造的教学环境。业务状态须依据当前查询，不能依据过去的付款结论。')
    (home / 'memories/MEMORY.md').write_text(memory)
    probes = []
    for path in json.loads(os.environ['EVAL_FORBIDDEN']):
        try:
            target = Path(path)
            list(target.iterdir()) if target.is_dir() else target.read_bytes()
            probes.append({'target': Path(path).name, 'denied': False})
        except PermissionError:
            probes.append({'target': Path(path).name, 'denied': True})
    if not all(p['denied'] for p in probes):
        raise RuntimeError('评分材料可被执行进程读取，拒绝模型运行')
    import hermes_cli.env_loader
    # 独立 Home 不含 dotenv；显式阻止上游向项目或个人路径搜索配置。
    hermes_cli.env_loader.load_hermes_dotenv = lambda **kwargs: []
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from tools.registry import registry
    from tools.terminal_tool import TERMINAL_SCHEMA, _handle_terminal
    terminal_schema = deepcopy(TERMINAL_SCHEMA)
    if terminal_description:
        terminal_schema['description'] = terminal_description
        terminal_schema['parameters']['properties']['command']['description'] = terminal_description
    def readonly_terminal(args, **kwargs):
        try:
            return _handle_terminal({**args, 'command': readonly_command(args), 'background': False}, **kwargs)
        except (ValueError, PermissionError) as exc:
            return json.dumps({'error': str(exc), **({'error_type': 'policy_violation'} if terminal_description else {})}, ensure_ascii=False)

    # 课程业务适配器经原生 registry 接入，Hermes 的工具循环没有重写。
    registry.register(name='terminal', toolset='terminal', schema=terminal_schema,
                      handler=readonly_terminal, check_fn=lambda: True, override=True)
    sid = 'eval-' + uuid.uuid4().hex
    db = SessionDB(home / 'state.db')
    events = []
    def start(call_id, name, args):
        events.append({'stage': 'start', 'tool_call_id': call_id, 'name': name, 'args': args})
    def end(call_id, name, args, receipt):
        events.append({'stage': 'complete', 'tool_call_id': call_id, 'name': name, 'args': args, 'receipt': receipt})
    agent = AIAgent(model=os.environ['EVAL_MODEL'], provider='custom', api_mode='chat_completions',
                    api_key=os.environ['GLM_API_KEY'], base_url=os.environ['EVAL_API'],
                    enabled_toolsets=['skills', 'terminal'], max_iterations=int(os.environ.get('EVAL_MAX_ITERATIONS', 12)),
                    max_tokens=int(os.environ.get('EVAL_MAX_TOKENS', 4096)),
                    request_overrides={'temperature': float(os.environ['EVAL_TEMPERATURE']),
                                       'extra_body': {'thinking': {'type': 'disabled'}}},
                    quiet_mode=True, skip_context_files=True, load_soul_identity=False,
                    save_trajectories=False, session_id=sid, session_db=db,
                    tool_start_callback=start, tool_complete_callback=end)
    restrict_tools(agent)
    requests = []
    client = agent.client
    # HTTP 观察只保存端点和状态，不保存认证头；不调整原生调用过程。
    http_client = getattr(client, '_client', None)
    if http_client is not None and hasattr(http_client, 'event_hooks'):
        http_client.event_hooks['request'].append(lambda r: requests.append({'url': str(r.url), 'stage': 'request'}))
        http_client.event_hooks['response'].append(lambda r: requests.append({'status': r.status_code, 'stage': 'response'}))
    prompt = json.loads((work / 'input.json').read_text())
    before = tree(work / 'skills')
    try:
        from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
        set_thread_tool_whitelist(set(READONLY_TOOLS))
        try:
            result = agent.run_conversation(user_message=prompt['task'], conversation_history=[],
                                            system_message=prompt['system'], task_id=sid)
        finally:
            clear_thread_tool_whitelist()
        rows = db.get_messages(sid, include_inactive=True)
        output = {k: result.get(k) for k in ('final_response', 'messages', 'completed', 'failed', 'error', 'partial', 'interrupted', 'turn_exit_reason')}
        output.update(session_id=sid, pid=os.getpid(), skill_hashes=before, skills_unchanged=before == tree(work / 'skills'),
                      memory_before=memory, memory_after=(home / 'memories/MEMORY.md').read_text(),
                      events=events, db_messages=rows, api_requests=agent._api_call_count, transport=requests,
                      prompt_tokens=agent.session_prompt_tokens, completion_tokens=agent.session_completion_tokens,
                      isolation_probes=probes, tool_names=[t['function']['name'] for t in agent.tools],
                      background_learning=False, model=os.environ['EVAL_MODEL'], base_url=os.environ['EVAL_API'])
    finally:
        agent.close(); db.close()
    reopened = SessionDB(home / 'state.db')
    output['session_reopen_verified'] = rows == reopened.get_messages(sid, include_inactive=True) and bool(rows)
    reopened.close()
    return output


if __name__ == '__main__':
    destination = Path(os.environ['EVAL_WORK']) / 'home/execution.json'
    capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            result = invoke()
    except Exception as exc:
        result = {'infrastructure_error': type(exc).__name__ + ': ' + str(exc)}
    save(destination, result)
    save(destination.with_name('worker.log'), capture.getvalue())
    sys.exit(1 if result.get('infrastructure_error') else 0)
