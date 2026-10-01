"""构造的支付日志现场；模型通过只读工具查询，不提供文件或命令执行。"""
import copy
from datetime import datetime

OLD_ENTRY = "payment-api-legacy"
CURRENT_ENTRY = "payment-api-prod"
FIRST_WINDOW = ("2026-10-20T10:00:00Z", "2026-10-20T10:05:00Z")
NEXT_WINDOW = ("2026-10-20T11:00:00Z", "2026-10-20T11:05:00Z")
MIGRATION = {
    "change_id": "PAY-2026-1020", "owner": "支付平台组",
    "effective_at": "2026-10-20T09:00:00Z", "service": "payment-api",
    "environment": "prod", "current_entry": CURRENT_ENTRY, "archive_entry": OLD_ENTRY,
    "scope": "生效后的生产日志写入 current_entry；archive_entry 只保留生效前历史记录。",
    "verified_by": "本练习固定部署记录与日志数据相互核对",
}
ROWS = {
    OLD_ENTRY: [{"at": "2026-10-20T08:30:00Z", "status": "ok", "request_id": "old-1"}],
    CURRENT_ENTRY: [
        {"at": "2026-10-20T10:01:00Z", "status": "timeout", "request_id": "first-1"},
        {"at": "2026-10-20T10:02:00Z", "status": "timeout", "request_id": "first-2"},
        {"at": "2026-10-20T11:01:00Z", "status": "timeout", "request_id": "next-1"},
    ],
}
ROLE = """你负责教学支付系统的日志调查。业务数据是构造的，工具查询实际执行。
用 lesson11_logs 核对部署与日志；只根据实际回执报告入口、时段、样本数和现象。
监控携带的入口可能是历史采样，不代表当前部署。不要把空日志解释成业务恢复。
自行决定调查步骤；不能假装已经查过。"""
FIRST_TASK = (
    f"核对 payment-api 生产告警，时段 {FIRST_WINDOW[0]} 至 {FIRST_WINDOW[1]}。"
    f"监控入口样本：{OLD_ENTRY}，采集于 2026-10-19T18:00:00Z。请说明日志依据。"
)
NEXT_TASK = (
    f"核对 payment-api 新一次生产告警，时段 {NEXT_WINDOW[0]} 至 {NEXT_WINDOW[1]}。"
    "请说明日志依据。"
)
SCHEMA = {
    "name": "lesson11_logs",
    "description": "只读查询教学支付部署和日志。describe 返回已核实的部署记录；read 按入口及 UTC 时段读取日志。",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["describe", "read"]},
        "entry": {"type": "string"}, "since": {"type": "string"}, "until": {"type": "string"},
    }, "required": ["action"], "additionalProperties": False},
}


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed


def query(args):
    try:
        if args.get("action") == "describe":
            return {"success": True, "deployment": copy.deepcopy(MIGRATION)}
        if args.get("action") != "read":
            raise ValueError("仅支持 describe/read")
        entry = args["entry"]
        if entry not in ROWS:
            raise ValueError("日志入口不存在；可通过 describe 核对部署")
        since, until = instant(args["since"]), instant(args["until"])
        if since > until:
            raise ValueError("开始时间晚于结束时间")
        rows = [r for r in ROWS[entry] if since <= instant(r["at"]) <= until]
        return {"success": True, "entry": entry, "since": args["since"],
                "until": args["until"], "count": len(rows), "records": copy.deepcopy(rows)}
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        return {"success": False, "error": str(exc)}


def adoption(calls, window):
    """只核对可观察的查询行为，不把它自动归因于记忆。"""
    business = [c for c in calls if c["name"] == "lesson11_logs"]
    matched = []
    for call in business:
        receipt = call.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("success") is not True:
            continue
        try:
            valid = (receipt.get("entry") == CURRENT_ENTRY and receipt.get("count", 0) > 0
                     and instant(receipt["since"]) == instant(window[0])
                     and instant(receipt["until"]) == instant(window[1]))
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
        if valid:
            matched.append(call["tool_call_id"])
    return {"current_entry_used": bool(matched), "matching_call_ids": matched,
            "first_business_action": business[0]["args"] if business else None,
            "old_entry_attempted": any(c["args"].get("entry") == OLD_ENTRY for c in business),
            "memory_causality": "未自动判定；需结合启动记忆、完整回答与工具顺序阅读"}
