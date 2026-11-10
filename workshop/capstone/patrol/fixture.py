"""Optional deterministic model for wiring tests of the real Hermes loop."""

import json

from environment import classify
from hermes_runtime import FOLLOWUP_PREFIX, ONCALL_SKILL


CONFIRMATION_STEP = "首次探测返回 5xx 时，再调用一次 oncall_probe"


def respond_followup(scope, current):
    if not current:
        return "skill_view", {"name": ONCALL_SKILL}

    probes = []
    report = {key: scope[key] for key in ("incident_id", "service", "environment_id")}
    report.update(probe_http_statuses=probes, root_cause="unknown")
    name, content = current[0]
    skill = json.loads(content)
    if (name != "skill_view" or not skill.get("success") or not skill.get("sha256")
            or "## 事故补查与值班交接" not in skill.get("content", "")):
        return None, {**report, "error": "The follow-up Skill could not be read"}

    for name, content in current[1:]:
        receipt = json.loads(content)
        status = receipt.get("http_status")
        if (name != "followup_probe" or receipt.get("error")
                or receipt.get("service") != scope["service"]
                or receipt.get("environment_id") != scope["environment_id"]
                or type(status) is not int or status // 100 not in (2, 5)):
            return None, {**report, "error": "The follow-up probe lacks a valid scoped HTTP receipt"}
        probes.append(status)

    if len(probes) < 3:
        return "followup_probe", {}
    if len(probes) != 3:
        return None, {**report, "error": "The follow-up requires exactly three probes"}
    failures = sum(status >= 500 for status in probes)
    report["assessment"] = "persistent" if failures == 3 else "intermittent" if failures else "not_reproduced"
    report["analysis"] = f"{scope['service']} 的三次探测有 {failures} 次 5xx，只能判断当前故障表现，根因未知。"
    report["recovery_plan"] = {
        "action": "clear_lab_fault" if failures else "observe", "approval_required": bool(failures),
        "verification": "two_clean_windows" if failures else "continue_patrol",
    }
    return None, report


def respond(messages):
    current = []
    calls = {}
    prompt = None
    for item in messages:
        if item.get("role") == "user":
            current.clear()
            calls.clear()
            prompt = item.get("content")
        elif item.get("role") == "assistant":
            calls.update({call["id"]: call["function"]["name"] for call in item.get("tool_calls") or []})
        elif item.get("role") == "tool":
            current.append((calls.get(item["tool_call_id"]), item["content"]))
    if isinstance(prompt, str) and prompt.startswith(FOLLOWUP_PREFIX):
        header = prompt.partition("\n")[0]
        return respond_followup(json.loads(header.removeprefix(FOLLOWUP_PREFIX)), current)
    results = {}
    alert = None
    probes = []
    for name, content in current:
        value, end = json.JSONDecoder().raw_decode(content)
        results[name] = value
        if name == "oncall_probe" and "http_status" in value:
            probes.append(value["http_status"])
        for line in content[end:].splitlines():
            if line.startswith("ONCALL_ALERT "):
                alert = json.loads(line.removeprefix("ONCALL_ALERT "))
    if "patrol_skill_view" not in results:
        return "patrol_skill_view", {}
    if not results["patrol_skill_view"].get("success"):
        return None, {"status": "unavailable", "history_id": None, "summary": "巡检 Skill 无法读取。"}
    if "service_snapshot" not in results:
        return "service_snapshot", {}
    observation = results["service_snapshot"]
    if observation.get("error"):
        return None, {"status": "unavailable", "history_id": None, "summary": observation["error"]}
    if alert and "skill_view" not in results:
        return "skill_view", {"name": ONCALL_SKILL}
    if alert and results["skill_view"].get("sha256"):
        confirm = CONFIRMATION_STEP in results["skill_view"]["content"]
        if not probes or (confirm and len(probes) == 1 and probes[0] >= 500):
            return "oncall_probe", {}
    if observation["history_available"] and "search_inspections" not in results:
        return "search_inspections", {}
    history = results.get("search_inspections", [])
    report = {
        "status": classify(observation),
        "history_id": history[0]["id"] if history else None,
        "summary": f"当前窗口有 {observation['requests']} 个请求，"
                   f"5xx 比例 {observation['error_rate']}，P95 {observation['p95_ms']} ms。",
    }
    if alert and probes:
        status = probes[0]
        assessment = "failure_observed" if status >= 500 else "not_reproduced"
        if len(probes) == 2:
            assessment = "persistent" if probes[1] >= 500 else "intermittent"
        report.update(
            incident_id=alert["incident_id"], probe_http_status=status,
            probe_http_statuses=probes, assessment=assessment,
        )
        report["summary"] += (
            f"当前探测依次返回 {probes}。影响范围与根因仍需核实。"
            "请值班人检查实验故障开关，处置后等待新一轮指标确认恢复。"
        )
    return None, report
