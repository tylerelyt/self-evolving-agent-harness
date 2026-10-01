"""只读构造现场；模型调用真实工具，每次查询都有独立审计回执。"""
import argparse
import fcntl
import json
from common import runtime, now

SCHEMA = {"name": "payment_query", "description": "只读查询构造订单：help 给出字段含义；local 查本地流水；channel 用渠道交易号查最终状态；platform 查平台结果；notification 查商户通知。不支持扣款、改订单或补发。", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["help", "local", "channel", "platform", "notification"]}, "identifier": {"type": "string", "description": "订单号或渠道交易号；help 可省略"}}, "required": ["action"]}}

def operate(action, identifier=''):
    work = runtime() / 'business'
    with (work / 'platform.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads((work / 'records.json').read_text())
        base = {'constructed': True, 'at': now(), 'read_only': True, 'source': 'paylab/' + action}
        if action == 'help':
            result = {**base, 'actions': ['local', 'channel', 'platform', 'notification'],
              'fields': 'local 的 request_result=success 是请求入口的成功返回，其业务含义需确认；channel 的 final_status=paid/processing/failed 是渠道付款状态。按 channel_transaction_id 查渠道；merchant_id、amount、amount_unit、currency 可核对是否同笔。notification 只说明当前通知回执，timeout 不证明商户未处理。所有值均为构造值。',
              'scope': '单笔足额支付，只读调查和报告；不重扣款、不改订单、不补发通知。'}
        elif action not in ('local','channel','platform','notification'):
            result = {**base, 'success': False, 'status': 'denied', 'reason': '只读范围，不支持此操作'}
        else:
            row = next((x for x in data if (x['local']['channel_transaction_id'] if action == 'channel' else x['order']) == identifier), None)
            result = ({**base, 'success': True, 'order': row['order'], **row[action]} if row else {**base, 'success': False, 'status': 'not_found'})
        with (work / 'audit.jsonl').open('a') as out:
            out.write(json.dumps({'at': now(), 'task_id': __import__('os').environ.get('PAYLAB_TASK'), 'action': action, 'identifier': identifier, 'result': result},ensure_ascii=False)+'\n')
        return result

def register():
    from tools.registry import registry
    registry.register(name='payment_query',toolset='oncall_payments',schema=SCHEMA,
                      handler=lambda args, **kw: json.dumps(operate(**args),ensure_ascii=False),check_fn=lambda: True,
                      description='读取当前构造订单，保留查询回执。')

if __name__=='__main__':
    p=argparse.ArgumentParser(description='构造现场，只读。');p.add_argument('action');p.add_argument('identifier',nargs='?',default='')
    a=p.parse_args();print(json.dumps(operate(a.action,a.identifier),ensure_ascii=False,indent=2))
