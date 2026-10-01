#!/usr/bin/env python3
"""同一三幕入口：真实常驻运行；无key仅回放本版本已确认的真实记录。"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from common import HERE, BASE, runtime, observations, api, record, save, now, scrub
from validation_checks import require,evaluate,package_hash
import lab

def hashes():
    return {str(p.relative_to(HERE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in HERE.rglob('*') if p.is_file() and p.suffix in ('.py','.sh','.md','.yaml','.ipynb') and not any(x in p.relative_to(HERE).parts for x in ('runtime','.runtime','validation','output','__pycache__'))}

class Story:
    def __init__(self, auto_approve=None):
        # 本入口为教学演练；生产采用时关闭开关，保留人工确认。
        self.auto_approve = (os.environ.get('LAB_AUTO_APPROVE', '1') == '1'
                             if auto_approve is None else auto_approve)
        os.environ['LAB_AUTO_APPROVE'] = '1' if self.auto_approve else '0'
        self.offline=bool(os.environ.get('LAB_VALIDATION_RUN_ID'));self.version=hashes();self.started=time.monotonic()
        self.tasks=[];self.jobs=[];self.rows=[];self.snapshots=[];self.approval={};self.suffix=secrets.token_hex(3)
        self.elapsed_before=0
        if self.offline:
            import re
            ident=os.environ['LAB_VALIDATION_RUN_ID'];require(bool(re.fullmatch('[a-zA-Z0-9_-]{1,100}',ident)),'回放编号不合法')
            require((HERE/'validation/replay.json').exists(),'缺少当前版本成功live生成的 validation/replay.json，不能伪造回放')
            self.fixture=json.loads((HERE/'validation/replay.json').read_text())
            require(self.fixture['source_hashes']==self.version and self.fixture.get('source_verdict_passed'),
                    '随附回放不可用：不是当前源码的成功运行记录；不改写旧哈希，不自动调用模型')
            self.run=BASE/'runs'/('replay-'+ident);require(not self.run.exists(),'回放编号已用，请换新编号')
            self.run.mkdir(parents=True);os.environ['SKILL_LAB_RUNTIME']=str(self.run)
            self.out=observations();self.out.mkdir(parents=True,exist_ok=True);self.work=lab.setup()
            print('无key静默回放：不加载凭据，不启动服务，不调用模型；以下过程来自随附真实运行。')
        elif os.environ.get('LAB_CONTINUE_RUN'):
            self.run=(BASE/'runs'/os.environ['LAB_CONTINUE_RUN']).resolve();require(self.run.is_relative_to(BASE.resolve()),'运行目录越界')
            os.environ['SKILL_LAB_RUNTIME']=str(self.run);self.out=observations();self.work=self.run/'business'
            saved=json.loads((self.out/'story.json').read_text());require(saved['source_hashes']==self.version,'续跑源文件已变化')
            for key in ('tasks','jobs','snapshots','suffix','approval'):setattr(self,key,saved[key])
            self.elapsed_before=saved['elapsed_seconds'];print('接续同一真实运行，保留已经完成的前台与候选记录：',self.run.name)
        else:
            from credentials import load_credentials

            if not os.environ.get('LAB_MODEL_ADAPTER_ACTIVE'): load_credentials()
            lab.up();self.run=runtime();self.out=observations();self.work=lab.setup()
        record(self.out/'source-hashes.json',self.version)

    def checkpoint(self):
        record(self.out/'story.json',{'source_hashes':self.version,'tasks':self.tasks,'jobs':self.jobs,'snapshots':self.snapshots,
              'suffix':self.suffix,'approval':self.approval,'elapsed_seconds':self.elapsed_before+time.monotonic()-self.started})

    def task(self,index,sid,title,message,new=True):
        if len(self.tasks)>index:
            print('此前实际前台报告：\n'+self.tasks[index]['result']['final_response']);return self.tasks[index]
        if self.offline:
            task=self.fixture['tasks'][index];print('回放前台报告：\n'+task['result']['final_response'])
            if new:self.snapshots.append(self.fixture['snapshots'][len(self.snapshots)])
        else:
            if new:
                official=api('/api/sessions',{'id':sid,'title':title})
                snapshot=api('/sessions/'+sid);snapshot['official_session']=official
                self.snapshots.append(snapshot)
            tid=lab.send(sid,message);task=lab.wait_task(tid)
        self.tasks.append(task);record(self.out/'tasks'/(task['id']+'.json'),{**task,'replayed':self.offline});self.checkpoint();return task

    def investigate(self):
        print('\n第一幕｜前台调查与纠正留在 Session')
        sid='investigate-'+self.suffix
        self.task(0,sid,'构造订单首次调查','构造单笔足额订单 order-timeout 一直显示支付中。先查看本地记录，说明“请求成功”目前能确认什么；含义不清的部分请提出需要业务同学解释的问题。')
        # 同事只给业务语义纠正，没有指定方法名称或学习步骤。
        self.task(1,sid,'构造订单纠正','受理成功≠最终付款。',new=False)
        if self.offline:self.jobs=self.fixture['jobs']
        elif not self.jobs:
            self.jobs=lab.wait_review(self.tasks[1]['id'])
        self.checkpoint();return self.tasks[1]

    def learning(self):
        print('\n第二幕｜先查现有技能，再决定新建、修订或不更新')
        require(bool(self.jobs),'请先完成前台调查')
        review=self.jobs[0]['review'];print('复盘决策：',review['decision'])
        for c in review.get('calls',[]):
            print(c['name'],json.dumps(c['args'],ensure_ascii=False),'→',json.dumps(c.get('receipt'),ensure_ascii=False))
        print('完整候选 SHA-256：',review['candidate_sha256'])
        for path in review['changed_files']:print('\n候选文件：'+path+'\n'+review['after'][path])
        record(self.out/'candidate.json',review)
        return {'job_id':self.jobs[0]['id'],'candidate_complete':review['candidate_complete'],'candidate_sha256':review['candidate_sha256'],'decision':review['decision']}

    def confirm(self,digest=None):
        if self.approval:return self.approval
        if self.offline:
            self.approval={**self.fixture['approval'],'replayed':True};print('回放原运行的候选采用回执，不将回放算作新的审批。')
        else:
            review=self.jobs[0]['review'];require(review['candidate_complete'],'本次没有完整可用候选；如实保存，不伪造创建成功')
            from approval_control import confirmation
            fields = confirmation(review['candidate_sha256'], digest, self.auto_approve)
            self.approval = api('/approve', {'job_id': self.jobs[0]['id'], **fields})
        record(self.out/'approval.json',self.approval);self.checkpoint();return self.approval

    def reuse(self):
        print('\n第三幕｜新任务自主复用与不同原因验证')
        require(bool(self.approval),'需先确认采用完整候选')
        for i,order in enumerate(('order-signature','order-processing'),2):
            self.task(i,order+'-'+self.suffix,'构造新订单只读调查 '+order,'构造单笔足额订单 '+order+' 一直显示支付中。请只读调查，报告目前状态与尚未确认的部分。')
        return self.tasks[2:]

    def finish(self):
        if self.offline:
            self.rows=self.fixture['rows'];audit=self.fixture['audit'];records=self.fixture['business_records']
            save(self.work/'audit.jsonl','\n'.join(json.dumps(x,ensure_ascii=False) for x in audit)+'\n')
            from hermes_state import SessionDB
            db=SessionDB(self.run/'hermes/state.db')
            for row in self.rows:
                db.create_session(row['id'],source='api_server');db.set_session_title(row['id'],row['title'])
                for m in row['messages']:db.append_message(row['id'],m['role'],m.get('content'),tool_calls=m.get('tool_calls'),tool_call_id=m.get('tool_call_id'),tool_name=m.get('tool_name'),timestamp=m.get('timestamp'))
            db.close()
            for p,s in self.jobs[0]['review']['after'].items():save(self.run/'hermes/skills'/p,s)
        else:
            from hermes_state import SessionDB
            db=SessionDB(self.run/'hermes/state.db')
            for sid in dict.fromkeys(t['session'] for t in self.tasks):
                meta=db.get_session(sid);self.rows.append({'id':sid,'title':meta['title'],'messages':db.get_messages(sid,include_inactive=True)})
            db.close();audit=[json.loads(x) for x in (self.work/'audit.jsonl').read_text().splitlines()];records=json.loads((self.work/'records.json').read_text())
        verdict=evaluate(self.tasks,self.jobs,self.rows,self.approval,self.snapshots,audit,live=not self.offline)
        # 独立重新打开 SessionDB 核对持久请求与同编号回执，不只看HTTP内存记录。
        from hermes_state import SessionDB
        db=SessionDB(self.run/'hermes/state.db')
        try:
            persisted={sid:db.get_messages_as_conversation(sid,repair_alternation=True) for sid in dict.fromkeys(t['session'] for t in self.tasks)}
        finally:db.close()
        from common import tool_calls
        checks=verdict['checks'];checks['SessionDB重开保留全部调用回执']=all(all(c['receipt'] is not None for c in tool_calls(ms)) and bool(tool_calls(ms)) for ms in persisted.values())
        initial=json.loads((self.out/'setup.json').read_text())
        checks['业务记录与初始构造值一致']=hashlib.sha256((self.work/'records.json').read_bytes()).hexdigest()==initial['business_sha256']
        from package_integrity import snapshot, package_hash
        checks['正式完整包与审批一致'] = package_hash(snapshot(self.run/'hermes/skills')) == self.approval['candidate_sha256']
        verdict['passed']=all(checks.values())
        require(self.version==hashes(),'运行期间源码变化，不能沿用结果')
        if not self.offline:lab.down()
        else:record(self.out/'shutdown.json',{'service_started':False,'model_requests':0})
        result={**verdict,'status':'passed' if verdict['passed'] else 'failed','approval_mode':self.approval.get('reviewer'),
           'source_hashes':self.version,'run':self.run.name,'constructed':True,'elapsed_seconds':round(self.elapsed_before+time.monotonic()-self.started,3),
           'foreground_tasks':0 if self.offline else len(self.tasks),'background_reviews':0 if self.offline else len(self.jobs),
           'model_turns':0 if self.offline else len(self.tasks)+len(self.jobs),
           'model_requests':0 if self.offline else sum(t.get('model_requests',0) for t in self.tasks)+sum(j['review'].get('model_requests',0) for j in self.jobs),
           'tool_calls':sum(len(t.get('calls',[])) for t in self.tasks),'replayed_tasks':len(self.tasks) if self.offline else 0,
           'api_key_prompts':0 if self.offline else int(os.environ.get('LAB_API_KEY_PROMPTS','0')),'background_interval':2,'observation_directory':str(self.out.relative_to(HERE)),
           'candidate_sha256':self.jobs[0]['review']['candidate_sha256']}
        record(self.out/'result.json',result);mode='offline' if self.offline else 'live';record(HERE/'validation'/(mode+'-latest.json'),result)
        for j in self.jobs:record(self.out/'jobs'/(j['id']+'.json'),{**j,'replayed':self.offline})
        record(self.out/'session-rows.json',self.rows)
        if not self.offline:
            record(self.out/'replay-candidate.json',{'source_run':self.run.name,'source_hashes':self.version,'constructed':True,'tasks':self.tasks,'jobs':self.jobs,'rows':self.rows,
                 'snapshots':self.snapshots,'approval':self.approval,'audit':audit,'business_records':records})
        record(self.out/'REPORT.md','# 第15讲本次运行\n\n所有业务对象和数值均为构造值。\n\n'+ '\n'.join('- '+k+'：'+('通过' if v else '未通过') for k,v in checks.items())+'\n\n[运行数字](result.json) · [原始前台任务](tasks/) · [原生复盘](jobs/) · [完整候选](candidate.json) · [人工确认](approval.json) · [SessionDB消息](session-rows.json) · [结束核查](shutdown.json)\n')
        print('运行数字：',json.dumps({k:v for k,v in result.items() if k not in ('checks','source_hashes')},ensure_ascii=False,indent=2))
        require(verdict['passed'],'部分验收未通过；失败和实际记录保留，不整理成功回放')
        return result


def failure_record(story, error):
    mode='offline' if os.environ.get('LAB_VALIDATION_RUN_ID') else 'live'
    # 总验收已经落盘时保留具体失败项与全部模型请求，不能用空检查覆盖。
    if story is not None and (story.out/'result.json').exists():
        result=json.loads((story.out/'result.json').read_text())
        record(HERE/'validation'/(mode+'-latest.json'),{**result,'error':str(error)})
        return
    tasks=[]
    if story is not None:
        tasks=[json.loads(p.read_text()) for p in (story.out/'tasks').glob('*.json')]
    record(HERE/'validation'/(mode+'-latest.json'),{
      'passed':False,'status':'blocked' if ('429' in str(error) or '前台未完成' in str(error) or 'replay.json' in str(error)) else 'failed',
      'path':'recorded-replay' if mode=='offline' else 'native-live','error':str(error),
      'source_hashes':story.version if story else hashes(),'run':story.run.name if story else None,
      'foreground_tasks':len(tasks),'completed_foreground_tasks':sum(t.get('status')=='completed' for t in tasks),
      'background_reviews':len(story.jobs) if story else 0,'model_turns':len(tasks) if mode=='live' else 0,
      'model_requests':sum(t.get('model_requests',0) for t in tasks) if mode=='live' else 0,
      'tool_calls':sum(len(t.get('calls',[])) for t in tasks),'api_key_prompts':0,
      'elapsed_seconds':round(time.monotonic()-story.started,3) if story else 0,
      'observation_directory':str(story.out.relative_to(HERE)) if story else None,
      'checks':{},'constructed':True})


def main():
    p=argparse.ArgumentParser();p.add_argument('--prepare-only',action='store_true');p.add_argument('--resume');p.add_argument('--approve-sha256')
    p.add_argument('--auto-approve',action=argparse.BooleanOptionalAction,default=None,help='教学默认自动采用完整候选；--no-auto-approve 恢复人工确认')
    a=p.parse_args()
    if a.resume:os.environ['LAB_CONTINUE_RUN']=a.resume
    if a.approve_sha256:os.environ['LAB_HUMAN_APPROVAL_SHA256']=a.approve_sha256
    keep_running = False
    try:
        story=Story(auto_approve=a.auto_approve);story.investigate();story.learning()
        if a.prepare_only:
            keep_running = True
            print('候选等待人工确认；续跑编号：',story.run.name);return
        story.confirm();story.reuse();story.finish()
    except Exception as e:
        failure_record(locals().get('story'),e)
        print(scrub('演练失败：'+str(e)),file=sys.stderr)
        if 'story' in locals():record(story.out/'failure.json',{'error':str(e),'source_hashes':story.version})
        sys.exit(1)
    finally:
        if not keep_running:
            lab.cleanup_owned()
if __name__=='__main__':main()
