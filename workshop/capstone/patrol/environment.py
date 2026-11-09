#!/usr/bin/env python3
"""Start four loopback HTTP services and generate real traffic for the patrol."""

import argparse
from collections import deque
from contextlib import ExitStack, closing
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import random
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid


SERVICES = ("orders", "payments", "inventory", "notifications")
TRACE_DEPENDENCIES = {"payments": "inventory", "orders": "notifications"}
SHARED_ROOT_ROUTES = {"payments": "notifications", "orders": "notifications"}
TELEMETRY_SOURCE = "live_lab_http_telemetry"
DEFAULT_STATE = Path(__file__).resolve().parents[1] / "runtime" / "hermes-patrol"
WINDOW = 30
TELEMETRY_WINDOW = 240
MAX_TELEMETRY_ROWS = 2048
DIAGNOSTIC_V1 = "/diagnostics/v1"
DIAGNOSTIC_V2 = "/diagnostics/v2"
DIAGNOSTIC_SOURCE = "live_lab_http_diagnostics"


def write_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def classify(observation):
    if observation["requests"] < 10:
        return "insufficient"
    if observation["error_rate"] >= .05 or observation["p95_ms"] >= 500:
        return "abnormal"
    return "healthy"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def get_snapshot(state, service):
    address = load_json(state / "environment.json")[service]
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{address}/metrics", timeout=3) as response:
        result = json.load(response)
    if result["service"] != service:
        raise RuntimeError("Metrics response belongs to another service")
    return result


def get_telemetry(state, service):
    """Read one service's rolling spans and failure logs from the lab."""
    address = load_json(state / "environment.json")[service]
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{address}/telemetry", timeout=3) as response:
        result = json.load(response)
    if result["service"] != service or result["source"] != TELEMETRY_SOURCE:
        raise RuntimeError("Telemetry response belongs to another service")
    return result


def probe_service(state, service, *, environment_id=None):
    address = load_json(state / "environment.json")[service]
    opener = build_opener(ProxyHandler({}))
    try:
        request = Request(f"{address}/work", headers={"X-Lab-Probe": "1"})
        with opener.open(request, timeout=3) as response:
            status = response.status
            trace_id = response.headers.get("X-Lab-Trace-Id")
            observed_scope = json.load(response) if environment_id else None
    except HTTPError as exc:
        status = exc.code
        trace_id = exc.headers.get("X-Lab-Trace-Id") if exc.headers is not None else None
        try:
            observed_scope = json.load(exc) if environment_id else None
        finally:
            exc.close()
    receipt = {"service": service, "path": "/work", "http_status": status, "checked_at": time.time()}
    if environment_id:
        if (not isinstance(observed_scope, dict) or observed_scope.get("service") != service
                or observed_scope.get("environment_id") != environment_id):
            raise RuntimeError("Probe response belongs to a different HTTP environment")
        receipt["environment_id"] = environment_id
    if trace_id is not None:
        if not re.fullmatch("[0-9a-f]{32}", trace_id):
            raise ValueError("Probe response has an invalid trace ID")
        receipt["trace_id"] = trace_id
    return receipt


def probe_diagnostic(state, service, path, environment_id):
    """Read one scoped diagnostic route without following redirects or replacements."""
    if service not in SERVICES or path not in (DIAGNOSTIC_V1, DIAGNOSTIC_V2):
        raise ValueError("Unknown diagnostic service or path")
    address = urlsplit(load_json(state / "environment.json")[service])
    if (address.scheme != "http" or address.hostname != "127.0.0.1" or address.port is None
            or address.username is not None or address.path or address.query or address.fragment):
        raise RuntimeError("Diagnostic address must be the assigned loopback HTTP endpoint")

    with closing(HTTPConnection(address.hostname, address.port, timeout=3)) as connection:
        connection.request("GET", path)
        with connection.getresponse() as response:
            status = response.status
            expected_missing = 410 if path == DIAGNOSTIC_V1 else 404
            if status not in (200, expected_missing):
                raise RuntimeError(f"Unexpected diagnostic HTTP status {status}")
            body = json.load(response)

    expected = {
        "source": DIAGNOSTIC_SOURCE, "service": service, "environment_id": environment_id,
        "path": path, "active": status == 200, "replacement": DIAGNOSTIC_V2 if status == 410 else None,
    }
    if body != expected or type(body["active"]) is not bool:
        raise RuntimeError("Diagnostic response does not match its service, environment or route")
    return {**body, "http_status": status}


def migrate_diagnostics(state, service):
    """Switch the assigned service's diagnostic route with one exclusive marker."""
    if service not in SERVICES:
        raise ValueError("Unknown diagnostic service")
    load_json(state / "environment.json")[service]
    (state / f"{service}.diagnostics-v2").touch(exist_ok=False)


def trace_routes(trace_service, shared_root):
    if shared_root:
        if trace_service is not None:
            raise ValueError("The shared root and single traced service are separate lab profiles")
        return SHARED_ROOT_ROUTES
    return {trace_service: TRACE_DEPENDENCIES[trace_service]} if trace_service is not None else {}


class Service:
    def __init__(self, name, state, environment_id, trace_service=None, *, shared_root=False):
        routes = trace_routes(trace_service, shared_root)
        self.name = name
        self.state = state
        self.environment_id = environment_id
        self.samples = deque()
        self.lock = threading.Lock()
        self.traffic_counts = {}
        self.probe_counts = {}
        self.telemetry_enabled = bool(routes)
        self.traced_callers = frozenset(caller for caller, upstream in routes.items() if upstream == name)
        self.root_scoped = shared_root and bool(self.traced_callers)
        self.upstream = None
        self.logs = deque(maxlen=MAX_TELEMETRY_ROWS)
        self.traces = deque(maxlen=MAX_TELEMETRY_ROWS)

    def request(self, *, probe=False, caller=None, trace_id=None):
        started = time.monotonic()
        time.sleep(random.uniform(.008, .035))
        # The shared profile puts the switch on the root. The single-caller lab puts it on the frontend.
        fault_scope = self.name if self.root_scoped else (caller or self.name)
        try:
            fault = (self.state / f"{fault_scope}.fault").read_text()
        except FileNotFoundError:
            fault = None
        with self.lock:
            if fault == "intermittent":
                counts = self.probe_counts if probe else self.traffic_counts
                count = counts[fault_scope] = counts.get(fault_scope, 0) + 1
                status = 503 if count % 2 else 200
            else:
                self.probe_counts.pop(fault_scope, None)
                self.traffic_counts.pop(fault_scope, None)
                status = 503 if fault is not None else 200
            now = time.time()
            self.trim(now)
            self.samples.append((now, status, round((time.monotonic() - started) * 1000, 2)))
            if self.telemetry_enabled and status == 503:
                identifier = trace_id or uuid.uuid4().hex
                log = {
                    "id": f"log-{identifier}", "trace_id": identifier, "time": now,
                    "service": self.name, "status": status, "caller_service": caller,
                    "fault_mode": "intermittent" if fault == "intermittent" else "on",
                    "message": ("Dependency handler returned 503 under a caller-scoped lab fault"
                                if caller else "Service handler returned 503 under its direct lab fault"),
                }
                if self.root_scoped:
                    log.update(fault_scope=fault_scope, message="Service handler returned 503 under the shared root lab fault")
                self.logs.append(log)
        return status

    def request_upstream(self, *, probe=False):
        upstream, port = self.upstream
        trace_id = uuid.uuid4().hex
        started_at = time.time()
        started = time.monotonic()
        try:
            with closing(HTTPConnection("127.0.0.1", port, timeout=1)) as connection:
                connection.request("GET", "/work", headers={
                    "X-Lab-Caller": self.name, "X-Lab-Trace-Id": trace_id,
                    "X-Lab-Probe": "1" if probe else "0",
                })
                with connection.getresponse() as response:
                    status = response.status
                    body = json.load(response)
            upstream_ms = round((time.monotonic() - started) * 1000, 2)
            expected = {"service": upstream, "environment_id": self.environment_id,
                        "trace_id": trace_id, "ok": status == 200}
            if status not in (200, 503) or body != expected:
                raise ValueError("Unexpected dependency receipt")
        except (OSError, HTTPException, ValueError):
            # A transport failure has no verified downstream span to export.
            status, trace_id = 502, None
        with self.lock:
            now = time.time()
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            self.trim(now)
            self.samples.append((now, status, duration_ms))
            if trace_id is not None:
                self.traces.append({
                    "id": f"span-{trace_id}", "trace_id": trace_id, "started_at": started_at,
                    "time": now, "service": self.name, "status": status, "upstream_service": upstream,
                    "upstream_status": status, "duration_ms": duration_ms, "upstream_ms": upstream_ms,
                })
        return status, trace_id

    def telemetry(self):
        with self.lock:
            end = time.time()
            start = end - TELEMETRY_WINDOW
            return {
                "source": TELEMETRY_SOURCE, "service": self.name, "environment_id": self.environment_id,
                "window_start": start, "window_end": end,
                "upstream_service": self.upstream[0] if self.upstream else None,
                "logs": [item for item in self.logs if start <= item["time"] <= end],
                "traces": [item for item in self.traces if start <= item["started_at"] <= item["time"] <= end],
            }

    def trim(self, end):
        while self.samples and self.samples[0][0] < end - WINDOW:
            self.samples.popleft()

    def metrics(self):
        with self.lock:
            end = time.time()
            self.trim(end)
            samples = list(self.samples)
        count = len(samples)
        latencies = sorted(ms for _, _, ms in samples)
        return {
            "source": "live_lab_http_requests", "service": self.name,
            "environment_id": self.environment_id, "window_start": end - WINDOW, "window_end": end,
            "requests": count,
            "error_rate": sum(code >= 500 for _, code, _ in samples) / count if count else None,
            "p95_ms": latencies[math.ceil(.95 * count) - 1] if count else None,
        }

    def diagnostics(self, path):
        migrated = (self.state / f"{self.name}.diagnostics-v2").exists()
        status = 200
        if path == DIAGNOSTIC_V1 and migrated:
            status = 410
        elif path == DIAGNOSTIC_V2 and not migrated:
            status = 404
        return status, {
            "source": DIAGNOSTIC_SOURCE, "service": self.name, "environment_id": self.environment_id,
            "path": path, "active": status == 200, "replacement": DIAGNOSTIC_V2 if status == 410 else None,
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        service = self.server.service
        trace_id = None
        if self.path == "/metrics":
            status, body = 200, service.metrics()
        elif self.path == "/telemetry" and service.telemetry_enabled:
            status, body = 200, service.telemetry()
        elif self.path == "/work":
            probe = self.headers.get("X-Lab-Probe") == "1"
            caller = self.headers.get("X-Lab-Caller")
            if caller is not None:
                trace_id = self.headers.get("X-Lab-Trace-Id", "")
                if caller not in service.traced_callers or not re.fullmatch("[0-9a-f]{32}", trace_id):
                    self.send_error(400)
                    return
                status = service.request(probe=probe, caller=caller, trace_id=trace_id)
            elif service.upstream:
                status, trace_id = service.request_upstream(probe=probe)
            else:
                status = service.request(probe=probe)
            body = {"service": service.name, "environment_id": service.environment_id, "ok": status == 200}
            if trace_id is not None:
                body["trace_id"] = trace_id
        elif self.path in (DIAGNOSTIC_V1, DIAGNOSTIC_V2):
            status, body = service.diagnostics(self.path)
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if trace_id is not None:
            self.send_header("X-Lab-Trace-Id", trace_id)
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


def traffic(url, interval, stop):
    opener = build_opener(ProxyHandler({}))
    while not stop.is_set():
        try:
            with opener.open(f"{url}/work", timeout=2):
                pass
        except HTTPError as exc:
            exc.close()  # The service already recorded the failed request.
        except OSError as exc:
            print(f"traffic {url}: {exc}", flush=True)
        stop.wait(interval)


def serve(state, trace_service=None, *, shared_root=False):
    routes = trace_routes(trace_service, shared_root)
    with ExitStack() as stack:
        environment_id = uuid.uuid4().hex
        manifest = state / "environment.json"
        # Atomically claim this state directory before publishing endpoints.
        try:
            with manifest.open("x") as handle:
                handle.write("{}")
        except FileExistsError as exc:
            raise RuntimeError(f"{manifest} exists. Stop the environment or remove its stale manifest.") from exc
        stack.callback(manifest.unlink, missing_ok=True)
        stop = threading.Event()
        workers = []
        servers = {}
        endpoints = {}
        try:
            for name in SERVICES:
                server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
                server.service = Service(name, state, environment_id, trace_service, shared_root=shared_root)
                servers[name] = server
                endpoints[name] = f"http://127.0.0.1:{server.server_port}"
            for caller, upstream in routes.items():
                servers[caller].service.upstream = upstream, servers[upstream].server_port
            for server in servers.values():
                listener = threading.Thread(target=server.serve_forever, daemon=True)
                listener.start()
                stack.callback(listener.join)
                stack.callback(server.shutdown)
            for name, url in endpoints.items():
                worker = threading.Thread(
                    target=traffic, args=(url, 8 if name == "notifications" else .18, stop), daemon=True,
                )
                worker.start()
                workers.append(worker)
            write_json(manifest, endpoints)
            print(json.dumps(endpoints), flush=True)
            stop.wait()
        finally:
            stop.set()
            for worker in workers:
                worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    profile = commands.add_parser("serve").add_mutually_exclusive_group()
    profile.add_argument("--trace-service", choices=TRACE_DEPENDENCIES)
    profile.add_argument("--shared-root", action="store_true")
    fault = commands.add_parser("fault")
    fault.add_argument("service", choices=SERVICES)
    fault.add_argument("mode", choices=("on", "off", "intermittent"))
    commands.add_parser("metrics").add_argument("service", choices=SERVICES)
    args = parser.parse_args()
    state = args.state.resolve()
    if args.command == "serve":
        state.mkdir(parents=True, exist_ok=True)
        serve(state, args.trace_service, shared_root=args.shared_root)
    elif args.command == "metrics":
        print(json.dumps(get_snapshot(state, args.service)))
    else:
        load_json(state / "environment.json")[args.service]
        path = state / f"{args.service}.fault"
        if args.mode == "off":
            path.unlink(missing_ok=True)
        else:
            path.write_text("" if args.mode == "on" else "intermittent")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        raise SystemExit(str(exc))
