"""同一常驻演练入口：实际模型维护与查询；无凭据仅回放已验记录。"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from common import HERE,BASE,runtime,observations,api,record,save,scrub,tool_calls
from package_control import baseline_check,files_at,package_hash
from validation_checks import require,evaluate
import lab


def hashes():
    return {str(p.relative_to(HERE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in HERE.rglob('*') if p.is_file() and p.suffix in ('.py','.sh','.md','.yaml','.ipynb','.json') and not any(x in p.relative_to(HERE).parts for x in ('runtime','.runtime','validation','output','__pycache__'))}


class Story:
    def __init__(self, auto_approve=None):
        self.offline=bool(os.environ.get('LAB_VALIDATION_RUN_ID'));self.started=time.monotonic();self.version=hashes()
        self.tasks=[];self.jobs=[];self.snapshots=[];self.rows=[];self.probes={};self.approval={};self.suffix=secrets.token_hex(3)
        self.auto_approve = (os.environ.get('LAB_AUTO_APPROVE', '1') == '1'
                             if auto_approve is None else auto_approve)
        os.environ['LAB_AUTO_APPROVE'] = '1' if self.auto_approve else '0'
        baseline_check()
        if self.offline:
            import re
            ident=os.environ['LAB_VALIDATION_RUN_ID'];require(bool(re.fullmatch('[a-zA-Z0-9_-]{1,100}',ident)),'回放编号不合法')
            self.fixture=json.loads((HERE/'validation/replay.json').read_text())
            require(self.fixture['source_hashes']==self.version and self.fixture['source_verdict_passed'],
                    '随附回放不可用：不是当前源码的成功运行记录；不改写旧哈希，不自动调用模型')
            self.run=BASE/'runs'/('replay-'+ident);require(not self.run.exists(),'回放编号已使用，请换新编号保留旧记录')
            self.run.mkdir(parents=True);os.environ['SKILL_LAB_RUNTIME']=str(self.run)
            self.out=observations();self.out.mkdir(parents=True);self.work=lab.setup()
            print('无凭据静默回放：不读凭据、不启服务、不调用模型；以下调用与回复来自随附真实运行。')
        else:
            from credentials import load_credentials
            load_credentials();lab.up();self.run=runtime();self.out=observations();self.work=lab.setup()
            self.connection=api('/status');record(self.out/'connection.json',self.connection)
        record(self.out/'source-hashes.json',self.version)

    def task(self,sid,title,message,new=True):
        index=len(self.tasks)
        if self.offline:
            task=self.fixture['tasks'][index]
            if new:self.snapshots.append(self.fixture['snapshots'][len(self.snapshots)])
        else:
            if new:
                official=api('/api/sessions',{'id':sid,'title':title})
                snapshot=api('/sessions/'+sid);snapshot['official_session']=official;self.snapshots.append(snapshot)
            task=lab.wait_task(lab.send(sid,message))
        if self.offline:print('回放报告：\n'+task['result']['final_response'])
        self.tasks.append(task);record(self.out/'tasks'/(task['id']+'.json'),{**task,'replayed':self.offline})
        return task

    def investigate(self):
        print('\n缺口归因机制：查当前规则，区分工具、执行与方法条件')
        sid='gap-'+self.suffix
        self.task(sid,'构造新条件调查','构造订单 order-split 有一笔付款成功，但订单仍显示支付中。请只读调查，核对当前方法是否适用，并说明当前能确认什么、还缺什么。')
        self.task(sid,'构造新条件调查',
            '补充构造观测：①查询工具曾超时，重试后恢复；②另一调查原方法要求查全但执行漏查一笔；③本次原方法只适用单笔足额，当前业务允许分次付款。'
            '请归因并完成当前订单调查。最终只给JSON，reason_classes用timeout/missed_query/split_payment三个键，值分别在tool/execution/method中选择；'
            '另用findings简述实际查询及方法缺口。不要修改文件。',new=False)
        self.jobs=self.fixture['jobs'] if self.offline else lab.wait_review(self.tasks[1]['id'])
        return self.tasks[1]

    def learning(self):
        require(bool(self.jobs),'先完成当前条件调查')
        review=self.jobs[0]['review']
        print('\n先读后改机制：逐文件原生请求与回执')
        for c in review.get('calls',[]):print(c['name'],json.dumps(c['args'],ensure_ascii=False),'→',json.dumps(c.get('receipt'),ensure_ascii=False))
        print('执行器独立原生回读（不计模型调用）：',json.dumps(review.get('verification_reads',[]),ensure_ascii=False))
        print('基线 SHA-256：',review['baseline_sha256']);print('候选 SHA-256：',review['candidate_sha256'])
        print('整目录一致性：',review['structural_checks']);print(review['diff'])
        record(self.out/'candidate.json',review);record(self.out/'patch.diff',review['diff'])
        for phase in ('before','after'):
            for p,s in review[phase].items():record(self.out/'package'/phase/p,s)
        return {'candidate_complete':review['candidate_complete'],'baseline_sha256':review['baseline_sha256'],'candidate_sha256':review['candidate_sha256']}

    def failures(self):
        print('\n部分失败与版本核对机制：真实拒绝，恢复一致后再采用')
        require(self.jobs[0]['review']['candidate_complete'],'本次候选不完整，保留旧版和原始记录，不进入采用演练')
        self.probes=self.fixture['probes'] if self.offline else api('/probes',{'job_id':self.jobs[0]['id']})
        record(self.out/'probes.json',self.probes)
        for c in self.probes['trace']:print(c['name'],json.dumps(c['args'],ensure_ascii=False),'→',json.dumps(c['receipt'],ensure_ascii=False))
        print('部分候选提交：',self.probes['partial_reason']);print('并发候选提交：',self.probes['concurrent_reason'])
        print('恢复后基线：',self.probes['restored_baseline_sha256'],'；同名维护串行：',self.probes['serial_verified'])
        return self.probes

    def confirm(self, digest=None):
        if self.approval:
            return self.approval
        review = self.jobs[0]['review']
        require(review['candidate_complete'], '本次候选不完整，未采用')
        if self.offline:
            self.approval = {**self.fixture['approval'], 'replayed': True}
            print('展示原运行采用回执，不计作本次人工审批。')
        else:
            from approval_control import confirmation
            fields = confirmation(review['candidate_sha256'], digest, self.auto_approve)
            self.approval = api('/approve', {'job_id': self.jobs[0]['id'], **fields})
        record(self.out/'approval.json', self.approval)
        print('采用回执：', self.approval)
        return self.approval

    def regression(self):
        print('\n新旧条件回归机制：四个空历史会话实际查询')
        require(bool(self.approval),'完整候选尚未采用')
        for order in ('order-single','order-split','order-duplicate','order-unknown'):
            self.task(order+'-'+self.suffix,'构造条件验证 '+order,
              '请只读调查构造订单 '+order+' 的付款状态。最终只给JSON：confirmed_amount_fen、remaining_fen是已确认金额与应付差额；'
              'unknown_transactions、processing_transactions、failed_transactions列渠道交易号；notification_checked是布尔值；'
              'unknown_transactions仅列渠道查询不到或状态字段缺失的交易，已查到processing的交易另列processing_transactions；'
              'unknown_details逐项含id、channel_amount_fen、final_status，查不到的字段用null；note简述来源与仍待确认部分。')
        return self.tasks[2:]

    def finish(self):
        from hermes_state import SessionDB
        if self.offline:
            self.rows=self.fixture['rows'];audit=self.fixture['audit'];records=self.fixture['records']
            save(self.work/'audit.jsonl','\n'.join(json.dumps(x,ensure_ascii=False) for x in audit)+'\n')
            db=SessionDB(self.run/'hermes/state.db')
            for row in self.rows:
                db.create_session(row['id'],source='api_server');db.set_session_title(row['id'],row['title'])
                for m in row['messages']:db.append_message(row['id'],m['role'],m.get('content'),tool_calls=m.get('tool_calls'),tool_call_id=m.get('tool_call_id'),tool_name=m.get('tool_name'),timestamp=m.get('timestamp'))
            db.close()
            for p,s in self.jobs[0]['review']['after'].items():save(self.run/'hermes/skills'/p,s)
        else:
            db=SessionDB(self.run/'hermes/state.db')
            for sid in dict.fromkeys(t['session'] for t in self.tasks):
                meta=db.get_session(sid);self.rows.append({'id':sid,'title':meta['title'],'messages':db.get_messages(sid,include_inactive=True)})
            db.close();audit=[json.loads(x) for x in (self.work/'audit.jsonl').read_text().splitlines()];records=json.loads((self.work/'records.json').read_text())
        verdict=evaluate(self.tasks,self.jobs,self.probes,self.approval,self.snapshots,records,self.rows,audit,not self.offline)
        db=SessionDB(self.run/'hermes/state.db')
        try:
            persisted={r['id']:tool_calls(db.get_messages_as_conversation(r['id'],repair_alternation=True)) for r in self.rows}
            checks=verdict['checks']
            checks['SessionDB重开逐编号核对全部回执']=all({c['tool_call_id']:c['receipt'] for c in tool_calls([m for m in r['messages']])}=={c['tool_call_id']:c['receipt'] for c in persisted[r['id']]} for r in self.rows)
        finally:db.close()
        checks['业务记录只读未变']=hashlib.sha256((self.work/'records.json').read_bytes()).hexdigest()==json.loads((self.out/'setup.json').read_text())['business_sha256']
        from package_integrity import snapshot
        checks['正式方法目录与完整候选一致']=package_hash(snapshot(self.run/'hermes/skills'))==self.jobs[0]['review']['candidate_sha256']
        verdict['passed']=all(checks.values())
        for k,v in checks.items():print(('通过' if v else '未通过')+'｜'+k)
        require(self.version==hashes(),'运行期间源文件发生变化，不能沿用这次成功')
        if not self.offline:lab.down()
        else:record(self.out/'shutdown.json',{'service_started':False,'model_requests':0})
        result={**verdict,'source_hashes':self.version,'run':self.run.name,'constructed':True,
          'elapsed_seconds':round(time.monotonic()-self.started,3),'foreground_tasks':0 if self.offline else len(self.tasks),
          'background_reviews':0 if self.offline else len(self.jobs),'model_turns':0 if self.offline else len(self.tasks)+len(self.jobs),
          'model_requests':0 if self.offline else sum(t.get('model_requests',0) for t in self.tasks)+sum(j['review'].get('model_requests',0) for j in self.jobs),
          'tool_calls':sum(len(t.get('calls',[])) for t in self.tasks),'replayed_tasks':len(self.tasks) if self.offline else 0,
          'api_key_prompts':0 if self.offline else int(os.environ.get('LAB_API_KEY_PROMPTS','0')),'background_interval':2,
          'observation_directory':str(self.out.relative_to(HERE)),'model_endpoint':'https://open.bigmodel.cn/api/paas/v4',
          'proxy_environment':False if self.offline else self.connection['proxy_environment']}
        record(self.out/'result.json',result);record(HERE/'validation'/('offline-latest.json' if self.offline else 'live-latest.json'),result)
        record(self.out/'session-rows.json',self.rows)
        for j in self.jobs:record(self.out/'jobs'/(j['id']+'.json'),j)
        if not self.offline:record(self.out/'replay-candidate.json',{'source_run':self.run.name,'source_hashes':self.version,'source_verdict_passed':verdict['passed'],'constructed':True,'tasks':self.tasks,'jobs':self.jobs,'snapshots':self.snapshots,'rows':self.rows,'records':records,'audit':audit,'probes':self.probes,'approval':self.approval})
        record(self.out/'REPORT.md','# 第16讲本次运行\n\n所有业务对象与数值均为构造值。\n\n'+ '\n'.join('- '+k+'：'+('通过' if v else '未通过') for k,v in checks.items())+'\n\n[运行数字](result.json) · [模型任务](tasks/) · [原生后台](jobs/) · [完整候选及读取回执](candidate.json) · [前后差异](patch.diff) · [拒绝及恢复](probes.json) · [采用回执](approval.json) · [SessionDB消息](session-rows.json) · [停止检查](shutdown.json)\n')
        print('运行数字：',json.dumps({k:v for k,v in result.items() if k not in ('checks','case_results','source_hashes')},ensure_ascii=False,indent=2))
        require(verdict['passed'],'验收未全过，保留原始失败，不整理成功回放')
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--auto-approve', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--approve-sha256', help='人工审阅后明确给出的候选哈希')
    args = parser.parse_args()
    try:
        story = Story(auto_approve=args.auto_approve)
        story.investigate()
        story.learning()
        story.failures()
        story.confirm(args.approve_sha256)
        story.regression()
        story.finish()
    except Exception as e:
        print(scrub(str(e)),file=sys.stderr)
        if 'story' in locals():record(story.out/'failure.json',{'error':str(e),'source_hashes':story.version})
        sys.exit(1)
    finally:
        lab.cleanup_owned()


if __name__=='__main__':main()
