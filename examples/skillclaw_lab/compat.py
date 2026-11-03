"""课程兼容层：结构化工具状态优先于 SkillClaw 的全文错误正则。

只在新采集进程的代理生命周期内替换错误摘要分类函数；不改上游文件、
原始工具回执、模型回答或既有归档。这不是 SkillClaw 原生配置。
"""
from contextlib import contextmanager
import json

COMPAT_ID = 'course-structured-tool-status-v1'


def classify_tool_result(content, original):
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return original(content)
    if isinstance(value, dict):
        if value.get('success') is True and 'error' not in value:
            return False, None
        if value.get('success') is False:
            # 失败类型只从实际 error 取，不从 Skill 名称猜测；缺少类型仍是失败。
            error = value.get('error')
            error_text = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
            _, error_type = original(error_text)
            return True, error_type or 'structured_failure'
    return original(content)


@contextmanager
def structured_tool_status_compat():
    from skillclaw import api_server
    original = api_server._classify_tool_error

    def classify(content):
        return classify_tool_result(content, original)

    marker = {
        'id': COMPAT_ID, 'enabled': True, 'kind': 'course_compatibility_layer',
        'target': 'skillclaw.api_server._classify_tool_error',
        'policy': 'JSON success=true 且无 error 键时成功；success=false 时失败；其余沿用原分类器',
        'raw_tool_payloads_unchanged': True,
        'upstream_source_unchanged': True,
    }
    api_server._classify_tool_error = classify
    try:
        yield marker
    finally:
        api_server._classify_tool_error = original


@contextmanager
def strict_evolution_result():
    """格式错误不能被原生 workflow 当作主动 skip 并消费会话。"""
    from evolve_server.pipeline import execution
    from evolve_server.core.constants import DecisionAction
    original = execution._parse_evolve_result

    def parse(raw, skill_name):
        try:
            payload = json.loads(raw[raw.index('{'):raw.rindex('}') + 1])
        except (ValueError, TypeError):
            raise ValueError('演化结果格式无效；不是主动跳过，保留会话和原始回答。') from None
        action = payload.get('action') if isinstance(payload, dict) else None
        allowed = (DecisionAction.SKIP, DecisionAction.CREATE, DecisionAction.IMPROVE, DecisionAction.OPTIMIZE_DESC)
        if action not in allowed:
            raise ValueError('演化结果必须明确返回合法 action，缺失字段不能默认为 skip。')
        if action != DecisionAction.SKIP:
            skill = payload.get('skill')
            field = 'description' if action == DecisionAction.OPTIMIZE_DESC else 'content'
            if (not isinstance(skill, dict) or not isinstance(skill.get(field), str) or not skill[field].strip()
                    or action == DecisionAction.CREATE and not isinstance(skill.get('name'), str)
                    or action == DecisionAction.CREATE and not skill['name'].strip()):
                raise ValueError('演化结果缺少对应动作必需的 Skill 内容；不发布不完整候选。')
        result = original(raw, skill_name)
        if result is None:
            raise ValueError('演化结果格式无效；不是主动跳过，保留会话和原始回答。')
        return result

    execution._parse_evolve_result = parse
    try:
        yield {'id': 'course-strict-evolution-result-v1',
               'kind': 'course_compatibility_layer', 'upstream_source_unchanged': True,
               'policy': '无法解析时走原生异常分支，保留待处理会话；不修改候选'}
    finally:
        execution._parse_evolve_result = original
