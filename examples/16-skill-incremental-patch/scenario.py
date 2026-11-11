"""构造多服务告警现场，关联结论须由当前时间线、依赖与探测交叉核对。"""
TITLE = "从逐条告警排查扩展到事件级关联"
SEED_NAME = "oncall-alert-triage"
SEED = """---
name: oncall-alert-triage
description: 只读调查单条服务告警，核对指标、日志和对应组件的当前状态。
---
# 单条告警调查
1. 确认服务、告警时段和影响范围。
2. 核对当前指标与日志，不凭告警标题直接下结论。
3. 用只读探测确认可疑组件，区分观察到的事实和待确认部分。
4. 汇报影响与当前依据，不执行恢复或改配置。
字段与工具用法见 references/alert-tools.md。
"""
REFERENCE = """# 只读工具
investigate 的 alerts 列出当前告警，metrics/log 查看对应服务。
topology 返回服务依赖关系，trace 返回调用链及传播方向，probe 查询指定组件。
所有数据都是构造值，未知结果不能当作正常。
"""
TASK = "构造事件 east 出现多实例、多服务告警。请先读原方法，再调查当前告警，核对时间、依赖、调用链、传播方向与探测，判断哪些告警属于共同故障，哪些需要分别调查。只读，不执行恢复。"
FUTURE_TASK = "构造事件 west 出现多服务告警。请查找可用方法并调查当前记录，区分共同原因、独立问题和尚未确认的部分。"
ANSWER_FORMAT = "最后给JSON：groups为已确认分组，每项包含services列表与cause_component；unresolved_services列尚未确认原因的服务；note说明依据。"
INITIAL_CASE = "east"
FUTURE_CASES = [("west", FUTURE_TASK), ("single", "构造事件 single 只有一条告警。请查找可用方法并完成只读调查。")]
FOCUS = """本次复盘多告警调查。先 skills_list、skill_view 核对已有 oncall-alert-triage 主文件和 references/alert-tools.md。
从本次调查的有效技术与工具回执提炼经验。优先 patch 现有 Skill，补充事件级多告警关联步骤，不为同一排查工作另造近似 Skill。
外层 Harness 汇总跨时段、多实例的任务分布，本次 Background Review 只复盘传入的这一段 Session，不声称自行看到了其他会话。
工具暂时故障是工具问题；原方法有步骤而执行漏做是执行问题；原方法未覆盖事件级关联才是方法缺口。
新流程先判断单告警或多告警：单条保留原路径；多条汇总时间、依赖、调用链与传播方向，并验证根因。同期告警不必同源。
Procedure first，把条件与教训融入相应步骤，不只在末尾追加教训。工具字段细节放引用文件，并保持主文件与引用一致。
不要保存本次组件名、区域名、时间戳或未经核实的原因。拓扑共同依赖与时间接近只是待核对线索，不能单凭这两项确认共同故障。
先读目标文件再用原生 skill_manage patch，引用按需 write_file；核对成功回执并回读最终文件。无可靠新增流程时允许不修改。"""
SCHEMA = {"name": "investigate", "description": "只读构造告警：alerts、metrics、log、topology、trace、probe。case 选 east/west/single，target 为服务或组件名。",
          "parameters": {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["alerts", "metrics", "log", "topology", "trace", "probe"]},
              "case": {"type": "string", "enum": ["east", "west", "single"]},
              "target": {"type": "string"}}, "required": ["action", "case"]}}


def dataset(case):
    if case == "single":
        data = dataset("east")
        service = data["alerts"][0]["service"]
        return {**data, "alerts": data["alerts"][:1],
                "topology": {service: data["topology"][service]},
                "logs": {service: data["logs"][service]},
                "traces": data["traces"][:1],
                "probes": {data["shared"]: data["probes"][data["shared"]]}}
    if case not in ("east", "west"):
        raise ValueError("未知构造事件")
    suffix = "e" if case == "east" else "w"
    shared = "storage-" + suffix
    a, b, unrelated = "orders-" + suffix, "billing-" + suffix, "search-" + suffix
    return {
        "alerts": [{"service": a, "instance": a+"-1", "at": "10:00:02", "signal": "latency"},
                   {"service": b, "instance": b+"-2", "at": "10:00:04", "signal": "error_rate"},
                   {"service": unrelated, "instance": unrelated+"-1", "at": "10:00:03", "signal": "queue_depth"}],
        "topology": {a: [shared], b: [shared], unrelated: ["queue-" + suffix]},
        "traces": [{"caller": a, "dependency": shared, "direction": shared+" -> "+a, "at": "10:00:01", "error": "dependency_timeout"},
                   {"caller": b, "dependency": shared, "direction": shared+" -> "+b, "at": "10:00:03", "error": "connection_refused"},
                   {"caller": unrelated, "dependency": "queue-"+suffix, "direction": "queue-"+suffix+" -> "+unrelated, "at": "10:00:02", "error": "consumer_stalled"}],
        "logs": {a: ["10:00:01 dependency timeout: " + shared],
                 b: ["10:00:03 connection refused: " + shared],
                 unrelated: ["10:00:02 consumer stalled: queue-" + suffix]},
        "probes": {shared: {"status": "failed", "onset": "09:59:59"},
                   "queue-" + suffix: {"status": "failed" if case == "east" else "unknown",
                                      "onset": "10:00:00" if case == "east" else None}},
        "shared": shared,
    }


def query(action, case, target=""):
    data = dataset(case)
    if action == "alerts":
        result = data["alerts"]
    elif action == "topology":
        result = data["topology"]
    elif action == "trace":
        result = data["traces"]
    elif action == "log":
        result = data["logs"].get(target)
    elif action == "metrics":
        result = next(({"service": target, "signal": a["signal"], "before": 1, "during": 20}
                       for a in data["alerts"] if a["service"] == target), None)
    elif action == "probe":
        result = data["probes"].get(target, {"status": "unknown", "onset": None})
    else:
        raise ValueError("只允许只读调查动作")
    return {"success": result is not None, "constructed": True, "read_only": True,
            "case": case, "result": result}


def verify(calls, case):
    from common import receipt_object
    data = dataset(case)
    queries = [c for c in calls if c["name"] == "investigate" and c["args"].get("case") == case
               and receipt_object(c).get("success")]
    actions = {c["args"].get("action") for c in queries}
    probes = {c["args"].get("target"): receipt_object(c).get("result")
              for c in queries if c["args"].get("action") == "probe"}
    logs = {c["args"].get("target") for c in queries if c["args"].get("action") == "log"}
    metrics = {c["args"].get("target") for c in queries if c["args"].get("action") == "metrics"}
    required = {"alerts", "metrics", "log", "probe"}
    if case != "single":
        required |= {"topology", "trace"}
    return (required <= actions
            and set(data["topology"]) <= logs
            and set(data["topology"]) <= metrics
            and all(probes.get(k) == v for k, v in data["probes"].items()))


def verify_answer(answer, case):
    data = dataset(case)
    groups = {}
    unknown = []
    for service, dependencies in data["topology"].items():
        component = dependencies[0]
        if data["probes"][component]["status"] == "failed":
            groups.setdefault(component, []).append(service)
        else:
            unknown.append(service)
    actual = {}
    groups_input = answer.get("groups", [])
    unresolved = answer.get("unresolved_services", [])
    if not isinstance(groups_input, list) or not isinstance(unresolved, list):
        return False
    if not all(isinstance(s, str) for s in unresolved):
        return False
    for group in groups_input:
        if not isinstance(group, dict) or not isinstance(group.get("services"), list):
            return False
        if not all(isinstance(s, str) for s in group["services"]):
            return False
        component = group.get("cause_component")
        if not isinstance(component, str) or component in actual:
            return False
        actual[component] = sorted(group.get("services", []))
    return (actual == {k: sorted(v) for k, v in groups.items()}
            and sorted(answer.get("unresolved_services", [])) == sorted(unknown))
