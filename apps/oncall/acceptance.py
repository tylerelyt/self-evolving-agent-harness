"""Bounded real-model acceptance with a request/usage ledger and no key logging."""

import argparse
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .paths import prepare
prepare()
from self_evolving_harness.http_server import LoopbackHTTPServer


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@contextmanager
def budget_proxy(endpoint, key, model, record, *, max_requests=160, max_seconds=900):
    """Proxy one fixed model endpoint; reserve request budget before dispatch."""
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Acceptance requires an explicit credential-free HTTPS endpoint")
    token, lock = secrets.token_urlsafe(32), threading.Lock()
    started = time.monotonic()
    rows = []
    record = Path(record)

    def save():
        temporary = record.with_suffix(".tmp")
        temporary.write_text(json.dumps({"model": model, "endpoint": endpoint,
            "max_requests": max_requests, "max_seconds": max_seconds, "requests": rows}, indent=2))
        temporary.replace(record)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.reply(401, b'{"error":{"message":"Unauthorized local transport"}}')
                return
            if self.path not in ("/chat/completions", "/v1/chat/completions"):
                self.reply(404, b'{}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4 * 1024 * 1024:
                    raise ValueError("Request body size outside acceptance limit")
                payload = json.loads(self.rfile.read(size))
                if payload.get("model") != model:
                    raise ValueError("Use the fixed acceptance model")
            except (ValueError, TypeError):
                self.reply(400, b'{"error":{"message":"Invalid acceptance request"}}')
                return
            with lock:
                if len(rows) >= max_requests or time.monotonic() - started >= max_seconds:
                    self.reply(429, b'{"error":{"message":"Acceptance budget exhausted"}}')
                    return
                row = {"index": len(rows) + 1, "started_at": time.time(), "status": "requested"}
                rows.append(row)
                save()
            request = Request(endpoint.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            try:
                opener = build_opener(ProxyHandler({}), NoRedirect())
                try:
                    with opener.open(request, timeout=120) as response:
                        code, body = response.status, response.read(8 * 1024 * 1024)
                        content_type = response.headers.get("Content-Type", "application/json")
                except HTTPError as error:
                    code, body = error.code, error.read(1024 * 1024)
                    content_type = "application/json"
                if "text/event-stream" in content_type:
                    chunks = [json.loads(line[5:]) for line in body.decode().splitlines()
                              if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")]
                    usage = next((chunk["usage"] for chunk in reversed(chunks) if chunk.get("usage")), None)
                else:
                    usage = json.loads(body).get("usage")
                with lock:
                    row.update(status="completed" if code == 200 else "failed", http_status=code,
                               finished_at=time.time(), usage=usage)
                    save()
                self.reply(code, body, content_type)
            except Exception as error:
                with lock:
                    row.update(status="transport_error", error_type=type(error).__name__, finished_at=time.time())
                    save()
                self.reply(502, b'{"error":{"message":"Acceptance transport failed"}}')

    server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", token
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        with lock:
            save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=160)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--patrol-interval", type=float, default=20,
                        help="Seconds between recurring samples during bounded acceptance")
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests <= 1000 or not 30 <= args.max_seconds <= 3600:
        parser.error("Use 1–1000 requests and 30–3600 seconds")
    if not math.isfinite(args.patrol_interval) or not 1 <= args.patrol_interval <= 60:
        parser.error("Use a patrol interval of 1–60 seconds")
    prepare()
    from workbench_config import load_model_environment
    from environment import load_json, write_json
    info = load_model_environment(args.env_file)
    if not info["configured"]:
        parser.error("Model credentials are not configured")
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    # Patrols keep running while background steps execute. Slow the recurring
    # sampling so the finite acceptance budget is spent on the whole lifecycle.
    implementation = root / "implementation.py"
    implementation.write_text('from apps.oncall.workflow import *\n'
        'def patrol_config():\n'
        f'    return {{"services":["orders","payments"],"sample":1,"interval":{args.patrol_interval!r}}}\n')
    result = {"mode": "real-model", "provider": info["provider"], "model": info["model"],
              "patrol_interval": args.patrol_interval, "status": "running", "started_at": time.time()}
    write_json(root / "acceptance.json", result)
    with budget_proxy(os.environ["GLM_BASE_URL"], os.environ["GLM_API_KEY"], info["model"],
                      root / "usage.json", max_requests=args.max_requests, max_seconds=args.max_seconds) as (url, token):
        env = {**os.environ, "ONCALL_PROVIDER": "glm", "GLM_BASE_URL": url, "GLM_API_KEY": token}
        with (root / "runtime.log").open("w") as output:
            process = subprocess.Popen([sys.executable, "-B", "-m", "apps.oncall", "demo", "--live",
                "--out", str(root / "run"), "--implementation", str(implementation)],
                cwd=Path(__file__).resolve().parents[2], env=env, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=args.max_seconds)
                result.update(status="completed" if code == 0 else "failed", exit_code=code)
            except subprocess.TimeoutExpired:
                result.update(status="budget_timeout")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                result["finished_at"] = time.time()
                if (root / "run/report.json").exists():
                    report = load_json(root / "run/report.json")
                    result["workflow_status"] = report.get("status")
                    if report.get("status") != "complete":
                        result["status"] = "failed"
                write_json(root / "acceptance.json", result)
    print(json.dumps(result))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
