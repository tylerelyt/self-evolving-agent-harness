"""使用已安装 Hermes 验证配置和工具分派；禁止网络，不调用模型。"""
import json
import os
from pathlib import Path
import sys
import tempfile

from run_background_review import HERE, ROOT, lesson_config
from scene import CURRENT_ENTRY, FIRST_WINDOW, SCHEMA, query


def main():
    src = Path(os.environ.get("HERMES_SRC", ROOT / ".deps/hermes-agent")).resolve()
    if (src / ".env").exists():
        raise RuntimeError("依赖目录存在 .env，离线检查不读取它")
    with tempfile.TemporaryDirectory(prefix="offline-native-", dir=HERE) as directory:
        home = Path(directory)
        os.environ["HERMES_HOME"] = str(home)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        (home / "config.yaml").write_text(json.dumps(lesson_config(2)))

        def guard(event, args):
            if event in ("socket.connect", "socket.bind"):
                raise RuntimeError("离线核验禁止网络")
            if event == "open" and isinstance(args[0], (str, bytes)):
                path = Path(os.fsdecode(args[0])).resolve()
                if path.name in (".env", "auth.json", "credentials.json") and not path.is_relative_to(home):
                    raise RuntimeError("离线核验禁止读取个人凭据")
                mode, flags = args[1], args[2] or 0
                writing = ((isinstance(mode, str) and any(c in mode for c in "wax+"))
                           or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
                if writing and not path.is_relative_to(home) and str(path) != "/dev/null":
                    raise RuntimeError("离线核验禁止写入临时 Home 以外")
        sys.addaudithook(guard)
        sys.path.insert(0, str(src))
        from tools.registry import registry
        registry.register(name="lesson11_logs", toolset="lesson11", schema=SCHEMA,
                          handler=lambda args, **kw: json.dumps(query(args)))
        from run_agent import AIAgent
        agent = AIAgent(model="glm-5.2", provider="glm", api_key="offline-placeholder-not-a-credential",
                        base_url="https://open.bigmodel.cn/api/paas/v4", enabled_toolsets=["lesson11", "memory"],
                        quiet_mode=True, skip_context_files=True, max_iterations=6)
        try:
            assert {"lesson11_logs", "memory"}.issubset(agent.valid_tool_names), agent.valid_tool_names
            assert agent._memory_nudge_interval == 2 and agent._skill_nudge_interval == 0
            from model_tools import handle_function_call
            receipt = json.loads(handle_function_call("lesson11_logs", {"action": "read", "entry": CURRENT_ENTRY,
                                                                       "since": FIRST_WINDOW[0], "until": FIRST_WINDOW[1]}))
            assert receipt["count"] == 2, receipt
        finally:
            agent.close()
    print("原生初始化、nudge配置、工具定义和实际分派通过；未调用模型、未联网。")


if __name__ == "__main__":
    main()
