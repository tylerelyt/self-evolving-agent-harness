#!/usr/bin/env python3
"""文件后端练习：真实 MemoryProvider 接口；--with-model 才调用 GLM。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from native_openviking import HERE, load_native, require_completed

MemoryManager, _ = load_native(HERE / 'runtime/provider/bootstrap')
from agent.memory_provider import MemoryProvider


def save_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class FileBackedMemory(MemoryProvider):
    """保存完整轮次，不把对话自动解释成已验证的成功经验。"""
    def __init__(self, path, identity='A', namespace='shared-lab'):
        self.path = Path(path)
        self.identity, self.namespace = identity, namespace
        self.lock = threading.RLock()
        self.prefetch_calls, self.lookup_calls = [], []
        self.last_error = None
        self.closed = False

    @property
    def name(self):
        return 'filemem'

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.session_id = session_id
        self.home = Path(kwargs['hermes_home'])
        self.home.mkdir(parents=True, exist_ok=True)
        self.outbox = self.home / 'outbox.json'
        if not self.outbox.exists():
            save_json(self.outbox, [])
        # 本地队列先就绪；不连接共享后端，后端故障不能阻止 Agent 启动。

    def pending(self):
        with self.lock:
            return json.loads(self.outbox.read_text())

    def read_rows(self):
        # 每次读取后端，已启动的 B 才能看见 A 后续的写入。
        with (self.path.parent / '.memory.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return json.loads(self.path.read_text()) if self.path.exists() else []

    def store(self, record):
        with (self.path.parent / '.memory.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows = json.loads(self.path.read_text()) if self.path.exists() else []
            existing = next((row for row in rows if row['id'] == record['id']), None)
            if existing is not None and existing != record:
                raise ValueError('同一编号的消息正文不一致')
            if existing is None:
                save_json(self.path, rows + [record])
            if record not in json.loads(self.path.read_text()):
                raise OSError('文件回读不完整')

    def recall_rows(self, query):
        terms = set(re.findall(r'[a-z0-9_-]+|[\u4e00-\u9fff]{2}', query.lower()))
        return [row for row in self.read_rows() if row['namespace'] == self.namespace and
                any(term in (row['user_content'] + row['assistant_content']).lower()
                    for term in terms)][:5]

    def prefetch(self, query, *, session_id=''):
        rows = self.recall_rows(query)
        self.prefetch_calls.append({'query': query, 'record_ids': [row['id'] for row in rows]})
        return json.dumps(rows, ensure_ascii=False) if rows else ''

    def sync_turn(self, user_content, assistant_content, *, session_id='', messages=None):
        record = {'namespace': self.namespace, 'agent_id': self.identity,
                  'session_id': session_id or self.session_id, 'user_content': user_content,
                  'assistant_content': assistant_content, 'messages': messages or []}
        record['id'] = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            rows = self.pending()
            if not any(row['id'] == record['id'] for row in rows):
                save_json(self.outbox, rows + [record])
            self.retry()

    def retry(self):
        with self.lock:
            self.last_error = None
            rows = self.pending()
            while rows:
                try:
                    self.store(rows[0])
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    raise
                # 后端提交且回读一致后才移除本地条目。
                rows = rows[1:]
                save_json(self.outbox, rows)

    def get_tool_schemas(self):
        return [{'name': 'memory_lookup', 'description': '按关键词检索共享记忆，返回来源身份和会话编号。',
                 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                                'required': ['query'], 'additionalProperties': False}}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        if tool_name != 'memory_lookup':
            raise ValueError('未知记忆工具')
        rows = self.recall_rows(args['query'])
        self.lookup_calls.append({'query': args['query'], 'record_ids': [row['id'] for row in rows]})
        return json.dumps({'records': rows}, ensure_ascii=False)

    def shutdown(self):
        # 不删除未发送内容；下次用相同 home 初始化即可补传。
        self.closed = True


class Client:
    def __init__(self, label, root, backend, with_model=False, provider_type=FileBackedMemory):
        self.home = root / label
        self.home.mkdir(mode=0o700)
        self.session_id = f'{label}-{uuid.uuid4().hex}'
        self.provider = provider_type(backend, identity=label)
        self.manager = MemoryManager()
        self.manager.add_provider(self.provider)
        self.agent = None
        self.builtin = self.home / 'memories/MEMORY.md'
        self.builtin.parent.mkdir()
        self.builtin.write_text('教学内置记忆：此文件不由外部 Provider 改写。\n')
        self.builtin_before = self.builtin.read_bytes()
        save_json(self.home / 'config.yaml', {'memory': {'memory_enabled': False,
                  'user_profile_enabled': False, 'nudge_interval': 0},
                  'skills': {'creation_nudge_interval': 0}, 'agent': {'environment_probe': False}})
        try:
            if with_model:
                from openviking_config import checked_base, OFFICIAL_BASE
                if not os.environ.get('GLM_API_KEY'):
                    raise RuntimeError('--with-model 需要进程环境 GLM_API_KEY')
                os.environ['HERMES_HOME'] = str(self.home)
                from run_agent import AIAgent
                self.agent = AIAgent(
                    model='glm-5.2', provider='glm', api_mode='chat_completions',
                    api_key=os.environ['GLM_API_KEY'],
                    base_url=checked_base(os.environ.get('GLM_BASE_URL', OFFICIAL_BASE)),
                    session_id=self.session_id, quiet_mode=True, max_iterations=6, max_tokens=8000,
                    enabled_toolsets=['memory'], skip_memory=True, skip_context_files=True,
                    load_soul_identity=False,
                    request_overrides={'extra_body': {'thinking': {'type': 'disabled'}}})
                if self.agent._memory_manager:
                    self.agent._memory_manager.shutdown_all()
                self.agent._memory_manager = self.manager
                self.agent._memory_nudge_interval = self.agent._skill_nudge_interval = 0
            self.manager.initialize_all(session_id=self.session_id, hermes_home=str(self.home))
            if self.agent:
                from agent.memory_manager import inject_memory_provider_tools
                inject_memory_provider_tools(self.agent)
        except BaseException:
            self.close()
            raise

    def turn(self, message, *, lookup=False):
        if self.agent:
            os.environ['HERMES_HOME'] = str(self.home)
            result = self.agent.run_conversation(user_message=message, conversation_history=[])
            save_json(self.home / f'turn-{uuid.uuid4().hex}.json', result)
            require_completed(result)
        else:
            context = self.manager.prefetch_all(message, session_id=self.session_id)
            if lookup:
                self.manager.handle_tool_call('memory_lookup', {'query': 'API'})
            result = {'offline': True, 'context': context,
                      'final_response': '离线接口输入；不是模型回答。',
                      'messages': [{'role': 'user', 'content': message}]}
            self.manager.sync_all(message, result['final_response'],
                                  session_id=self.session_id, messages=result['messages'])
        if not self.manager.flush_pending(timeout=20):
            raise TimeoutError('提交队列未排空，不能判断后端保存结果')
        assert self.builtin.read_bytes() == self.builtin_before, '外部 Provider 不应改写内置记忆'
        return result

    def close(self):
        try:
            self.manager.shutdown_all()
        finally:
            if self.agent:
                self.agent.close()


def exercise(provider_type=FileBackedMemory, *, with_model=False):
    if shutil.disk_usage(HERE).free < 1024 ** 3:
        raise RuntimeError('可用空间不足1GiB，停止生成运行文件')
    os.umask(0o077)
    root = HERE / 'runtime/provider' / uuid.uuid4().hex
    root.mkdir(parents=True)
    shared = root / 'backend'
    shared.mkdir()
    backend = shared / ('memory.json' if provider_type is FileBackedMemory else 'memory.sqlite3')
    clients = []
    try:
        b = Client('B', root, backend, with_model, provider_type); clients.append(b)
        assert b.manager.prefetch_all('API 文档', session_id=b.session_id) == ''
        a = Client('A', root, backend, with_model, provider_type); clients.append(a)
        a.turn('记住这条教学资料：API 文档地址为 https://docs.example.test/api-v7，只需确认。')
        assert not a.provider.pending(), 'A 写入失败，不能继续声称共享成功'
        seed_ids = {row['id'] for row in a.provider.read_rows()}
        recalled = b.turn('API 文档地址是什么？请根据外部记忆回答；没有资料则明确说不知道。')
        auto = any(seed_ids.intersection(item['record_ids']) for item in b.provider.prefetch_calls)
        assert auto, 'B 的自动召回没有取得 A 的记录'
        if with_model:
            assert 'docs.example.test/api-v7' in recalled['final_response'], 'B 未实际使用地址'
        b.turn('请调用 memory_lookup 工具，query 使用 API，核对文档地址并说明记录来自哪个实例。', lookup=True)
        assert any(seed_ids.intersection(item['record_ids']) for item in b.provider.lookup_calls), '未观察到按需检索'
        before = b.provider.read_rows()
        offline_backend = root / 'backend-unavailable'
        shared.rename(offline_backend)
        try:
            fault_result = b.turn('API 文档更新待核实。当前任务只需回答 7+5 的结果，不必等待记忆后端。')
            if with_model:
                assert re.search(r'\b12\b|十二', fault_result['final_response']), '故障期间未完成当前计算任务'
            queued = b.provider.pending()
            assert queued and b.provider.last_error, '故障轮没有完整保留在本地队列'
            assert queued[-1]['user_content'].endswith('不必等待记忆后端。')
            save_json(root / 'fault-receipt.json', {'outbox': queued,
                      'backend_error': b.provider.last_error, 'frontend': fault_result})
        finally:
            offline_backend.rename(shared)
        assert b.provider.read_rows() == before, '失败轮意外修改共享后端'
        replacement = provider_type(backend, identity='B')
        try:
            replacement.initialize(b.session_id, hermes_home=str(b.home))
            replacement.retry()
            replacement.retry()
            after = replacement.read_rows()
            assert not replacement.pending()
            assert all(after.count(item) == 1 for item in queued), '补传丢失正文或出现重复'
            save_json(root / 'recovery-receipt.json', {'outbox': replacement.pending(),
                      'recovered': [row for row in after if row['id'] in {item['id'] for item in queued}]})
        finally:
            replacement.shutdown()
        summary = {'provider': a.provider.name, 'with_model': with_model,
                   'automatic_recall': auto, 'on_demand_lookup': b.provider.lookup_calls,
                   'distinct_sessions': a.session_id != b.session_id,
                   'b_started_before_a_write': True, 'outbox_recovered_exactly_once': True,
                   'frontend_completed_during_failure': with_model,
                   'offline_only': not with_model, 'business_effect_verified': False,
                   'prefetch': b.provider.prefetch_calls, 'backend': str(backend)}
        save_json(root / 'summary.json', summary)
        print(json.dumps({'records': str(root), **summary}, ensure_ascii=False, indent=2))
        return root
    finally:
        for client in reversed(clients):
            client.close()


def main(provider_type=FileBackedMemory):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-model', action='store_true', help='真实 AIAgent 和 GLM；默认仅离线接口实验')
    args = parser.parse_args()
    exercise(provider_type, with_model=args.with_model)


if __name__ == '__main__':
    main()
