"""从原生回执、查询轨迹和外部业务状态核验，不把文字推演算作查询。"""
import json
import re
from common import completed, tool_calls
from package_control import NAME,DIRECTORY,REFERENCE,candidate_check,package_hash


def require(ok,message):
    if not ok: raise RuntimeError(message)


def answer(task):
    text=task['result']['final_response'].strip()
    blocks=re.findall(r'```(?:json)?\s*\n(.*?)```',text,re.S)
    if len(blocks)==1:text=blocks[0]
    try:return json.loads(text)
    except ValueError:return {}


def case_check(task,order):
    calls=task.get('calls',[])
    queries=[c for c in calls if c['name']=='payment_query']
    local=[c for c in queries if c['args'].get('action')=='local' and c['args'].get('identifier')==order['order_id']]
    rows={t['channel_transaction_id']:t for t in order['transactions']}
    channel={c['args'].get('identifier'):c.get('receipt') or {} for c in queries if c['args'].get('action')=='channel'}
    confirmed=sum(t['amount'] for t in rows.values() if t['final_status']=='paid' and not t['unavailable'])
    unknown=[n for n,t in rows.items() if t['unavailable']]
    processing=[n for n,t in rows.items() if t['final_status']=='processing']
    failed=[n for n,t in rows.items() if t['final_status']=='failed' and not t['unavailable']]
    should_notify=confirmed>=order['total_due'] and not unknown
    got=answer(task)
    first_query=next((i for i,c in enumerate(calls) if c['name']=='payment_query' and c['args'].get('action')=='local'),len(calls))
    views=[c for c in calls[:first_query] if c['name']=='skill_view' and c['args'].get('name') in (NAME,DIRECTORY) and (c.get('receipt') or {}).get('success')]
    # 前台主文件须在业务查询前加载；引用说明按需读取。
    # 逐文件强制读取是后台修改的要求，不能偷换成每次前台查询都得读完整目录。
    loaded=any(not c['args'].get('file_path') for c in views)
    actual_actions=[c['args'].get('action') for c in queries]
    notification=('notification' in actual_actions and 'platform' in actual_actions) if should_notify else not any(a in ('notification','platform') for a in actual_actions)
    matching=True
    for tid,t in rows.items():
        receipt=channel.get(tid,{})
        if t['unavailable']:
            matching &= receipt.get('status')=='not_found' and receipt.get('final_status') is None and receipt.get('amount') is None
        else:
            matching &= bool(receipt.get('success')) and all(receipt.get(k)==t[k] for k in ('order_id','channel_transaction_id','merchant_id','amount','amount_unit','currency','final_status'))
    correct=(got.get('confirmed_amount_fen')==confirmed and got.get('remaining_fen')==max(0,order['total_due']-confirmed)
             and sorted(got.get('unknown_transactions',[]))==sorted(unknown)
             and sorted(got.get('processing_transactions',[]))==sorted(processing)
             and sorted(got.get('failed_transactions',[]))==sorted(failed)
             and got.get('notification_checked') is should_notify)
    if unknown:
        details=got.get('unknown_details',[])
        correct &= all(any(d.get('id')==tid and d.get('channel_amount_fen') is None and d.get('final_status') is None for d in details) for tid in unknown)
    return {'passed':bool(local and loaded and set(rows)<=set(channel) and matching and notification and correct and completed(task.get('result',{}))),
            'loaded_before_query':loaded,'queried_all_ids':set(rows)<=set(channel),'matching_receipts':bool(matching),
            'conditional_notification':notification,'answer_correct':bool(correct),'answer':got,
            'expected':{'confirmed_amount_fen':confirmed,'remaining_fen':max(0,order['total_due']-confirmed),'unknown':unknown,'notification_checked':should_notify}}


def evaluate(tasks,jobs,probes,approval,snapshots,records,rows,audit,live):
    review=jobs[0]['review'];native=review.get('calls',[])
    candidate=candidate_check(review)
    cases={r['order_id']:case_check(t,r) for t,r in zip(tasks[2:],records)}
    classification=answer(tasks[1]).get('reason_classes',{})
    readonly=all(c['name'] in ('payment_query','skills_list','skill_view') or ((c.get('receipt') or {}).get('success') is False and '前台只读' in str((c.get('receipt') or {}).get('error'))) for t in tasks for c in t.get('calls',[]))
    checks={
      '官方Session接口建立持久会话':all(s.get('official_session') and s['history_at_start']==0 for s in snapshots),
      '旧方法缺口与当前查询留在Session':all(completed(t.get('result',{})) for t in tasks[:2]) and any(c['name']=='skill_view' and c['args'].get('name') in (NAME,DIRECTORY) for c in tasks[0].get('calls',[])) and any(c['name']=='payment_query' and c['args'].get('action')=='rules' for t in tasks[:2] for c in t.get('calls',[])),
      '修改理由区分工具故障执行漏步方法缺条件':classification=={'timeout':'tool','missed_query':'execution','split_payment':'method'},
      '原生阈值触发且前台先返回':jobs[0]['counter_before']==1 and jobs[0]['counter_after']==0 and jobs[0]['threshold']==2 and jobs[0]['started_at']>=tasks[1]['ended_at'] and all(j['status']=='queued' for j in tasks[1].get('learning_at_return',[])),
      '原生后台线程实际读取并patch与write_file':review.get('thread')=='bg-review' and review.get('model_requests',0)>0 and all(any(c['name']=='skill_manage' and c['args'].get('action')==a and (c.get('receipt') or {}).get('success') for c in native) for a in ('patch','write_file')),
      '未读主文件原生真实拒绝':probes['unread_main_rejected'],
      '读主文件能改但未读引用仍拒绝':probes['main_read_patch_succeeded'] and probes['unread_reference_rejected'],
      '实际部分写入被发现且候选不采用':probes['real_partial_write'] and probes['partial_consistency_failed'] and probes['partial_submit_rejected'],
      '并发基线变化真实拒绝提交':probes['concurrent_submit_rejected'] and '基线哈希' in probes['concurrent_reason'],
      '恢复基线与候选后正式目录仍完整':probes['official_unchanged'] and probes['restored_baseline_sha256']==jobs[0]['baseline_sha256'] and probes['candidate_restored_sha256']==review['candidate_sha256'],
      '同名方法维护串行化':jobs[0].get('maintenance_serial') and probes['serial_verified'],
      '逐文件先读后改并回执与最终目录相符':candidate['candidate_complete'] and candidate['baseline_sha256']==jobs[0]['baseline_sha256'],
      '完整同名包通过一致性与哈希核对才采用':approval.get('adopted') and approval['candidate_sha256']==review['candidate_sha256'] and approval['baseline_sha256']==jobs[0]['baseline_sha256'],
      '单笔付足后仍实际查平台与通知':cases['order-single']['passed'],
      '分次付款实际查全且处理中不计成功':cases['order-split']['passed'],
      '重复流水去重且付足后继续查询':cases['order-duplicate']['passed'],
      '未知结果保留未知而非成功失败零金额':cases['order-unknown']['passed'],
      '前台只读且查询回执完整配对':readonly and all(c.get('receipt') is not None for t in tasks for c in t.get('calls',[])) and all(x['result'].get('read_only') for x in audit),
    }
    return {'checks':{k:bool(v) for k,v in checks.items()},'passed':all(checks.values()),'case_results':cases,'path':'native-live' if live else 'recorded-replay'}
