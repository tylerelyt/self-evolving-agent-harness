"""独立 HOME 重建父对象，bg-review 线程执行 Hermes 返回的原生 target。"""
import argparse
import json
import os
import sys
import threading
from pathlib import Path
from common import HERE, record, now, sanitize_logging, tool_calls
from package_integrity import snapshot, content_files, package_hash

p=argparse.ArgumentParser();p.add_argument('job');a=p.parse_args()
job_path=Path(a.job);job=json.loads(job_path.read_text());home=job_path.parent/'learning-home'
home.mkdir();(home/'config.yaml').write_text((HERE/'config/hermes.yaml').read_text())
(home/'.no-bundled-skills').touch()
import shutil
source = Path(os.environ['HERMES_HOME'])/'skills'
if package_hash(snapshot(source)) != job['baseline_sha256']:
    raise RuntimeError('建立候选前基线已变化，停止本次维护')
shutil.copytree(source, home/'skills', symlinks=True)
before_package = snapshot(home/'skills')
if package_hash(before_package) != job['baseline_sha256']:
    raise RuntimeError('复制期间基线已变化，停止本次维护')
os.environ['HERMES_HOME']=str(home)
sanitize_logging()
from run_agent import AIAgent
from agent.background_review import spawn_background_review_thread
from review_observer import ReviewObserver
from payctl import register
register()
parent=AIAgent(model=job['model'],provider='glm',api_key=os.environ['GLM_API_KEY'],base_url=job['base_url'],session_id=job['parent_session'],
  enabled_toolsets=['memory','skills','oncall_payments'],quiet_mode=True,skip_context_files=True,max_iterations=16)
# 保留原生复盘提示，用原生 focus 参数传入本轮范围和核查要求。
learning_focus=(HERE/'config/skill-review-policy.md').read_text()
parent._cached_system_prompt=job['cached_prompt']
parent._memory_nudge_interval=0;parent._skill_nudge_interval=0
before = content_files(before_package)
observer=ReviewObserver(job);observer.install()
try:
    target,prompt=spawn_background_review_thread(parent,job['snapshot'],review_memory=False,review_skills=True,focus=learning_focus)
    t=threading.Thread(target=target,name='bg-review',daemon=True)
    t.start();t.join()
    after_package = snapshot(home/'skills')
    after = content_files(after_package)
    result={**observer.result,'job_id':job['id'],'started_at':now(),'learning_home':str(home),'before':before,'after':after,'prompt':prompt,
            'status':'completed' if observer.result.get('completed') else 'failed',
            'before_package':before_package,'after_package':after_package,
            'baseline_sha256':job['baseline_sha256']}
    # 候选与调用逐字比较；不能由 target 返回推断保存成功。
    from validation_checks import candidate_check
    record(job_path.parent/'review.json',result)
    result.update(candidate_check(result))
    record(job_path.parent/'review.json',result)
finally:
    observer.restore();parent.close()
