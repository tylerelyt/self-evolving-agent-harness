"""检查原始请求、同编号回执、真实候选内容、SessionDB 和查询条件。"""
import hashlib
import json
import re
from pathlib import PurePosixPath
import yaml
from common import tool_calls, completed

def require(ok,message):
    if not ok:raise RuntimeError(message)

def foreground_read_only(tasks):
    allowed={'payment_query','skills_list','skill_view'}
    for task in tasks:
        for call in task.get('calls',[]):
            if call['name'] in allowed:continue
            receipt=call.get('receipt')
            # 只能认可前台白名单的明确拒绝；一般错误和成功写入都不能通过。
            if not (isinstance(receipt,dict) and not receipt.get('success')
                    and receipt.get('error')==f"前台只读，禁止 {call['name']}。"):return False
    return True

def package_hash(files):
    return hashlib.sha256(json.dumps(files,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def candidate_check(review):
    """从原生调用逐次重建正文，拒绝无来源的新增、覆盖、删除和未完成复盘。"""
    from package_integrity import (
        native_review_complete, package_hash, verify_review_package,
    )
    before, after = review["before"], review["after"]
    state = dict(before)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    mains = [p for p in changed if p.endswith("/SKILL.md") and p in after]
    root = str(PurePosixPath(mains[0]).parent) if len(mains) == 1 else None
    meta = {}
    if root:
        parts = after[root + "/SKILL.md"].split("---", 2)
        meta = yaml.safe_load(parts[1]) if len(parts) == 3 else {}
    name = (meta or {}).get("name")
    references = {p for p in after if root and p.startswith(root + "/references/")}
    valid = bool(name and references)
    valid &= native_review_complete(review) and verify_review_package(review)
    written, readback, ever_read, failures = set(), set(), set(), set()
    trace = []
    for call in review.get("calls", []):
        args = call["args"]
        receipt = call.get("receipt") or {}
        if not isinstance(receipt, dict):
            valid = False
            continue
        if call["name"] not in ("skill_manage", "skill_view"):
            continue
        if args.get("name") not in (name, root):
            if call["name"] == "skill_manage":
                valid = False
            continue
        relative = PurePosixPath(args.get("file_path") or "SKILL.md")
        if relative.is_absolute() or ".." in relative.parts or not root:
            valid = False
            continue
        path = root + "/" + str(relative)
        if call["name"] == "skill_view":
            if receipt.get("success") and receipt.get("content") == state.get(path):
                readback.add(path)
                ever_read.add(path)
            continue
        if not receipt.get("success"):
            failures.add(path)
            trace.append({"call_id": call["tool_call_id"], "path": path, "success": False})
            continue
        action = args.get("action")
        if action in ("create", "edit"):
            content = args.get("content")
            valid &= relative == PurePosixPath("SKILL.md")
            if action == "create":
                valid &= path not in state and receipt.get("path") == root
            else:
                valid &= path in state and path in ever_read
        elif action == "write_file":
            content = args.get("file_content")
            valid &= path not in state or path in ever_read
        elif action == "patch":
            from package_integrity import native_patch
            valid &= path in ever_read
            content, count, strategy, error = native_patch(state.get(path, ""), args)
            valid &= error is None and count > 0
        else:
            valid = False
            continue
        if not isinstance(content, str):
            valid = False
            continue
        state[path] = content
        written.add(path)
        readback.discard(path)
        failures.discard(path)
        trace.append({"call_id": call["tool_call_id"], "path": path, "action": action,
                      "success": True})
    usage = json.loads(review.get("after_package", {}).get(".usage.json", "{}"))
    provenance = usage.get(name, {}).get("created_by") == "agent"
    all_traced = state == after and changed <= written
    complete = bool(valid and provenance and not failures and all_traced
                    and written <= readback and references <= readback)
    return {
        "candidate_complete": complete, "candidate_name": name,
        "candidate_directory": root, "changed_files": sorted(changed),
        "candidate_readback": written <= readback and bool(written),
        "native_review_complete": native_review_complete(review),
        "all_changes_traced": all_traced, "source_preserved": provenance,
        "write_comparisons": [{"path": p, "matches_write_request": state.get(p) == after.get(p)}
                              for p in sorted(written)],
        "write_failures": [{"path": p, "repaired_by_later_success": False}
                           for p in sorted(failures)],
        "read_write_trace": trace,
        "candidate_sha256": package_hash(review.get("after_package", {})),
        "baseline_sha256": package_hash(review.get("before_package", {})),
        "decision": ("create-new" if root and root + "/SKILL.md" not in before
                     else "revise-existing") if changed else "no-update",
    }


def evaluate(tasks,jobs,rows,approval,snapshots,audit,live=True):
    review=jobs[0]['review'];calls=review.get('calls',[]);writes=[c for c in calls if c['name']=='skill_manage']
    verified_candidate = candidate_check(review)
    first_write=next((i for i,c in enumerate(calls) if c['name']=='skill_manage'),len(calls))
    pre=calls[:first_write];name=review.get('candidate_name')
    def queries(t):return [c for c in t.get('calls',[]) if c['name']=='payment_query' and (c.get('receipt') or {}).get('success',True)]
    signature,processing=tasks[2:4]
    def reuse(t):
        cs=t.get('calls',[])
        view=next((i for i,c in enumerate(cs) if c['name']=='skill_view' and c['args'].get('name') in (name,review.get('candidate_directory',name)) and (c.get('receipt') or {}).get('success')),None)
        query=next((i for i,c in enumerate(cs) if c['name']=='payment_query' and c['args'].get('action')=='local'),None)
        return view is not None and query is not None and view<query
    def sequence(t,expected):
        actions=[c['args'].get('action') for c in queries(t) if c['args'].get('action')!='help']
        # 允许同阶段重复核对，首次进入各阶段仍须按条件前进。
        return list(dict.fromkeys(actions))==expected and all(a in expected for a in actions)
    def matching(t,order):
        qs=queries(t);local=next((c['receipt'] for c in qs if c['args'].get('action')=='local'),{})
        channel=next((c['receipt'] for c in qs if c['args'].get('action')=='channel'),{})
        return local.get('order')==order and channel.get('channel_transaction_id')==local.get('channel_transaction_id') and all(local.get(k)==channel.get(k) and local.get(k) is not None for k in ('merchant_id','amount','amount_unit','currency'))
    text=review['after'].get((review.get('candidate_directory') or name or '')+'/SKILL.md','')
    future_text='\n'.join(review['after'].values())
    correction=any(m['role']=='user' and m.get('content')=='受理成功≠最终付款。' for m in rows[0]['messages'])
    sig_answer=signature.get('result',{}).get('final_response','');proc_answer=processing.get('result',{}).get('final_response','')
    checks={
      '官方Session接口建库且新任务空历史':all(s.get('official_session') and s['history_at_start']==0 for s in snapshots),
      '原生轮次达到2并真实登记学习':jobs[0]['native_flags']['memory'] and jobs[0]['counter_before']==1 and jobs[0]['counter_after']==0 and jobs[0]['threshold']==2,
      '前台已返回而学习尚未完成':bool(tasks[1].get('learning_at_return')) and all(j['status']=='queued' for j in tasks[1]['learning_at_return']) and jobs[0]['started_at']>=tasks[1]['ended_at'],
      '纠正查询结果保存在SessionDB':correction and any(m.get('tool_call_id') for m in rows[0]['messages']) and all(completed(t.get('result',{})) for t in tasks),
      '复盘先列库并读现有方法再维护':any(c['name']=='skills_list' and (c.get('receipt') or {}).get('success') for c in pre) and any(c['name']=='skill_view' and c['args'].get('name') in ('log-troubleshooting','connection-pool-check') and (c.get('receipt') or {}).get('success') for c in pre),
      '原生bg-review真实调用维护工具':review.get('thread')=='bg-review' and review.get('model_requests',0)>0 and bool(writes) and any((c.get('receipt') or {}).get('success') for c in writes) and all(f.get('repaired_by_later_success') for f in review.get('write_failures',[])),
      '主文件与references完整逐字回读':verified_candidate['candidate_complete'],
      '候选包含适用条件与只读范围':bool(text) and '单笔' in text and '足额' in text and ('只读' in text or '只调查' in text) and 'references/' in text and any(w in future_text for w in ('不重','不重新','不得重新','不发起','不扣款','不得扣款')) and any(w in future_text for w in ('补发','重发')),
      '确认同一完整候选后采用且注明教学放行':(approval.get('reviewer')=='human' or (approval.get('reviewer')=='teaching-auto' and approval.get('teaching_only') is True)) and approval.get('candidate_sha256')==review.get('candidate_sha256') and approval.get('adopted') and all(t['created_at']>=approval['approved_at'] for t in tasks[2:]),
      '签名新任务自主读取方法':reuse(signature) and name not in signature['message'] and '受理' not in signature['message'],
      '不同原因签名失败来自当前查询':matching(signature,'order-signature') and sequence(signature,['local','channel','platform','notification']) and any(c['receipt'].get('notification_status')=='signature_failed' for c in queries(signature)) and '签名' in sig_answer and not re.search(r'(?:原因是|根因是|因通知)\s*超时',sig_answer),
      '渠道处理中按条件停在待确认':reuse(processing) and matching(processing,'order-processing') and sequence(processing,['local','channel']) and any(w in proc_answer for w in ('处理中','处理','待确认')) and any(w in proc_answer for w in ('未','尚','待')),
      '全部查询只读且业务数据未改':all(x['result'].get('read_only') for x in audit) and all(x['action'] in ('help','local','channel','platform','notification') for x in audit),
      '前台未获维护扣款补发能力':foreground_read_only(tasks),
    }
    for k,v in checks.items():print(('通过' if v else '未通过')+'｜'+k)
    return {'checks':{k:bool(v) for k,v in checks.items()},'passed':all(checks.values()),'path':'native-live' if live else 'recorded-replay'}
