"""在 Hermes 解释器中执行各幕；退出时只回收本执行器启动的服务。"""
import contextlib
import io
import json
import signal
import sys
import lab
from common import scrub, record
from demo import Story


def terminate(signum, frame):
    raise SystemExit("执行器收到停止请求")


signal.signal(signal.SIGTERM, terminate)
story = None
try:
    for line in sys.stdin:
        request = json.loads(line)
        action = request["action"]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                if action == "prepare":
                    story = Story()
                    result = {"observations": str(story.out), "runtime": str(story.run)}
                elif action == "confirm":
                    result = story.confirm(request.get("digest"))
                elif action in ("investigate", "learning", "reuse", "failures", "regression", "finish"):
                    result = getattr(story, action)()
                else:
                    raise ValueError("未知分幕动作")
            response = {"ok": True, "output": output.getvalue(), "result": result}
        except Exception as error:
            response = {"ok": False, "output": output.getvalue(), "error": str(error)}
            if story:
                record(story.out/"failure.json", {"error": str(error), "source_hashes": story.version})
        print(json.dumps(scrub(response), ensure_ascii=False), flush=True)
        if not response["ok"] or action == "finish":
            break
finally:
    lab.cleanup_owned()
