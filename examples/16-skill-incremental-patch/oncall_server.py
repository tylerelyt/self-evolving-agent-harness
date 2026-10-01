#!/usr/bin/env python3
"""官方 API Session 接口 + 课程持久前台/学习队列；每个 Session 真实 AIAgent。"""
import argparse
import asyncio
import copy
import hashlib
import inspect
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from common import HERE, runtime, observations, now, record, scrub, save, tool_calls, completed, sanitize_logging

def prepare():
    home=runtime()/'hermes';home.mkdir(exist_ok=True)
    (home/'config.yaml').write_text((HERE/'config/hermes.yaml').read_text())
    (home/'.no-bundled-skills').touch()
    os.environ.update(HERMES_HOME=str(home),TERMINAL_CWD=str(HERE))
    os.environ.pop('BIGMODEL_API_KEY',None)
    if not os.environ.get('GLM_API_KEY'): raise ValueError('缺少 GLM_API_KEY')
    from payctl import register
    register();sanitize_logging()
    return home

class Service:
    def __init__(self,home):
        from run_agent import AIAgent
        from hermes_state import SessionDB
        self.home=home;self.model=os.environ.get('GLM_MODEL','glm-5.2');self.base_url=os.environ.get('GLM_BASE_URL','https://open.bigmodel.cn/api/paas/v4')
        signature=list(inspect.signature(AIAgent._spawn_background_review).parameters)
        if signature!=['self','messages_snapshot','review_memory','review_skills','focus']: raise RuntimeError('私有复盘接口签名变化，请核查兼容性')
        self.db=sqlite3.connect(runtime()/'oncall.sqlite3',check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id))')
        self.lock=threading.RLock();self.tasks={};self.jobs={};self.agents={};self.histories={};self.sessions={}
        self.session_db=SessionDB(home/'state.db')
        self.queue=queue.Queue();self.learning_queue=queue.Queue();self.accepting=True
        self.stop_event=threading.Event()
        for kind,ident,data in self.db.execute('SELECT kind,id,data FROM records'):
            item=json.loads(data)
            if item.get('status') in ('queued','running'):
                item.update(status='interrupted',error='服务重启，执行结果待核查；不自动重复写入或重跑')
            (self.tasks if kind=='tasks' else self.jobs)[ident]=item
        for kind,items in (('tasks',self.tasks),('jobs',self.jobs)):
            for item in items.values(): self.persist(kind,item)
        service=self
        class OncallAgent(AIAgent):
            def _spawn_background_review(agent,messages_snapshot,review_memory=False,review_skills=False,focus=None):
                # 原生 turn_finalizer 满足阈值才到这里；不人工增加或重置计数。
                service.enqueue(agent,messages_snapshot,review_memory,review_skills,focus)
        self.Agent=OncallAgent
        self.worker=threading.Thread(target=self.work,name='oncall-queue',daemon=True);self.worker.start()
        self.learner=threading.Thread(target=self.learn,name='learning-queue',daemon=True);self.learner.start()
        # 第15讲已确认的完整包及原生来源记录，原样复制作为同阶段起点。
        from package_control import baseline_check
        baseline_check()
        if not list((home/'skills').rglob('SKILL.md')):
            shutil.copytree(HERE/'materials/baseline',home/'skills',dirs_exist_ok=True)
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)

    def persist(self,kind,item):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',(kind,item['id'],json.dumps(scrub(item),ensure_ascii=False)));self.db.commit()
            record(observations()/kind/(item['id']+'.json'),item)

    def agent(self,sid):
        if sid not in self.agents:
            if not self.session_db.get_session(sid): raise ValueError('请先 POST /api/sessions 建立官方 Session')
            history=self.session_db.get_messages_as_conversation(sid,repair_alternation=True)
            agent=self.Agent(model=self.model,provider='glm',api_key=os.environ['GLM_API_KEY'],base_url=self.base_url,
               session_id=sid,session_db=self.session_db,enabled_toolsets=['memory','skills','oncall_payments'],max_iterations=16,quiet_mode=True,skip_context_files=True)
            if agent._memory_nudge_interval!=2 or agent._skill_nudge_interval!=0: raise RuntimeError('教学阈值配置未生效')
            self.agents[sid]=agent;self.histories[sid]=history
            self.sessions[sid]={'id':sid,'history_at_start':len(history),'created_at':now()}
        return self.agents[sid]

    def submit(self,sid,message):
        with self.lock:
            if not self.accepting: raise ValueError('服务正在关闭')
            self.agent(sid)
            item={'id':'task-'+uuid.uuid4().hex[:12],'session':sid,'message':message,'created_at':now(),'status':'queued'}
            self.tasks[item['id']]=item;self.persist('tasks',item);self.queue.put(item['id']);return item

    def enqueue(self,agent,snapshot,memory,skills,focus):
        item={'id':'learn-'+uuid.uuid4().hex[:12],'task_id':agent._oncall_task,'parent_session':agent.session_id,'snapshot':copy.deepcopy(snapshot),
          'cached_prompt':agent._cached_system_prompt,'model':self.model,'base_url':self.base_url,'native_flags':{'memory':memory,'skills':skills,'focus':focus},
          'counter_before':self.tasks[agent._oncall_task]['counter_before'],'counter_after':agent._turns_since_memory,
          'threshold':agent._memory_nudge_interval,'created_at':now(),'status':'queued'}
        from package_integrity import snapshot as package_snapshot, content_files, package_hash
        baseline = package_snapshot(self.home/'skills')
        item['baseline_files'] = content_files(baseline)
        item['baseline_sha256'] = package_hash(baseline)
        self.jobs[item['id']]=item;self.persist('jobs',item)
        save(runtime()/'learning'/item['id']/'job.json',item)
        self.learning_queue.put(item['id'])

    def work(self):
        while True:
            tid=self.queue.get()
            if tid is None: self.queue.task_done();break
            task=self.tasks[tid];agent=self.agent(task['session']);agent._oncall_task=tid
            task.update(status='running',started_at=now(),counter_before=agent._turns_since_memory)
            self.persist('tasks',task);inherited={c['tool_call_id'] for c in tool_calls(self.histories[task['session']])}
            from hermes_cli.plugins import set_thread_tool_whitelist,clear_thread_tool_whitelist
            set_thread_tool_whitelist({'payment_query','skills_list','skill_view'},deny_msg_fmt='前台只读，禁止 {tool_name}。')
            os.environ['PAYLAB_TASK']=tid
            try:
                result=agent.run_conversation(user_message=task['message'],system_message=(HERE/'config/oncall-role.md').read_text(),conversation_history=self.histories[task['session']],task_id=tid)
                self.histories[task['session']]=result.get('messages',[])
                task.update(result=result,calls=[c for c in tool_calls(result.get('messages',[])) if c['tool_call_id'] not in inherited],
                    model_requests=agent._api_call_count,status='completed' if completed(result) else 'failed',counter_after=agent._turns_since_memory)
            except Exception as e:task.update(status='failed',error=scrub(str(e)))
            finally:
                clear_thread_tool_whitelist();os.environ.pop('PAYLAB_TASK',None)
                task['ended_at']=now();self.persist('tasks',task)
                task['learning_at_return']=[{'id':j['id'],'status':j['status']} for j in self.jobs.values() if j['task_id']==tid]
                self.persist('tasks',task);self.queue.task_done()

    def learn(self):
        import time
        while True:
            ident=self.learning_queue.get()
            if ident is None:self.learning_queue.task_done();break
            item=self.jobs[ident];task=self.tasks[item['task_id']]
            # 不等待复盘的前台先落完整返回记录，后台独立资源随后执行。
            while task['status'] in ('queued','running') or 'learning_at_return' not in task:time.sleep(.05)
            item.update(status='running',started_at=now(),foreground_ended_at=task['ended_at']);self.persist('jobs',item)
            path=runtime()/'learning'/ident/'job.json'
            # 单一 learning_queue 串行维护同名方法；持久基线在候选建立前固定。
            item['maintenance_serial']=True;self.persist('jobs',item)
            try:
                with (observations()/('learning-'+ident+'.log')).open('w') as log:
                    process=subprocess.Popen([sys.executable,'-B',str(HERE/'learning_worker.py'),str(path)],env=os.environ.copy(),cwd=HERE,stdout=log,stderr=log)
                    item['worker_pid']=process.pid;self.persist('jobs',item)
                    try:code=process.wait(timeout=600)
                    except subprocess.TimeoutExpired:
                        process.terminate();process.wait(timeout=15);raise RuntimeError('学习预算600秒耗尽，候选不得采用')
                review_path=path.parent/'review.json'
                if code or not review_path.exists():raise RuntimeError('学习进程失败，详情见专属日志')
                item.update(review=json.loads(review_path.read_text()),status=json.loads(review_path.read_text())['status'])
            except Exception as e:item.update(status='failed',error=scrub(str(e)))
            finally:item['ended_at']=now();self.persist('jobs',item);self.learning_queue.task_done()

    def approve(self,job_id,digest,reviewer,human_confirmed=False):
        from package_control import candidate_check
        from package_integrity import snapshot, package_hash, install_candidate
        import fcntl
        with self.lock, (runtime()/'maintenance.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            item=self.jobs[job_id];review=item.get('review',{})
            current=snapshot(self.home/'skills')
            # 先读后改不是版本锁：正式目录读后变动时，拒绝整个旧基线候选。
            if package_hash(current)!=item['baseline_sha256']:
                raise ValueError('基线哈希冲突，候选未采用；需重新核对当前目录')
            candidate=runtime()/'learning'/job_id/'learning-home/skills'
            checked = candidate_check(review)
            if item['status'] != 'completed' or not checked['candidate_complete']:
                raise ValueError('候选部分写入或回读不完整，未采用')
            if digest != checked['candidate_sha256']:
                raise ValueError('确认哈希不是当前完整候选，未采用')
            if package_hash(snapshot(candidate)) != digest:
                raise ValueError('候选目录已变化，未采用')
            teaching=reviewer=='teaching-auto' and os.environ.get('LAB_AUTO_APPROVE')=='1'
            if not teaching and not (reviewer=='human' and human_confirmed is True):
                raise ValueError('需要明确人工确认；教学自动采用须显式启用')
            if self.queue.unfinished_tasks or self.learning_queue.unfinished_tasks:raise ValueError('仍有任务，不替换方法')
            installed = install_candidate(candidate, self.home/'skills', digest, item['baseline_sha256'])
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
            receipt={'job_id':job_id,'candidate_sha256':digest,'baseline_sha256':item['baseline_sha256'],
                     'reviewer':reviewer,'human_confirmed':human_confirmed,
                     'teaching_only':teaching,'approved_at':now(),'adopted':True,**installed}
            record(observations()/'approvals'/(job_id+'.json'),receipt)
            record(observations()/'approval.json',receipt);return receipt

    def probes(self,job_id):
        from package_control import files_at,package_hash,DIRECTORY,REFERENCE
        from package_integrity import snapshot
        item=self.jobs[job_id];digest=item['review']['candidate_sha256']
        if self.queue.unfinished_tasks or self.learning_queue.unfinished_tasks:
            raise ValueError('等待当前任务结束后再演练失败分支')
        probe_dir=runtime()/'probes';probe_dir.mkdir(exist_ok=False)
        env=os.environ.copy();env['HERMES_HOME']=str(probe_dir/'hermes')
        result=subprocess.run([sys.executable,'-B',str(HERE/'read_guard_worker.py'),str(probe_dir)],
                              env=env,cwd=HERE,capture_output=True,text=True,timeout=60)
        if result.returncode:raise ValueError('原生读取探针失败：'+result.stderr[-1000:])
        native=json.loads((probe_dir/'native.json').read_text())
        official=snapshot(self.home/'skills');official_hash=package_hash(official)
        candidate=runtime()/'learning'/job_id/'learning-home/skills'
        # 支持文件仍旧，不能因主文件已更新便采用半套新方法。
        reference=candidate/DIRECTORY/REFERENCE;full=reference.read_text()
        reference.write_text(item['baseline_files'][DIRECTORY+'/'+REFERENCE])
        partial_rejected=False
        try:
            try:self.approve(job_id,digest,'teaching-auto')
            except ValueError as e:partial_rejected=True;partial_reason=str(e)
        finally:reference.write_text(full)
        # 另一维护者修改当前正式包；读取标记并不能使旧基线候选继续有效。
        main=self.home/'skills'/DIRECTORY/'SKILL.md';original=main.read_text()
        main.write_text(original+'\n构造并发维护记录：待核对的新字段说明。\n')
        concurrent_rejected=False
        try:
            try:self.approve(job_id,digest,'teaching-auto')
            except ValueError as e:concurrent_rejected=True;concurrent_reason=str(e)
        finally:main.write_text(original)
        from concurrent.futures import ThreadPoolExecutor
        import fcntl,time
        activity=[]
        def maintain(label):
            with (runtime()/'maintenance.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                start=__import__('time').monotonic();time.sleep(.08);end=__import__('time').monotonic()
                activity.append({'label':label,'start':start,'end':end})
        with ThreadPoolExecutor(2) as pool:list(pool.map(maintain,['A','B']))
        activity.sort(key=lambda x:x['start'])
        report={**native,'partial_submit_rejected':partial_rejected,'partial_reason':partial_reason,
                'concurrent_submit_rejected':concurrent_rejected,'concurrent_reason':concurrent_reason,
                'official_unchanged':snapshot(self.home/'skills')==official,
                'restored_baseline_sha256':package_hash(snapshot(self.home/'skills')),
                'original_baseline_sha256':official_hash,'candidate_restored_sha256':package_hash(snapshot(candidate)),
                'maintenance_intervals':activity,'serial_verified':activity[0]['end']<=activity[1]['start']}
        record(observations()/'probes.json',report);return report

async def serve(port):
    from aiohttp import web
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    home=prepare();service=Service(home)
    token=(runtime()/'http.token').read_text().strip()
    adapter=APIServerAdapter(PlatformConfig(enabled=True,extra={'key':token,'host':'127.0.0.1','port':port}))
    adapter._session_db=service.session_db
    @web.middleware
    async def auth(request,handler):
        if request.headers.get('Authorization')!='Bearer '+token:return web.json_response({'error':'需要本机课程令牌'},status=401)
        return await handler(request)
    async def control(request):
        path=request.path
        try:
            if request.method=='POST':
                data=await request.json()
                if path=='/tasks':result=service.submit(data['session'],data['message'])
                elif path=='/probes':result=service.probes(data['job_id'])
                elif path=='/approve':result=service.approve(data['job_id'],data['candidate_sha256'],data.get('reviewer'),data.get('human_confirmed',False))
                elif path=='/drain':
                    service.accepting = False
                    result = {'accepting': False}
                elif path=='/shutdown':
                    if service.queue.unfinished_tasks or service.learning_queue.unfinished_tasks:raise ValueError('仍有前台或后台任务，停止未执行')
                    service.accepting=False;service.stop_event.set();result={'stopping':True}
                else:raise ValueError('未知接口')
            elif path=='/status':result={'healthy':True,'pid':os.getpid(),'model':service.model,'official_sessions':True,'accepting':service.accepting,'base_url':service.base_url,'proxy_environment':any(k.lower().endswith('_proxy') and v for k,v in os.environ.items())}
            elif path=='/reviews':result=[j for j in service.jobs.values() if not request.query.get('after_task') or j['task_id']==request.query['after_task']]
            elif path.startswith('/tasks/'):result=service.tasks[path.split('/')[-1]]
            elif path.startswith('/sessions/'):
                sid=path.split('/')[-1];a=service.agent(sid);result={**service.sessions[sid],'counter':a._turns_since_memory}
            else:raise ValueError('未知接口')
            return web.json_response(scrub(result))
        except (ValueError,KeyError) as e:return web.json_response({'error':scrub(str(e))},status=400)
    app=web.Application(middlewares=[auth])
    # 官方 handler 原样使用：创建、列表、读取与消息历史不是课程仿造的 Session API。
    app.router.add_post('/api/sessions',adapter._handle_create_session)
    app.router.add_get('/api/sessions',adapter._handle_list_sessions)
    app.router.add_get('/api/sessions/{session_id}',adapter._handle_get_session)
    app.router.add_get('/api/sessions/{session_id}/messages',adapter._handle_session_messages)
    app.router.add_route('*','/{path:.*}',control)
    runner=web.AppRunner(app,access_log=None);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',port);await site.start()
    try:
        while not service.stop_event.is_set():await asyncio.sleep(.1)
    finally:
        await runner.cleanup();service.queue.put(None);service.learning_queue.put(None)
        service.worker.join(5);service.learner.join(5)
        for a in service.agents.values():a.close()
        service.session_db.close();service.db.close();adapter._response_store.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);p.add_argument('--runtime',required=True);args=p.parse_args()
    os.environ['SKILL_LAB_RUNTIME']=args.runtime
    try:asyncio.run(serve(args.port))
    except Exception as e:print(scrub(str(e)),file=sys.stderr);sys.exit(1)
