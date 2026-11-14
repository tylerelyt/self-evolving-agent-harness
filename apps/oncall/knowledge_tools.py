"""Model-facing historical retrieval with Harness-bound scope."""

import os

MARKER = "ONCALL_KNOWLEDGE_ENABLED"


def enabled():
    return bool(os.environ.get("ONCALL_MEMORY_CONFIG", "").strip())


def lookup(*, root, receiver, service, environment_id, session_id):
    """A failed provider stays explicit; the caller must still inspect reality."""
    if not environment_id:
        return {"backend": "openviking", "historical_only": True, "success": False,
                "error": "Read current service metrics before querying historical knowledge"}
    try:
        from apps.oncall.memory import configured_lookup
        reader = configured_lookup(root=root, receiver=receiver, service=service,
                                   environment_id=environment_id, session_id=session_id)
        if reader is None:
            raise RuntimeError("Configured knowledge provider is unavailable")
        result = reader()
        # Historical records can legitimately contain TimeoutError or other
        # failure text. Classify this read, not the incident being recalled.
        return {**result, "success": "error" not in result}
    except Exception as error:
        # Configuration/provider exceptions may contain private HTTP details.
        return {"backend": "openviking", "historical_only": True, "success": False,
                "error": "Knowledge lookup unavailable", "error_type": type(error).__name__}


def instruction():
    return (MARKER + "：按需用 knowledge_lookup 检索本轮服务与环境的历史事实和手册。"
            "先读取本轮现场指标或执行新 HTTP 探测再查记忆；检索结果仅为历史参考，必须保留原始来源，"
            "不能替代当前 HTTP 证据，也不能据此断言当前健康或确认根因。"
            "工具不可用时明确保留证据缺口，并继续现场巡检。")
