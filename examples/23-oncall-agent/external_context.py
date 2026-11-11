"""Lesson 23 OpenViking runbook acquisition and immutable evaluation context."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import urlsplit
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNBOOK = HERE / "context/payment-runbook.md"
SCHEMA = "lesson23-external-context-v1"
SCOPE = {"service": "payment-api", "environment": "lab"}
MARKER = "lesson23-timeout-context-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def within_namespace(uri, namespace):
    return isinstance(uri, str) and (uri == namespace or uri.startswith(namespace.rstrip("/") + "/"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def client(endpoint, account, user, api_key=None):
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.path not in {"", "/"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Lesson 23 requires a loopback OpenViking service root")
    for identity in (account, user):
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise ValueError("OpenViking identities must contain only letters, digits, underscores and hyphens")
    source = Path(os.environ.get("HERMES_SRC", ROOT / ".deps/hermes-agent")).resolve()
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    expected = (ROOT / "scripts/hermes-revision.txt").read_text().strip()
    if actual != expected:
        raise RuntimeError("OpenViking client requires the pinned course Hermes checkout")
    sys.path.insert(0, str(source))
    from plugins.memory.openviking import _VikingClient
    return _VikingClient(endpoint.rstrip("/"), api_key or "", account=account, user=user, agent="lesson23-context")


def unwrapped(response):
    if not isinstance(response, dict) or response.get("status") == "error":
        raise ValueError("OpenViking did not return a usable native response")
    return response.get("result", response)


def text_result(response):
    result = unwrapped(response)
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        text = result.get("content", result.get("text"))
        if isinstance(text, str):
            return text
    raise ValueError("OpenViking content response has no text")


def prepare_context(out, *, endpoint=None, account=None, user=None, api_key=None):
    """Ingest this lesson's runbook, retrieve it through OpenViking and freeze it once."""
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    endpoint = endpoint or os.environ.get("OPENVIKING_ENDPOINT", "http://127.0.0.1:1933")
    account = account or "lesson23"
    user = user or "run-" + uuid4().hex
    native = client(endpoint, account, user, api_key)
    namespace = "lesson23-" + uuid4().hex
    target = "viking://resources/" + namespace
    receipts = []

    def record(operation, arguments, response):
        item = {"id": "context-receipt-" + str(len(receipts) + 1), "operation": operation,
                "arguments": arguments, "response": response}
        receipts.append(item)
        save(out / "native-receipts.json", receipts)
        return item["id"]

    record("health", {}, native.get("/health"))
    uploaded = native.upload_temp_file(RUNBOOK)
    record("upload_resource", {"source": RUNBOOK.name, "source_sha256": content_hash(RUNBOOK.read_text())},
           {"temp_file_id": uploaded})
    arguments = {"temp_file_id": uploaded, "source_name": RUNBOOK.name, "to": target,
                 "wait": True, "timeout": 90}
    added = native.post("/api/v1/resources", arguments, timeout=100)
    record("add_resource", arguments, added)
    uri = unwrapped(added).get("root_uri")
    if not within_namespace(uri, target):
        raise ValueError("OpenViking ingested the resource outside the lesson's new namespace")
    query = {"query": MARKER + " payment-api timeout", "target_uri": uri, "limit": 5}
    found = native.post("/api/v1/search/find", query, timeout=90)
    search_receipt = record("find", query, found)
    results = unwrapped(found).get("resources", [])
    if not results or not all(within_namespace(item.get("uri"), uri) for item in results):
        raise ValueError("OpenViking search did not find the isolated lesson runbook")
    files = [item["uri"] for item in results if item.get("level") == 2 and item["uri"].endswith("/" + RUNBOOK.name)]
    if len(files) != 1:
        raise ValueError("OpenViking search must locate exactly the ingested lesson runbook file")
    resource_uri = files[0]
    response = native.get("/api/v1/content/read", params={"uri": resource_uri})
    content = text_result(response)
    read_receipt = record("read", {"uri": resource_uri}, response)
    if content.strip() != RUNBOOK.read_text().strip():
        raise ValueError("Native OpenViking read differs from the ingested lesson runbook")
    bundle = {"schema": SCHEMA, "provider": "openviking", "scope": dict(SCOPE),
              "endpoint": endpoint.rstrip("/"),
              "identity": {"account": account, "user": user}, "namespace_uri": uri,
              "entries": [{"uri": resource_uri, "scope": dict(SCOPE), "kind": "runbook",
                           "is_current_observation": False, "content": content,
                           "content_sha256": content_hash(content),
                           "receipts": [search_receipt, read_receipt]}],
              "native_receipts_sha256": digest(receipts)}
    bundle["context_sha256"] = digest(bundle)
    save(out / "external-context.json", bundle)
    return bundle


def load_frozen_context(path):
    path = Path(path)
    bundle = json.loads(path.read_text())
    fingerprint = bundle.get("context_sha256")
    if bundle.get("schema") != SCHEMA or fingerprint != digest({k: v for k, v in bundle.items() if k != "context_sha256"}):
        raise ValueError("Frozen OpenViking context fingerprint does not match")
    receipts = json.loads(path.with_name("native-receipts.json").read_text())
    if digest(receipts) != bundle["native_receipts_sha256"]:
        raise ValueError("Native retrieval evidence changed after context was frozen")
    read_context(bundle, SCOPE["service"], SCOPE["environment"])
    return bundle


def read_context(bundle, service, environment="lab"):
    """Read the exact frozen resource for both arms; no service write or current-state claim."""
    if bundle.get("schema") != SCHEMA or bundle.get("scope") != {"service": service, "environment": environment}:
        raise ValueError("Requested external context is outside this lesson's service/environment scope")
    if bundle.get("context_sha256") != digest({k: v for k, v in bundle.items() if k != "context_sha256"}):
        raise ValueError("Frozen context was changed")
    entries = bundle.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Frozen external context has no entries")
    for entry in entries:
        if (entry.get("scope") != bundle["scope"] or entry.get("kind") != "runbook"
                or entry.get("is_current_observation") is not False
                or not within_namespace(entry.get("uri"), bundle["namespace_uri"])
                or entry.get("content_sha256") != content_hash(entry.get("content", ""))):
            raise ValueError("Frozen resource content or scope does not match")
    return {"provider": "openviking", "scope": dict(bundle["scope"]), "context_sha256": bundle["context_sha256"],
            "entries": json.loads(json.dumps(entries))}


def live_read_context(bundle, service, environment="lab", *, endpoint=None, api_key=None):
    """Query and read the native service during an online task, preserving actual responses."""
    frozen = read_context(bundle, service, environment)
    native = client(endpoint or bundle["endpoint"], **bundle["identity"], api_key=api_key)
    query = {"query": MARKER + " " + service + " timeout", "target_uri": bundle["namespace_uri"], "limit": 5}
    found = native.post("/api/v1/search/find", query, timeout=90)
    hits = unwrapped(found).get("resources", [])
    if not hits or not all(within_namespace(hit.get("uri"), bundle["namespace_uri"]) for hit in hits):
        raise ValueError("Online OpenViking lookup did not remain within the pinned resource namespace")
    receipts = [{"operation": "find", "arguments": query, "response": found}]
    for entry in frozen["entries"]:
        response = native.get("/api/v1/content/read", params={"uri": entry["uri"]})
        content = text_result(response)
        if content_hash(content) != entry["content_sha256"]:
            raise ValueError("Online resource changed from this task's pinned external context")
        receipts.append({"operation": "read", "arguments": {"uri": entry["uri"]}, "response": response,
                         "content_sha256": content_hash(content)})
    return {**frozen, "retrieval": "native_openviking", "native_receipts": receipts,
            "native_receipts_sha256": digest(receipts)}


def service_python(python=None):
    selected = Path(python or os.environ.get("OPENVIKING_PYTHON", HERE / "runtime/openviking-deps/venv/bin/python")).expanduser().absolute()
    if not selected.is_file():
        raise RuntimeError("Run examples/23-oncall-agent/setup_openviking.py with the course Hermes Python, or set OPENVIKING_PYTHON to the pinned service interpreter")
    version = subprocess.check_output([str(selected), "-I", "-c",
        "import importlib.metadata; print(importlib.metadata.version('openviking'))"], text=True).strip()
    expected = (ROOT / "scripts/openviking-revision.txt").read_text().splitlines()[0]
    if version != expected:
        raise RuntimeError("OpenViking service package differs from the course pin")
    return selected


@contextmanager
def scripted_model():
    """Local deterministic embedding and summary endpoint for native service wiring."""
    counts = {"embedding_requests": 0, "summary_requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/embeddings":
                counts["embedding_requests"] += 1
                values = request["input"] if isinstance(request["input"], list) else [request["input"]]
                rows = []
                for index, value in enumerate(values):
                    vector = [0.0] * 32
                    for term in re.findall(r"[A-Za-z0-9_-]+", str(value).lower()):
                        slot = int(hashlib.sha256(term.encode()).hexdigest()[:8], 16) % len(vector)
                        vector[slot] += 1.0
                    length = math.sqrt(sum(number * number for number in vector)) or 1.0
                    rows.append({"object": "embedding", "index": index,
                                 "embedding": [number / length for number in vector]})
                payload = {"object": "list", "data": rows, "model": request.get("model"), "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            elif self.path == "/v1/chat/completions":
                counts["summary_requests"] += 1
                text = "lesson23-timeout-context-v1 payment-api lab runbook. Current state must be obtained from current tool receipts."
                payload = {"id": "lesson23-summary", "object": "chat.completion", "created": 0,
                           "model": request.get("model"), "choices": [{"index": 0, "finish_reason": "stop",
                           "message": {"role": "assistant", "content": text}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            else:
                self.send_error(404)
                return
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", counts
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def scripted_service(out, *, python=None):
    """Start real OpenViking in a new lesson-23 workspace, using only local model fixtures."""
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    python = service_python(python)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    with scripted_model() as (model, counts):
        config = {"server": {"host": "127.0.0.1", "port": port},
                  "storage": {"workspace": str(out / "data"), "vectordb": {"backend": "local"}, "agfs": {"backend": "local"}},
                  "vlm": {"provider": "openai", "model": "lesson23-summary", "api_base": model,
                          "api_key": "lesson23-local-model", "max_tokens": 1024, "max_concurrent": 1},
                  "embedding": {"dense": {"provider": "openai", "model": "lesson23-embedding", "api_base": model,
                                          "api_key": "lesson23-local-model", "dimension": 32, "encoding_format": "float"}},
                  "memory": {"extraction_enabled": False, "session_skill_extraction_enabled": False}}
        save(out / "ov.conf", config)
        endpoint = f"http://127.0.0.1:{port}"
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ") if key in os.environ}
        environment.update(OPENVIKING_CONFIG_FILE=str(out / "ov.conf"), NO_PROXY="localhost,127.0.0.1,::1",
                           no_proxy="localhost,127.0.0.1,::1", PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1")
        with (out / "server.log").open("w") as log:
            process = subprocess.Popen([str(python), "-B", "-c",
                "from openviking_cli.server_bootstrap import main; main()", "--config", str(out / "ov.conf")],
                cwd=out, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                native = client(endpoint, "lesson23", "health-" + uuid4().hex)
                deadline = time.monotonic() + 60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Native OpenViking exited; inspect {out / 'server.log'}")
                    try:
                        native.get("/health", timeout=1)
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"Native OpenViking health timed out; inspect {out / 'server.log'}")
                        time.sleep(.2)
                yield endpoint
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                save(out / "service.json", {"native_service": "openviking",
                    "pinned_version": (ROOT / "scripts/openviking-revision.txt").read_text().splitlines()[0],
                    "endpoint": endpoint, "workspace": str(out / "data"), "model_backend": "local-scripted-fixture",
                    "fixture_endpoint": model, "glm_requests": 0,
                    "fixture_requests": counts, "returncode": process.returncode})
