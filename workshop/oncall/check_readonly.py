#!/usr/bin/env python3
"""无模型回归：经 Hermes 真实工具执行路径验证前台写记忆被拒绝。"""
import json
import os
import tempfile
from pathlib import Path
from common import BASE, record, sanitize_logging

BASE.mkdir(parents=True, exist_ok=True)
root = Path(tempfile.mkdtemp(prefix='readonly-check-', dir=BASE))
os.environ['ONCALL_RUNTIME'] = str(root)
from oncall_server import prepare
key = prepare(Path.home() / '.hermes/.env')
sanitize_logging()
from run_agent import AIAgent
from openai.types.chat import ChatCompletionMessage
from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
agent = AIAgent(model='glm-5.2', provider='glm', api_key=key,
                base_url='https://open.bigmodel.cn/api/paas/v4', enabled_toolsets=['terminal', 'memory'],
                quiet_mode=True, skip_context_files=True)
agent._persist_disabled = True
set_thread_tool_whitelist({'terminal'}, deny_msg_fmt='前台只读，禁止 {tool_name}')
call = {'id': 'readonly-test', 'type': 'function',
        'function': {'name': 'memory', 'arguments': json.dumps({'action': 'add', 'target': 'memory', 'content': '这条不能写入'})}}
messages = [{'role': 'assistant', 'content': None, 'tool_calls': [call]}]
try:
    agent._execute_tool_calls(ChatCompletionMessage(role='assistant', content=None, tool_calls=[call]), messages, 'readonly-check')
finally:
    clear_thread_tool_whitelist()
receipt = json.loads(messages[-1]['content'])
file = root / 'hermes/memories/MEMORY.md'
assert '前台只读' in receipt.get('error', ''), receipt
assert not file.exists() or not file.read_text(), '前台越权写入记忆'
assert agent._memory_nudge_interval == 1 and agent._skill_nudge_interval == 0 and not agent.compression_enabled
agent.close()
record(BASE / 'observations' / root.name / 'check.json', {'model_requests': 0, 'foreground_memory_denied': True, 'receipt': receipt,
                            'disk_not_written': True, 'native_nudge_interval': 1, 'compression_disabled': True})
print('无模型回归成立：原生执行路径拒绝前台 memory，磁盘未写入；复盘配置与关闭压缩已核对。')
