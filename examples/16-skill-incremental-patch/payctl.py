"""只读构造查询，返回当前文件中的逐笔数据，不替模型计算整单金额。"""
import json
import os
import fcntl
from common import runtime, now

SCHEMA = {'name':'payment_query','description':'只读构造支付查询：help 解释字段；rules 读取当前业务条件；local 按订单列全部关联流水；channel 用交易号查最终状态；platform/notification 按订单核本地处理和通知。所有金额为构造值。',
 'parameters':{'type':'object','properties':{'action':{'type':'string','enum':['help','rules','local','channel','platform','notification']},'identifier':{'type':'string','description':'订单号或渠道交易号；help/rules可省略'}},'required':['action']}}


def operate(action, identifier=''):
    work = runtime()/'business'
    with (work/'platform.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data = json.loads((work/'records.json').read_text())
        base = {'constructed':True,'at':now(),'read_only':True,'source':'paylab/'+action}
        if action == 'help':
            result = {**base,'success':True,'fields':'request_result=success仅为受理。final_status=paid/processing/failed。local.transactions包含全部关联流水，可能重复；按channel_transaction_id去重。amount单位fen，currency=CNY。渠道not_found表示查询不到，final_status和amount仍未知，不能补成失败或零金额。', 'actions':['rules','local','channel','platform','notification']}
        elif action == 'rules':
            result = {**base,'success':True,'change_id':'constructed-split-rule-16','rule':'已确认业务允许单笔或分次付款。先核订单应付、币种和商户；查询全部关联交易，核对归属，去重后仅汇总有效paid金额。未付足或关键结果未知时，报告已确认金额、差额和待确认交易。确认付足且无关键未知后再查平台及通知；通知timeout不证明商户未处理。只读，不扣款、不补发。'}
        elif action == 'channel':
            matches = [t for order in data for t in order['transactions'] if t['channel_transaction_id'] == identifier]
            row = matches[0] if matches else None
            if row is None or row.get('unavailable'):
                result = {**base,'success':False,'status':'not_found','channel_transaction_id':identifier,'final_status':None,'amount':None,'meaning':'最终状态与渠道金额未知，保留待确认'}
            else:
                result = {**base,'success':True,**{k:v for k,v in row.items() if k not in ('request_result','unavailable')}}
        else:
            order = next((x for x in data if x['order_id'] == identifier),None)
            if order is None:
                result = {**base,'success':False,'status':'not_found'}
            elif action == 'local':
                first = order['transactions'][0]
                result = {**base,'success':True,'order_id':identifier,'payment_mode':order['payment_mode'],'total_due':order['total_due'],'order_status':'paying',
                          **{k:v for k,v in first.items() if k not in ('final_status','unavailable')},
                          'transactions':[{k:v for k,v in t.items() if k not in ('final_status','unavailable')} for t in order['transactions']]}
            elif action == 'platform':
                result = {**base,'success':True,'order_id':identifier,'payment_status':'paid','order_status':'paying'}
            elif action == 'notification':
                result = {**base,'success':True,'order_id':identifier,'notification_status':'signature_failed','merchant_processing':'unknown'}
            else:
                result = {**base,'success':False,'status':'denied','reason':'只读范围，不支持此操作'}
        with (work/'audit.jsonl').open('a') as out:
            out.write(json.dumps({'at':now(),'task_id':os.environ.get('PAYLAB_TASK'),'action':action,'identifier':identifier,'result':result},ensure_ascii=False)+'\n')
        return result


def register():
    from tools.registry import registry
    registry.register(name='payment_query',toolset='oncall_payments',schema=SCHEMA,handler=lambda args,**kw:json.dumps(operate(**args),ensure_ascii=False),check_fn=lambda:True,description='查询构造逐笔状态，保留回执。')
