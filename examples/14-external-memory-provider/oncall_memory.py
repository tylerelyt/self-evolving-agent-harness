"""Hermes 原生生命周期接入；案例仅从配对现场回执整理，不相信模型自述。"""
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from agent.memory_provider import MemoryProvider
from common import runtime, observations, record, now, tool_calls, scrub
from shared_store import SCOPE, local_schema, rows, pending, snapshot, enqueue, transmit, faults


class OncallMemoryProvider(MemoryProvider):
    @property
    def name(self):
        return 'oncall-memory'

    def is_available(self):
        return (runtime() / 'business.json').exists()

    def get_tool_schemas(self):
        return []

    def event(self, action, **details):
        event = {'id': uuid.uuid4().hex, 'action': action, 'session': self.session,
                 'instance': self.instance, 'scope': self.scope, 'at': now(),
                 'thread': threading.current_thread().name, **details}
        record(observations() / 'provider' / (event['id'] + '.json'), event)
        return event

    def initialize(self, session_id, **kwargs):
        self.session = session_id
        # 身份与范围来自已鉴权的服务会话元数据，不接受用户在召回文本中改身份。
        with sqlite3.connect(runtime() / 'oncall.sqlite3') as db:
            row = db.execute('SELECT data FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise ValueError('不存在已登记的实例身份')
        meta = json.loads(row[0])
        self.instance, self.scope = meta['instance'], meta['scope']
        self.context = kwargs.get('agent_context', 'primary')
        local_schema()
        self.event('initialize', platform=kwargs.get('platform'), hermes_home=kwargs['hermes_home'])
        # 重建实例时恢复持久待传记录；不重新运行模型或业务处置。
        self.retry()

    def system_prompt_block(self):
        return ('共享资料只提供调查线索。实例身份：' + self.instance + '；允许业务范围：' + self.scope +
                '。召回记录中的条件、来源和时间必须保留；当前根因仍须现场工具查询。')

    def prefetch(self, query, *, session_id=''):
        before = rows(self.scope)
        requested = re.search(r'\[scope=([^\]]+)\]', query)
        if requested and requested[1] != self.scope:
            self.event('prefetch', query=query, status='denied', requested_scope=requested[1],
                       before=before, after=before, receipt={'status': 'denied', 'records': []})
            raise PermissionError('召回请求超出当前实例业务范围')
        if faults().get('read_timeout'):
            # Hermes 当前版本原生等待上限8秒。构造慢读取9秒后退出，不无限挂起。
            self.event('prefetch-start', query=query, status='waiting', before=before, delay_seconds=9)
            time.sleep(9)
            self.event('prefetch', query=query, status='timed_out', before=before, after=rows(self.scope),
                       receipt={'status': 'timed_out', 'records': []})
            raise TimeoutError('构造读取超过原生等待预算，本轮无召回')
        found = [r for r in before if r['kind'] in ('manual', 'case') and '报表' in query]
        self.event('prefetch', query=query, status='completed', before=before, after=rows(self.scope),
                   receipt={'status': 'completed', 'records': found})
        return json.dumps(found, ensure_ascii=False) if found else ''

    def sync_turn(self, user_content, assistant_content, *, session_id='', messages=None):
        if self.context != 'primary':
            return
        # 完整历史可能包含多轮，只取最后一个真实user之后的请求与回执。
        messages = messages or []
        start = next((i for i in range(len(messages)-1, -1, -1) if messages[i].get('role') == 'user'), len(messages))
        turn = scrub(messages[start:])
        digest = hashlib.sha256(json.dumps([self.session, start, user_content, turn], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        rid = 'turn-' + digest
        source = {'instance': self.instance, 'session': self.session, 'time': now(), 'message_offset': start}
        raw = {'id': rid, 'kind': 'raw', 'scope': self.scope, 'source': source,
               'user': user_content, 'assistant': assistant_content, 'messages': turn, 'constructed': True}
        evidence = []
        calls = tool_calls(turn)
        by_id = {c['tool_call_id']: c for c in calls}
        counts = {key: sum(c['tool_call_id'] == key for c in calls) for key in by_id}
        seen_calls, seen_receipts = set(), set()
        ordered_calls = []
        for message in turn:
            seen_calls.update(c.get('id') for c in message.get('tool_calls') or [])
            if message.get('role') != 'tool':
                continue
            key = message.get('tool_call_id')
            if key in seen_calls and counts.get(key) == 1 and key not in seen_receipts:
                # 按回执出现次序核查，而非工具请求的排列次序。
                if sum(m.get('role') == 'tool' and m.get('tool_call_id') == key for m in turn) == 1:
                    ordered_calls.append(by_id[key])
                seen_receipts.add(key)
        for call in ordered_calls:
            if call['name'] != 'terminal' or './oncallctl ' not in call['args'].get('command', ''):
                continue
            try:
                payload = json.loads((call.get('receipt') or {}).get('output', ''))
            except (ValueError, TypeError):
                continue
            if (payload.get('scope') == self.scope and payload.get('instance') == self.instance
                    and payload.get('action')):
                evidence.append({'tool_call_id': call['tool_call_id'], 'receipt': payload})
        case = self.extract_case(rid, source, evidence)
        item = {'id': rid, 'scope': self.scope, 'source': source, 'raw': raw, 'case': case}
        existing = next((p for p in pending() if p['id'] == rid), None)
        if existing:
            item = existing['body']  # 重试保持原始时间和正文，编号及内容都稳定。
        enqueue(item)  # 先落盘，之后失败仍能在重启时补传。
        before = snapshot()
        receipt = transmit(item)
        self.event('sync_turn', before=before, after=snapshot(), receipt=receipt, record_id=rid)
        if receipt['error']:
            raise OSError(receipt['error'])  # MemoryManager隔离异常，前台已正常返回。

    def extract_case(self, rid, source, evidence):
        # 只接受同实例、同范围、同版本且按“旧状态-诊断-处置-复查”排列的回执。
        if any(e['receipt'].get('instance') != self.instance or
               e['receipt'].get('scope') != self.scope or
               e['receipt'].get('version') != 'v1' for e in evidence):
            return None
        stale_index = next((i for i, e in enumerate(evidence) if e['receipt']['action'] == 'report'
                            and e['receipt'].get('status') == 'ok' and e['receipt'].get('fresh') is False), None)
        if stale_index is None:
            return None
        action_index = next((i for i in range(stale_index + 1, len(evidence))
                             if evidence[i]['receipt']['action'] in ('resume', 'map-field')
                             and evidence[i]['receipt'].get('status') == 'completed'), None)
        if action_index is None:
            return None
        if not any(e['receipt']['action'] == 'report' and e['receipt'].get('status') == 'ok'
                   and e['receipt'].get('fresh') is True for e in evidence[action_index + 1:]):
            return None
        checks = evidence[stale_index + 1:action_index]
        ingestion = next((e['receipt'] for e in reversed(checks) if e['receipt']['action'] == 'ingestion'
                          and e['receipt'].get('status') == 'ok'), {})
        upstream = next((e['receipt'] for e in reversed(checks) if e['receipt']['action'] == 'upstream'
                         and e['receipt'].get('status') == 'ok'), {})
        actions = [evidence[action_index]]
        if (ingestion.get('paused') is True and actions[0]['receipt']['action'] == 'resume'
                and evidence[stale_index]['receipt'].get('collected') is True):
            condition, cause = '报表未更新；采集成功且入库任务暂停', '入库任务暂停'
        elif (ingestion.get('paused') is False and upstream.get('field') and upstream.get('expected')
              and upstream['field'] != upstream['expected'] and actions[0]['receipt']['action'] == 'map-field'):
            condition, cause = '报表未更新；入库正常且上游字段与映射不符', '上游字段变化'
        else:
            return None
        return {'id': 'case-' + rid, 'kind': 'case', 'scope': self.scope, 'version': 'v1',
                'source': source, 'condition': condition, 'cause': cause, 'actions': actions,
                'outcome': '构造现场复查报表已更新', 'evidence': evidence,
                'raw_id': rid, 'constructed': True}

    def retry(self, force=False):
        result = []
        for p in pending():
            if p['session'] != self.session or (not force and p['status'] not in ('pending', 'failed')):
                continue
            before = snapshot()
            receipt = transmit(p['body'], force=force)
            self.event('retry', before=before, after=snapshot(), receipt=receipt, record_id=p['id'])
            result.append(receipt)
        return result

    def shutdown(self):
        # 写入由 MemoryManager 排队；这里只记录退出，不把关闭当作补传成功。
        self.event('shutdown', pending_count=sum(p['status'] in ('pending', 'failed')
                                                for p in pending() if p['session'] == self.session))
