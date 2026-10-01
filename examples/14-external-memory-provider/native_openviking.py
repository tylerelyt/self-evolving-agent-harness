"""固定 Hermes 的原生 OpenViking Provider；不实现替代后端。"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit
import uuid
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HERMES_REVISION = 'aaf9688519cca58dd5f76a589a0911aff269b060'
MEMORY_POLICY = {'self': {'enabled': True}, 'peer': {'enabled': True},
                 'memory_types': ['profile', 'preferences', 'entities', 'events',
                                  'cases', 'trajectories', 'experiences']}
_active = False


def load_native(home):
    source = Path(os.environ.get('HERMES_SRC', ROOT / '.deps/hermes-agent')).resolve()
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != HERMES_REVISION:
        raise ValueError('本例只支持固定 Hermes aaf96885')
    os.environ['HERMES_HOME'] = str(home)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    from agent.memory_manager import MemoryManager
    from plugins.memory.openviking import OpenVikingMemoryProvider
    return MemoryManager, OpenVikingMemoryProvider


def demo_messages():
    """构造输入用于转换与提交观察；不是实际处置或成功经验。"""
    return [
        {'role': 'user', 'content': '教学报表延迟，请核对采集和入库。'},
        {'role': 'assistant', 'content': '先检索线索，再检查当前状态。', 'tool_calls': [
            {'id': 'recall-1', 'type': 'function', 'function': {'name': 'viking_search', 'arguments': '{"query":"报表延迟"}'}},
            {'id': 'check-1', 'type': 'function', 'function': {'name': 'report_status', 'arguments': '{"instance":"A"}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'recall-1', 'content': '旧检索内容，不应作为新工具经历回写。'},
        {'role': 'tool', 'tool_call_id': 'check-1', 'content': '{"constructed":true,"collected":true,"ingestion_paused":true}'},
        {'role': 'assistant', 'content': '构造回执显示入库暂停；尚未处置，不能声称恢复成功。'},
    ]


def require_completed(result):
    if (not result.get('completed') or result.get('failed') or result.get('interrupted')
            or result.get('partial') or not result.get('final_response')):
        raise RuntimeError('前台未正常完成；已保留真实返回，不执行 commit')


class NativeSession:
    """一个进程同一时刻只保留一个身份；不同身份并发使用独立进程。"""
    def __init__(self, *, allow_live=False, account='lesson14', user='report-team', agent='investigator',
                 agent_runtime=False):
        global _active
        if not allow_live:
            raise RuntimeError('服务操作可能调用模型；必须显式 allow_live=True')
        if _active:
            raise RuntimeError('当前进程已有会话；先关闭或使用另一个进程，避免环境身份串用')
        if agent_runtime and not os.environ.get('GLM_API_KEY'):
            raise RuntimeError('请在进程环境中设置 GLM_API_KEY；不会读取个人凭据文件')
        if shutil.disk_usage(HERE).free < 1024 ** 3:
            raise RuntimeError('可用空间小于1GiB，停止新增运行文件')
        for value in (account, user, agent):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                raise ValueError('教学身份只能使用字母、数字、下划线和连字符')
        endpoint = os.environ.get('OPENVIKING_ENDPOINT', 'http://127.0.0.1:1933').rstrip('/')
        url = urlsplit(endpoint)
        if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                or url.username or url.password or url.query or url.fragment or url.path):
            raise ValueError('本例只连接本机 OpenViking 服务根地址')
        self.sid = 'lesson14-' + uuid.uuid4().hex
        self.home = HERE / 'runtime/native/clients' / self.sid
        self.home.mkdir(mode=0o700, parents=True)
        self.home.joinpath('config.yaml').write_text(
            'memory:\n  provider: openviking\n  memory_enabled: false\n'
            '  user_profile_enabled: false\n  nudge_interval: 0\n'
            'skills:\n  creation_nudge_interval: 0\n'
            'agent:\n  environment_probe: false\n')
        os.environ.update(OPENVIKING_ENDPOINT=endpoint, OPENVIKING_ACCOUNT=account,
                          OPENVIKING_USER=user, OPENVIKING_AGENT=agent,
                          OPENVIKING_CLI_CONFIG_FILE=str(self.home / 'absent-ovcli.conf'))
        Manager, Provider = load_native(self.home)
        from plugins.memory.openviking import _VikingClient
        self.client = _VikingClient(endpoint, os.environ.get('OPENVIKING_API_KEY', ''),
                                    account=account, user=user, agent=agent)
        self.manager = Manager(external_prefetch_timeout=4)
        self.provider = Provider()
        self.manager.add_provider(self.provider)
        self.messages = []
        self.write_verified = False
        self.closed = False
        self.ai = None
        # 只禁用便利性的自动启动；initialize/prefetch/sync/commit 仍全部使用原生代码。
        # 本例不允许 Hermes 自动挑选 PATH 中其他环境的 openviking-server。
        self._autostart = patch('plugins.memory.openviking._start_local_openviking_server',
                                return_value=('failed', '请在独立服务环境显式启动 OpenViking'))
        self._autostart.start()
        _active = True
        try:
            self.client.get('/health')
            self.client.post('/api/v1/sessions', {'session_id': self.sid, 'memory_policy': MEMORY_POLICY})
            if agent_runtime:
                from openviking_config import checked_base, OFFICIAL_BASE
                from run_agent import AIAgent
                # AIAgent 按 config.yaml 自行加载原生 Provider 并注入工具。
                self.manager.shutdown_all()
                self.ai = AIAgent(
                    model='glm-5.2', provider='glm', api_mode='chat_completions',
                    api_key=os.environ['GLM_API_KEY'],
                    base_url=checked_base(os.environ.get('GLM_BASE_URL', OFFICIAL_BASE)),
                    session_id=self.sid, enabled_toolsets=['memory'],
                    max_tokens=8000, max_iterations=8, quiet_mode=True,
                    skip_context_files=True, load_soul_identity=False,
                    request_overrides={'extra_body': {'thinking': {'type': 'disabled'}}},
                    ephemeral_system_prompt=(
                        '你是报表值班助手。参考资料和旧经验只能提供线索，不能证明当前故障。'
                        '回答应引用实际取得的资料 URI，区分已有资料、当前回执和待核实事项。'
                        '本实验不提供生产操作工具，不得声称已经恢复故障。'),
                )
                self.manager = self.ai._memory_manager
                if self.manager is None:
                    raise RuntimeError('AIAgent 未加载外部 Provider')
                self.provider = next(p for p in self.manager.providers if isinstance(p, Provider))
            else:
                self.manager.initialize_all(session_id=self.sid, hermes_home=str(self.home), platform='lesson14')
            if self.provider._client is None:
                raise RuntimeError('原生 Provider 初始化后不可用')
        except BaseException:
            # 构造 AIAgent 后半段失败时，对象尚未赋值，但 Provider 可能已注册 atexit。
            from plugins.memory import openviking as module
            candidate = module._last_active_provider
            if candidate is not None and candidate._session_id == self.sid:
                candidate.shutdown()
            self.close()
            raise

    def sync(self, messages):
        self.messages = messages
        user = next(m['content'] for m in reversed(messages) if m['role'] == 'user')
        self.manager.sync_all(user, messages[-1]['content'], session_id=self.sid, messages=messages)
        return self.verify_write(messages)

    def verify_write(self, messages, *, user_content=None, assistant_content=None):
        """只排空与回读，不重复调用 sync_turn；可检查真实 AIAgent 收尾同步。"""
        self.messages = messages
        self.write_verified = False
        if not self.manager.flush_pending(timeout=15):
            raise TimeoutError('Hermes 提交队列未排空')
        # sync_turn 另起 writer；Manager 排空不代表 writer 已成功。
        if not self.provider._drain_writers(self.sid, timeout=65):
            raise TimeoutError('OpenViking writer 尚未结束，不能继续 commit')
        report = self.inspect()
        from plugins.memory.openviking import _derive_openviking_user_text
        user_content = _derive_openviking_user_text(user_content or next(
            (m.get('content', '') for m in reversed(messages) if m.get('role') == 'user'), ''))
        assistant_content = assistant_content or next(
            (m.get('content', '') for m in reversed(messages) if m.get('role') == 'assistant'), '')
        current = [dict(m) for m in self.provider._extract_current_turn_messages(
            messages, user_content, assistant_content)]
        for message in current:
            if message.get('role') == 'user':
                message['content'] = user_content
                break
        expected = self.provider._messages_to_openviking_batch(current, assistant_peer_id=self.provider._agent)
        result = report['context'].get('result', {})
        actual = result.get('messages', []) if isinstance(result, dict) else []
        remaining = iter(actual)
        def contains(actual_message, expected_message):
            if actual_message.get('role') != expected_message['role']:
                return False
            if 'peer_id' in expected_message and actual_message.get('peer_id') != expected_message['peer_id']:
                return False
            actual_parts = actual_message.get('parts', [])
            return len(actual_parts) == len(expected_message['parts']) and all(
                all(a.get(key) == value for key, value in e.items())
                for a, e in zip(actual_parts, expected_message['parts']))
        self.write_verified = bool(expected) and all(
            any(contains(candidate, wanted) for candidate in remaining) for wanted in expected)
        report['write_verified'] = self.write_verified
        report['expected_messages'] = expected
        return report

    def converse(self, query):
        if self.ai is None:
            raise RuntimeError('此会话没有启用真实 AIAgent')
        recall = []
        original_prefetch = self.provider.prefetch
        def observe_prefetch(*args, **kwargs):
            value = original_prefetch(*args, **kwargs)
            recall.append({'query': args[0] if args else kwargs.get('query'), 'context': value})
            return value
        with patch.object(self.provider, 'prefetch', side_effect=observe_prefetch):
            try:
                result = self.ai.run_conversation(user_message=query, conversation_history=[])
            finally:
                self.save('prefetch.json', recall)
        messages = result.get('messages') or []
        self.save('conversation.json', result)
        require_completed(result)
        try:
            report = self.verify_write(messages, user_content=query,
                                       assistant_content=result['final_response'])
        except Exception as exc:
            self.save('write-receipt.json', {'write_verified': False, 'error_type': type(exc).__name__})
            raise
        self.save('write-receipt.json', report)
        self.save('observations.json', {
            'session_id': self.sid, 'peer': self.provider._agent,
            'prefetch_nonempty': any(item['context'] for item in recall),
            'tool_calls': [{'id': call.get('id'), 'name': call.get('function', {}).get('name')}
                           for msg in messages for call in msg.get('tool_calls', [])],
            'write_verified': self.write_verified,
            'note': '是否正确使用召回内容还需对照回答与资料，不能只以调用数判断。',
        })
        return {'result': result, 'write_receipt': report}

    def save(self, filename, value):
        path = self.home / filename
        temp = path.with_suffix(path.suffix + '.tmp')
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        temp.replace(path)

    def inspect(self):
        return {'session': self.client.get(f'/api/v1/sessions/{self.sid}'),
                'context': self.client.get(f'/api/v1/sessions/{self.sid}/context'),
                'note': '须逐项核对消息和tool_id；无异常不等于消息完整或经验已生成。'}

    def end_session(self):
        # 应用 endSession 对应此回调。commit 可能触发多次付费抽取。
        if not self.write_verified:
            raise RuntimeError('尚未逐项回读确认消息，拒绝把写入失败当作可提交会话')
        if self.ai is None:
            self.manager.on_session_end(self.messages)
        else:
            self.ai.commit_memory_session(self.messages)
        report = self.inspect()
        report['commit_accepted'] = self.provider._has_committed_session(self.sid)
        report['note'] = 'commit 被接受仍不代表经验已提炼或可检索，需继续核对服务处理结果。'
        self.save('commit-receipt.json', report)
        return report

    def tool(self, name, **args):
        result = json.loads(self.provider.handle_tool_call(name, args))
        if isinstance(result, dict) and result.get('error'):
            raise RuntimeError('OpenViking 工具失败；请单独核查服务日志')
        return result

    def close(self):
        global _active
        if self.closed:
            return
        self.closed = True
        try:
            # cleanup 不隐式 commit，避免 Notebook 异常退出意外开始付费抽取。
            if self.manager is not None:
                self.manager.shutdown_all()
        finally:
            try:
                if self.ai is not None:
                    self.ai.close()
            finally:
                self._autostart.stop()
                _active = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
