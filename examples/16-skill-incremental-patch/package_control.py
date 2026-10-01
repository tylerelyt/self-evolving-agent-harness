"""课程补充的整目录控制；原生读取标记不承担版本锁或目录事务。"""
import difflib
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

from common import HERE
from package_integrity import (
    snapshot, content_files, native_review_complete, verify_review_package,
)

MANIFEST = json.loads((HERE / 'materials/baseline-source.json').read_text())
NAME = MANIFEST['name']
DIRECTORY = MANIFEST['directory']
REFERENCE = 'references/payment-query-fields.md'


def files_at(directory):
    """正文清单包含隐藏文件；来源信息另在完整包中参与审批。"""
    return content_files(snapshot(directory))


def package_hash(files):
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def baseline_check():
    files = files_at(HERE / 'materials/baseline')
    actual = {p: hashlib.sha256(s.encode()).hexdigest() for p, s in files.items()}
    if actual != MANIFEST['files'] or package_hash(files) != MANIFEST['package_sha256']:
        raise ValueError('第15讲起点文件与已确认完整包不符')
    usage = HERE / 'materials/baseline/.usage.json'
    if hashlib.sha256(usage.read_bytes()).hexdigest() != MANIFEST['provenance_sha256']:
        raise ValueError('起点的原生维护资格记录不符')
    return files


def consistent(files):
    """保守的结构检查；具体付款判断还须用真实模型查询验证。"""
    base = baseline_check()
    main = files.get(DIRECTORY + '/SKILL.md', '')
    ref = files.get(DIRECTORY + '/' + REFERENCE, '')
    checks = {
        '文件集合保持完整': set(files) == set(base),
        '保留同名方法': bool(re.search(r'^name:\s*' + re.escape(NAME) + r'\s*$', main, re.M)),
        '主文件与引用均已修订': main != base[DIRECTORY + '/SKILL.md'] and ref != base[DIRECTORY + '/' + REFERENCE],
        '主文件引用仍可访问': REFERENCE in main and bool(ref),
        '无关旧方法保持原文': all(files.get(p) == s for p,s in base.items() if not p.startswith(DIRECTORY + '/')),
        '两份说明都覆盖单笔与分次': all('单笔' in s and '分次' in s for s in (main,ref)),
        '两份说明都保留去重和未知': all('去重' in s and '未知' in s and '汇总' in s for s in (main,ref)),
        '两份说明都先付足再核通知': all(any(w in s for w in ('付足','足额','付清')) and '通知' in s for s in (main,ref)),
        '只读与受理语义保留': all('request_result' in s and 'final_status' in s for s in (main,ref)) and '只读' in main and '补发' in main,
        '工具重试观测未写进业务方法': all('重试' not in s and 'retry' not in s.lower() for s in (main,ref)),
        '当前规则查询与工具说明同步': all('rules' in s for s in (main,ref)) and '支持五个' not in main,
    }
    return {k:bool(v) for k,v in checks.items()}


def candidate_check(review):
    # 与原生 patch 使用同一匹配器重放；它允许格式差异，不能误当作精确替换。
    source = Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent')) / 'tools/fuzzy_match.py'
    spec = importlib.util.spec_from_file_location('lab16_native_fuzzy_match', source)
    matcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matcher)
    before, after = review['before'], review['after']
    calls = review.get('calls', [])
    state = dict(before)
    marks = set()
    ever_read = set()
    trace = []
    valid = native_review_complete(review) and verify_review_package(review)
    written = set()
    failures = []
    for call in calls:
        args = call['args']; receipt = call.get('receipt') or {}
        if not isinstance(receipt,dict):
            receipt={'success':False,'error':str(receipt)}
        target = DIRECTORY + '/' + (args.get('file_path') or 'SKILL.md')
        if args.get('name') not in (NAME,DIRECTORY):
            if call['name'] == 'skill_manage': valid = False
            continue
        if call['name'] == 'skill_view' and receipt.get('success'):
            matches = receipt.get('content') == state.get(target)
            if matches:
                marks.add(target)
                ever_read.add(target)
            trace.append({'call_id':call['tool_call_id'],'path':target,'read_matches_current':matches})
        if call['name'] == 'skill_manage':
            action = args.get('action')
            allowed = action in ('patch','write_file') and target in (DIRECTORY+'/SKILL.md',DIRECTORY+'/'+REFERENCE)
            valid &= allowed
            if not receipt.get('success'):
                failures.append({'path':target,'call_id':call['tool_call_id'],'index':len(trace),
                                 'read_guard_rejection':bool(receipt.get('_read_before_write_required'))})
                trace.append({'call_id':call['tool_call_id'],'path':target,'action':action,'native_success':False})
                continue
            valid &= target in ever_read
            if action == 'patch':
                old,new = args.get('old_string',''),args.get('new_string','')
                replaced,count,strategy,error = matcher.fuzzy_find_and_replace(
                    state.get(target,''),old,new,args.get('replace_all',False))
                valid &= error is None and count > 0
                if error is None: state[target] = replaced
            elif action == 'write_file': state[target] = args.get('file_content','')
            else: valid = False
            marks.discard(target);written.add(target)
            trace.append({'call_id':call['tool_call_id'],'path':target,'action':action,'native_success':bool(receipt.get('success')),
                          **({'match_strategy':strategy,'match_count':count,'match_error':error} if action=='patch' else {})})
    structural = consistent(after)
    for failure in failures:
        failure['repaired'] = any(x.get('path')==failure['path'] and x.get('native_success') for x in trace[failure['index']+1:])
    valid &= all(f['read_guard_rejection'] and f['repaired'] for f in failures)
    expected_files={DIRECTORY+'/SKILL.md',DIRECTORY+'/'+REFERENCE}
    verified=set()
    for read in review.get('verification_reads',[]):
        args=read.get('args',{});receipt=read.get('receipt') or {}
        target=DIRECTORY+'/'+(args.get('file_path') or 'SKILL.md')
        if (read.get('source')=='course-verifier' and args.get('name')==NAME
                and receipt.get('success') and receipt.get('content')==after.get(target)):
            verified.add(target)
    native_readback=expected_files<=verified
    model_readback=expected_files<=marks
    valid &= state == after and written == expected_files and (model_readback or native_readback)
    valid &= all(structural.values())
    before_usage = json.loads(review.get('before_package', {}).get('.usage.json', '{}'))
    after_usage = json.loads(review.get('after_package', {}).get('.usage.json', '{}'))
    provenance = before_usage.get(NAME, {}).get('created_by') == 'agent'
    provenance &= after_usage.get(NAME, {}).get('created_by') == 'agent'
    valid &= provenance
    diff = ''.join(''.join(difflib.unified_diff(before[p].splitlines(True),after.get(p,'').splitlines(True),fromfile='before/'+p,tofile='after/'+p)) for p in before if before[p] != after.get(p))
    return {'candidate_complete':bool(valid),'candidate_name':NAME,'candidate_directory':DIRECTORY,
            'candidate_sha256':package_hash(review.get('after_package', {})),
            'baseline_sha256':package_hash(review.get('before_package', {})),
            'native_review_complete':native_review_complete(review),'source_preserved':provenance,
            'structural_checks':structural,'read_write_trace':trace,'write_failures':failures,'diff':diff,
            'model_final_readback':model_readback,'independent_native_readback':native_readback,
            'decision':'revise-existing' if written else 'no-update'}
