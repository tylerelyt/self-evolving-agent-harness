"""真实原生读取拒绝探针；不调用模型，不在正式方法包内演练失败。"""
import json
import os
import shutil
import sys
from pathlib import Path
from common import HERE, record
from package_control import NAME, DIRECTORY, REFERENCE, files_at, package_hash, consistent

out = Path(sys.argv[1]); home=out/'hermes';home.mkdir()
(home/'config.yaml').write_text((HERE/'config/hermes.yaml').read_text())
(home/'.no-bundled-skills').touch()
shutil.copytree(HERE/'materials/baseline',home/'skills')
os.environ['HERMES_HOME']=str(home)
from tools.skill_manager_tool import skill_manage, _reset_background_review_read_marks
from tools.skills_tool import skill_view
from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

trace=[]
def call(tool_name, **args):
    raw=skill_view(**args) if tool_name=='skill_view' else skill_manage(**args)
    receipt=json.loads(raw) if isinstance(raw,str) else raw
    trace.append({'name':tool_name,'args':args,'receipt':receipt})
    return receipt

token=set_current_write_origin('background_review')
try:
    _reset_background_review_read_marks()
    before=files_at(home/'skills')
    old='仅限单笔足额支付' if '仅限单笔足额支付' in before[DIRECTORY+'/SKILL.md'] else '单笔'
    new='单笔或分次付款'
    unread=call('skill_manage',action='patch',name=NAME,old_string=old,new_string=new)
    untouched=files_at(home/'skills')==before
    main_read=call('skill_view',name=NAME)
    patch=call('skill_manage',action='patch',name=NAME,old_string=old,new_string=new)
    ref_unread=call('skill_manage',action='write_file',name=NAME,file_path=REFERENCE,
                    file_content=before[DIRECTORY+'/'+REFERENCE]+'\n构造探针：分次付款按订单汇总并去重，未知仍待确认。\n')
    partial=files_at(home/'skills')
    ref_read=call('skill_view',name=NAME,file_path=REFERENCE)
    ref_write=call('skill_manage',action='write_file',name=NAME,file_path=REFERENCE,
                   file_content=before[DIRECTORY+'/'+REFERENCE]+'\n构造探针：分次付款按订单汇总并去重，未知仍待确认。\n')
    report={'unread_main_rejected':bool(unread.get('_read_before_write_required') and not unread.get('success') and untouched),
            'main_read_patch_succeeded':bool(main_read.get('success') and patch.get('success')),
            'unread_reference_rejected':bool(ref_unread.get('_read_before_write_required') and not ref_unread.get('success')),
            'real_partial_write':partial[DIRECTORY+'/SKILL.md']!=before[DIRECTORY+'/SKILL.md'] and partial[DIRECTORY+'/'+REFERENCE]==before[DIRECTORY+'/'+REFERENCE],
            'partial_consistency_failed':not all(consistent(partial).values()),
            'reference_read_write_succeeded':bool(ref_read.get('success') and ref_write.get('success')),
            'before_sha256':package_hash(before),'partial_sha256':package_hash(partial),
            'partial_files':partial,'trace':trace,'probe_only':True,'model_requests':0}
    record(out/'native.json',report)
finally:
    reset_current_write_origin(token)
