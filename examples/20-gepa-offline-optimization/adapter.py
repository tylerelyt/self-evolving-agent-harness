"""官方 GEPAAdapter：执行 Hermes，返回业务评分及可核对的工具依据。"""
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
from support import body_hash
from alert_scorer import grade


def business_grader(task, result):
    verdict = grade(task, result['execution'])
    if verdict['status'] == 'infrastructure_failure':
        raise RuntimeError(verdict['reason'])
    detail = verdict.get('score') or {}
    # 格式错误和待复核回答不算成功；断连等基础设施错误则中止搜索，不给零分。
    score = float(detail.get('overall_pass') is True)
    failed = [name for section in ('business_fields', 'evidence_checks')
              for name, ok in detail.get(section, {}).items() if not ok]
    failed.extend(name for name in ('schema_pass', 'format_pass') if detail.get(name) is False)
    needs_review = verdict['status'] == 'needs_review' or bool(verdict.get('needs_review'))
    reason = verdict.get('reason')
    if needs_review:
        failed.append('needs_review')
        note = ('JSON 代码块外存在附加说明，需要人工复核' if verdict.get('outside_text')
                else '输出尚不能自动判定，需要人工复核')
        reason = reason + '；' + note if reason else note
    elif failed and not reason:
        reason = '未通过检查：' + '、'.join(failed)
    if failed and detail.get('business_pass') is True and detail.get('trace_pass') is True:
        reason += '；业务字段与工具依据检查均通过，输出规则问题不等于业务诊断错误'
    feedback = {
        'status': verdict['status'], 'failed_checks': failed,
        'reason': reason, 'needs_review': needs_review, 'expected': task['expected'],
        'required_citations': task['required_citations'], 'grading': verdict,
    }
    return {'score': score, 'feedback': feedback}


class OncallAdapter(GEPAAdapter):
    def __init__(self, run_case, grade_case=business_grader, record_reflection=None, record_evaluation=None):
        self.run_case = run_case
        self.grade_case = grade_case
        self.record_reflection = record_reflection
        self.record_evaluation = record_evaluation

    def evaluate(self, batch, candidate, capture_traces=False):
        if set(candidate) != {'skill_body'}:
            raise ValueError('本讲只优化 skill_body')
        outputs, scores = [], []
        traces = [] if capture_traces else None
        for task in batch:
            result = self.run_case(task=task, skill_body=candidate['skill_body'])
            if result['skill_body_sha256'] != body_hash(candidate['skill_body']):
                raise RuntimeError('实际执行的 skill_body 与候选不一致')
            verdict = self.grade_case(task, result)
            outputs.append(result['report'])
            scores.append(verdict['score'])
            trace = {'task': task['task'], 'case_id': task['id'], 'split': task['group'],
                     'report': result['report'], 'tool_calls': result['tool_calls'],
                     'feedback': verdict['feedback'], 'score': verdict['score'],
                     'artifact_dir': result.get('artifact_dir'),
                     'skill_body_sha256': result['skill_body_sha256']}
            if self.record_evaluation:
                self.record_evaluation(trace)
            if capture_traces:
                traces.append(trace)
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=traces)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        traces = eval_batch.trajectories
        if traces is None or len(traces) != len(eval_batch.scores):
            raise ValueError('反思需要逐题真实轨迹')
        if any(t['split'] != 'train' for t in traces):
            raise ValueError('只有训练轨迹可以进入反思材料')
        if any(t['skill_body_sha256'] != body_hash(candidate['skill_body']) for t in traces):
            raise ValueError('反思轨迹不属于当前候选')
        if components_to_update != ['skill_body']:
            raise ValueError('未知优化组件')
        records = [{'Inputs': {'task': t['task'], 'case_id': t['case_id']},
                    'Generated Outputs': {'report': t['report'], 'tool_calls': t['tool_calls']},
                    'Feedback': t['feedback'], 'score': t['score']} for t in traces]
        material = {'skill_body': records}
        if self.record_reflection:
            self.record_reflection(material)
        return material
