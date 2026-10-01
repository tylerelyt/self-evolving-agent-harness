"""隔离候选内，Hermes 的原生 bg-review target 读取并修订同名方法。"""
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from common import HERE, record, now, sanitize_logging
from package_control import files_at, package_hash, candidate_check, NAME, REFERENCE
from package_integrity import snapshot, content_files

job_path = Path(sys.argv[1]); job = json.loads(job_path.read_text())
home = job_path.parent/'learning-home'; home.mkdir()
(home/'config.yaml').write_text((HERE/'config/hermes.yaml').read_text())
(home/'.no-bundled-skills').touch()
source = Path(os.environ['HERMES_HOME'])/'skills'
if package_hash(snapshot(source)) != job['baseline_sha256']:
    raise RuntimeError('建立候选前基线已变化，停止本次维护')
shutil.copytree(source, home/'skills', symlinks=True)
before_package = snapshot(home/'skills')
if package_hash(before_package) != job['baseline_sha256']:
    raise RuntimeError('复制期间基线已变化，停止本次维护')
os.environ['HERMES_HOME'] = str(home)
sanitize_logging()
from run_agent import AIAgent
from agent.background_review import spawn_background_review_thread
from review_observer import ReviewObserver
from payctl import register
register()
parent = AIAgent(model=job['model'],provider='glm',api_key=os.environ['GLM_API_KEY'],base_url=job['base_url'],
                session_id=job['parent_session'],enabled_toolsets=['memory','skills','oncall_payments'],
                quiet_mode=True,skip_context_files=True,max_iterations=20)
parent._cached_system_prompt=job['cached_prompt']
parent._memory_nudge_interval=0;parent._skill_nudge_interval=0
before=content_files(before_package)
observer=ReviewObserver(job);observer.install()
try:
    target,prompt=spawn_background_review_thread(parent,job['snapshot'],review_memory=False,review_skills=True,
                         focus=(HERE/'config/skill-review-policy.md').read_text())
    thread=threading.Thread(target=target,name='bg-review',daemon=True);thread.start();thread.join()
    after_package=snapshot(home/'skills')
    after=content_files(after_package)
    result={**observer.result,'job_id':job['id'],'before':before,'after':after,'prompt':prompt,
            'baseline_sha256':job['baseline_sha256'],'ended_at':now(),
            'before_package':before_package,'after_package':after_package,
            'status':'completed' if observer.result.get('completed') else 'failed'}
    # 模型最后一句“已完成”不能代替核对。执行器另用原生工具回读两份最终文件。
    # 这两次读取单列，不混入模型的调用编号、请求数或自主动作。
    from tools.skills_tool import skill_view
    result['verification_reads']=[]
    for file_path in (None,REFERENCE):
        args={'name':NAME}
        if file_path:args['file_path']=file_path
        raw=skill_view(**args)
        receipt=json.loads(raw) if isinstance(raw,str) else raw
        result['verification_reads'].append({'name':'skill_view','args':args,'receipt':receipt,
                  'source':'course-verifier','at':now()})
    record(job_path.parent/'review.json',result)
    result.update(candidate_check(result))
    record(job_path.parent/'review.json',result)
finally:
    observer.restore();parent.close()
