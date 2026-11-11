"""A local scripted model; Hermes still performs actual tools and persistence."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
from uuid import uuid4

from scenario import CONTEXT_TOOL, SKILL, report_for


def respond(messages):
    start = next(index for index in range(len(messages) - 1, -1, -1)
                 if messages[index].get("role") == "user" and str(messages[index].get("content", "")).startswith("LESSON23_TASK "))
    task = json.loads(messages[start]["content"].split(" ", 1)[1])
    names, receipts = {}, {}
    for message in messages[start + 1:]:
        for call in message.get("tool_calls") or []:
            names[call["id"]] = call["function"]["name"]
        if message.get("role") == "tool" and message.get("tool_call_id") in names:
            receipts[names[message["tool_call_id"]]] = json.loads(message["content"])
    if "skill_view" not in receipts:
        return "skill_view", {"name": SKILL}
    if task.get("context_required") and CONTEXT_TOOL not in receipts:
        return CONTEXT_TOOL, task["scope"]
    for tool in ("query_service_health", "query_upstream_latency"):
        if tool not in receipts:
            return tool, task["scope"]
    skill = receipts["skill_view"]
    text = skill.get("raw_content", skill.get("content", ""))
    compare = task["extra_investigation"] or all(tool in text for tool in ("query_pool_config", "query_pool_effective"))
    if receipts["query_service_health"]["healthy"] and receipts["query_upstream_latency"]["p95_ms"] <= 1000 and compare:
        for tool in ("query_pool_config", "query_pool_effective"):
            if tool not in receipts:
                return tool, task["scope"]
    return None, report_for({name: value for name, value in receipts.items() if name.startswith("query_")}, receipts.get(CONTEXT_TOOL))


@contextmanager
def connection():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                size = int(self.headers["Content-Length"])
                if not 0 < size <= 2_000_000:
                    raise ValueError("Invalid model request size")
                request = json.loads(self.rfile.read(size))
                name, result = respond(request["messages"])
                message = {"role": "assistant", "content": None}
                if name:
                    message["tool_calls"] = [{"id": "lesson23-" + uuid4().hex, "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(result)}}]
                else:
                    message["content"] = json.dumps(result, ensure_ascii=False)
                finish = "tool_calls" if name else "stop"
                common = {"id": "lesson23-script", "model": request["model"], "created": 0}
                if request.get("stream"):
                    if name:
                        message["tool_calls"][0]["index"] = 0
                    chunks = [{**common, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                              {**common, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
                    data = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                    kind = "text/event-stream"
                else:
                    data = json.dumps({**common, "object": "chat.completion", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}).encode()
                    kind = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (KeyError, TypeError, ValueError, StopIteration):
                self.send_error(400, "Invalid lesson-23 scripted request")

    names = ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL")
    previous = {name: os.environ.get(name) for name in names}
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1"
        os.environ.update(GLM_API_KEY="lesson23-local-script", GLM_BASE_URL=url, GLM_MODEL="lesson23-script")
        try:
            yield url
        finally:
            server.shutdown()
            thread.join()
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
