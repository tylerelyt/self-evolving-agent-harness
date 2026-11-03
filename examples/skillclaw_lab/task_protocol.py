"""任务的执行约定；不读取快照、题号、预期动作或检查结果。"""

PROTOCOL_VERSION = 'skill-branches-v1'


def task_instructions(role: str) -> dict:
    if role not in ('source-a', 'source-b', 'receiver'):
        raise ValueError('未知任务角色')
    system = (
        '你负责一次只读值班初查。先用 skill_view 读取 timeout-triage 的完整正文，'
        '再按正文和用户明确补充的方法执行；技能简介不能替代正文中的条件。'
        '工具可用不表示本轮需要调用全部工具。每次选取后续检查前，'
        '先根据已经收到的回执说明适用哪个分支；条件不满足的步骤不要执行。'
        '依赖前置结果的检查不能与前置检查同批发起。'
        '走到报告或后续建议时结束本轮，不为穷尽可能性而继续其他分支。'
        '报告区分实际观测、尚待验证的解释和未执行的建议；'
        '同时出现的异常不能直接写成因果关系。不要修改 Skill。'
    )
    user = (
        'payment-api 在 cn-east 的 production 环境持续超时。'
        '请使用 timeout-triage 完成一轮初查，按适用分支报告结果和下一步建议，'
        '不要自行扩大为全面排查。'
    )
    if role == 'source-b':
        user += (
            '本任务补充旧方法尚未覆盖的一步：如果健康和上游延迟都正常，'
            '继续检查连接池以及连接占用和释放情况，再报告。'
            '这个补充只适用于上述条件成立时；不要直接断言最终根因。'
        )
    return {'version': PROTOCOL_VERSION, 'system': system, 'user': user}
