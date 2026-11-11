#!/usr/bin/env python3
"""Start four loopback HTTP services and generate real traffic for the patrol."""

import argparse
from collections import deque
from contextlib import ExitStack, closing, contextmanager
import fcntl
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import random
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


SERVICES = ("orders", "payments", "inventory", "notifications")
TRACE_DEPENDENCIES = {"payments": "inventory", "orders": "notifications"}
SHARED_ROOT_ROUTES = {"payments": "notifications", "orders": "notifications"}
TELEMETRY_SOURCE = "live_lab_http_telemetry"
REQUEST_LOG_SOURCE = "live_lab_http_request_logs"
CHANGE_SOURCE = "lab-environment-change"
DEFAULT_STATE = Path(__file__).resolve().parents[1] / "runtime" / "hermes-patrol"
WINDOW = 30
TELEMETRY_WINDOW = 240
MAX_TELEMETRY_ROWS = 2048
DIAGNOSTIC_V1 = "/diagnostics/v1"
DIAGNOSTIC_V2 = "/diagnostics/v2"
DIAGNOSTIC_SOURCE = "live_lab_http_diagnostics"
ALERT_SOURCE = "lab-alert-monitor"
RESOLUTION_SOURCE = "lab-verified-incident-resolution"


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


def alert_event(observation, *, emitted_at=None):
    service = observation["service"]
    return {"incident_id": f"lab-{service}-{uuid.uuid4().hex[:12]}", "service": service,
            "environment_id": observation["environment_id"], "source": ALERT_SOURCE,
            "emitted_at": time.time() if emitted_at is None else emitted_at}


def publish_alert(state, alert):
    """Retain one immutable event per incident, plus the legacy latest-event file."""
    service = alert["service"]
    incident_id = alert["incident_id"]
    if (service not in SERVICES or not re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident_id)
            or alert["source"] != ALERT_SOURCE or not alert["environment_id"]
            or type(alert["emitted_at"]) not in (int, float)
            or not math.isfinite(alert["emitted_at"]) or alert["emitted_at"] <= 0):
        raise ValueError("Invalid lab monitoring event")
    directory = state / "alerts" / service
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{incident_id}.json"
    temporary = directory / f".{incident_id}.{uuid.uuid4().hex}"
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(alert, output)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if load_json(target) != alert:
                raise RuntimeError("Incident ID already belongs to a different monitoring event")
    finally:
        temporary.unlink(missing_ok=True)
    write_json(state / f"{service}.alert.json", alert)


def resolution_for_alert(state, alert):
    """Read a scoped recovery acknowledgement; incomplete receipts never hide alerts."""
    try:
        service, incident_id = alert["service"], alert["incident_id"]
        if service not in SERVICES or not re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident_id):
            return None
        result = load_json(state / "resolutions" / service / f"{incident_id}.json")
        if (result.get("schema") != "lab-incident-resolution-v1"
                or result.get("source") != RESOLUTION_SOURCE
                or any(result.get(key) != alert[key]
                       for key in ("incident_id", "service", "environment_id", "emitted_at"))):
            return None
        evidence = result["evidence"]
        if (not all(isinstance(evidence[key], str) and evidence[key] for key in
                    ("patrol_database", "session_database", "source_session_id", "investigation_session_id",
                     "page_receipt", "page_sha256"))
                or evidence["source_session_id"] == evidence["investigation_session_id"]
                or not all(type(evidence[key]) is int and evidence[key] > 0 for key in
                           ("source_report_item_id", "investigation_report_item_id", "recovered_inspection_id"))):
            return None
        windows = evidence["recovery_windows"]
        paged_at = evidence["paged_at"]
        if (len(windows) != 2 or type(paged_at) not in (int, float) or not math.isfinite(paged_at)
                or paged_at < alert["emitted_at"] or result["resolved_at"] < paged_at
                or windows[-1]["inspection_id"] != evidence["recovered_inspection_id"]):
            return None
        for window in windows:
            observation = window["observation"]
            if (observation["service"] != service or observation["environment_id"] != alert["environment_id"]
                    or observation["source"] != "live_lab_http_requests"
                    or classify(observation) != "healthy" or observation["error_rate"] != 0
                    or not paged_at < observation["window_start"] < observation["window_end"] <= result["resolved_at"]):
                return None
        if windows[0]["observation"]["window_end"] >= windows[1]["observation"]["window_start"]:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def acknowledge_resolution(state, alert, evidence):
    """Atomically retain a verified recovery without changing the monitoring event."""
    service, incident_id = alert["service"], alert["incident_id"]
    if service not in SERVICES or not re.fullmatch(rf"lab-{service}-[0-9a-f]{{12}}", incident_id):
        raise ValueError("Invalid monitoring scope for recovery acknowledgement")
    raw = state / "alerts" / service / f"{incident_id}.json"
    if not raw.is_file():
        raw = state / f"{service}.alert.json"
    if load_json(raw) != alert:
        raise ValueError("Recovery acknowledgement differs from the original monitoring event")
    target = state / "resolutions" / service / f"{incident_id}.json"
    with change_lock(state):
        if resolution_for_alert(state, alert) is not None:
            return target
        if target.exists():
            raise ValueError("Existing recovery acknowledgement is incomplete or belongs to another event")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = {"schema": "lab-incident-resolution-v1", "source": RESOLUTION_SOURCE,
                  **{key: alert[key] for key in ("incident_id", "service", "environment_id", "emitted_at")},
                  "resolved_at": time.time(), "evidence": evidence}
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
        try:
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        if resolution_for_alert(state, alert) is None:
            target.unlink()
            raise ValueError("Recovery acknowledgement is missing scoped recovery evidence")
    return target


def read_alert(state, service, *, seen_ids=None, environment_id=None):
    """Read the latest unresolved event, or the oldest unseen unresolved event."""
    events = {}
    for path in (state / "alerts" / service).glob("*.json"):
        alert = load_json(path)
        if alert["incident_id"] != path.stem or alert["service"] != service:
            raise RuntimeError("Stored monitoring event does not match its incident path")
        events[alert["incident_id"]] = alert
    try:
        legacy = load_json(state / f"{service}.alert.json")
    except FileNotFoundError:
        legacy = None
    if legacy is not None:
        incident_id = legacy["incident_id"]
        if incident_id in events and events[incident_id] != legacy:
            raise RuntimeError("Latest monitoring event differs from its immutable incident")
        events[incident_id] = legacy
    pending = [event for event in events.values()
               if event["service"] == service
               and (environment_id is None or event["environment_id"] == environment_id)
               and (seen_ids is None or event["incident_id"] not in seen_ids)
               and resolution_for_alert(state, event) is None]
    pending.sort(key=lambda event: (event["emitted_at"], event["incident_id"]))
    if not pending:
        return None
    return pending[-1] if seen_ids is None else pending[0]


@contextmanager
def change_lock(state, *, shared=False):
    """Serialize state changes across CLI, workbench and cleanup processes."""
    with (state / ".changes.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _append_change(state, event):
    # Callers hold change_lock so the marker and its before/after receipt agree.
    row = {"id": f"change-{uuid.uuid4().hex}", "source": CHANGE_SOURCE,
           "time": time.time(), **event}
    with (state / "changes.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def read_changes(state, *, environment_id=None, limit=100):
    """Return actual configuration changes, retaining each environment's scope."""
    if type(limit) is not int or not 1 <= limit <= MAX_TELEMETRY_ROWS:
        raise ValueError("Choose a positive change limit no larger than 2048")
    rows = deque(maxlen=limit)
    with change_lock(state, shared=True):
        try:
            with (state / "changes.jsonl").open(encoding="utf-8") as changes:
                for line in changes:
                    row = json.loads(line)
                    if environment_id is None or row["environment_id"] == environment_id:
                        rows.append(row)
        except FileNotFoundError:
            pass
    return list(rows)


def inject_fault(state, service, mode, *, cleanup=None, actor="lab-operator"):
    if service not in SERVICES or mode not in ("on", "off", "intermittent"):
        raise ValueError("Unknown lab service or fault mode")
    with change_lock(state):
        flag = state / f"{service}.fault"
        try:
            before = "intermittent" if flag.read_text(encoding="utf-8") == "intermittent" else "on"
        except FileNotFoundError:
            before = "off"
        if mode == "off":
            flag.unlink(missing_ok=True)
        else:
            content = "intermittent" if mode == "intermittent" else ""
            if cleanup is None:
                temporary = flag.with_name(f".{flag.name}.{uuid.uuid4().hex}")
                try:
                    temporary.write_text(content, encoding="utf-8")
                    temporary.replace(flag)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                with flag.open("x", encoding="utf-8") as fault:
                    cleanup.callback(inject_fault, state, service, "off", actor="lab-cleanup")
                    fault.write(content)
        # An off request with no marker changed nothing. A repeated on request
        # replaces the marker and can cause a new monitoring event, so record it.
        if mode == before == "off":
            return
        try:
            environment_id = load_json(state / "environment-scope.json")["environment_id"]
        except FileNotFoundError:
            environment_id = None  # Marker-only test/setup, not a running environment.
        title = {"on": "注入持续失败", "intermittent": "注入间歇失败", "off": "解除故障"}[mode]
        _append_change(state, {"type": "fault_changed", "environment_id": environment_id,
                               "service": service, "actor": actor, "before": before, "after": mode,
                               "summary": f"{service} · {title}"})


def service_address(state, service):
    address = urlsplit(load_json(state / "environment.json")[service])
    if (address.scheme != "http" or address.hostname != "127.0.0.1" or address.port in (None, 0)
            or address.username is not None or address.path or address.query or address.fragment):
        raise RuntimeError("Service address must be the assigned loopback HTTP endpoint")
    return address


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def open_service(state, service, path, *, headers=None):
    address = service_address(state, service)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        return opener.open(Request(address.geturl() + path, headers=headers or {}), timeout=3)
    except HTTPError as exc:
        if not 300 <= exc.code < 400:
            raise
        exc.close()
        raise RuntimeError("Lab service unexpectedly returned a redirect") from exc


def get_snapshot(state, service):
    with open_service(state, service, "/metrics") as response:
        result = json.load(response)
    if result["service"] != service:
        raise RuntimeError("Metrics response belongs to another service")
    return result


def get_telemetry(state, service):
    """Read one service's rolling spans and failure logs from the lab."""
    with open_service(state, service, "/telemetry") as response:
        result = json.load(response)
    if result["service"] != service or result["source"] != TELEMETRY_SOURCE:
        raise RuntimeError("Telemetry response belongs to another service")
    return result


def get_request_logs(state, service):
    """Read recent /work receipts without adding traffic or advancing probes."""
    with open_service(state, service, "/logs") as response:
        result = json.load(response)
    if result["service"] != service or result["source"] != REQUEST_LOG_SOURCE:
        raise RuntimeError("Request log response belongs to another service")
    return result


def probe_service(state, service, *, environment_id=None):
    try:
        with open_service(state, service, "/work", headers={"X-Lab-Probe": "1"}) as response:
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
    address = service_address(state, service)

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
        self.requests = deque(maxlen=MAX_TELEMETRY_ROWS)
        self.logs = deque(maxlen=MAX_TELEMETRY_ROWS)
        self.traces = deque(maxlen=MAX_TELEMETRY_ROWS)

    def record_request(self, now, status, duration_ms, *, probe, caller=None, trace_id=None, request_id=None):
        """Called under self.lock only when a /work handler has executed."""
        row = {"id": request_id or f"request-{uuid.uuid4().hex}", "time": now,
               "service": self.name, "environment_id": self.environment_id,
               "method": "GET", "path": "/work", "status": status,
               "duration_ms": duration_ms, "probe": probe}
        if caller is not None:
            row["caller_service"] = caller
        if trace_id is not None:
            row["trace_id"] = trace_id
        self.requests.append(row)

    def request(self, *, probe=False, caller=None, trace_id=None, request_id=None):
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
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            self.trim(now)
            self.samples.append((now, status, duration_ms))
            self.record_request(now, status, duration_ms, probe=probe, caller=caller,
                                trace_id=trace_id, request_id=request_id)
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

    def request_upstream(self, *, probe=False, request_id=None):
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
            self.record_request(now, status, duration_ms, probe=probe, trace_id=trace_id,
                                request_id=request_id)
            if trace_id is not None:
                self.traces.append({
                    "id": f"span-{trace_id}", "trace_id": trace_id, "started_at": started_at,
                    "time": now, "service": self.name, "status": status, "upstream_service": upstream,
                    "upstream_status": status, "duration_ms": duration_ms, "upstream_ms": upstream_ms,
                })
        return status, trace_id

    def request_log(self):
        with self.lock:
            end = time.time()
            start = end - TELEMETRY_WINDOW
            return {"source": REQUEST_LOG_SOURCE, "service": self.name,
                    "environment_id": self.environment_id, "window_start": start,
                    "window_end": end,
                    "entries": [dict(item) for item in self.requests if start <= item["time"] <= end]}

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
        request_id = None
        if self.path == "/metrics":
            status, body = 200, service.metrics()
        elif self.path == "/logs":
            status, body = 200, service.request_log()
        elif self.path == "/telemetry" and service.telemetry_enabled:
            status, body = 200, service.telemetry()
        elif self.path == "/work":
            request_id = f"request-{uuid.uuid4().hex}"
            probe = self.headers.get("X-Lab-Probe") == "1"
            caller = self.headers.get("X-Lab-Caller")
            if caller is not None:
                trace_id = self.headers.get("X-Lab-Trace-Id", "")
                if caller not in service.traced_callers or not re.fullmatch("[0-9a-f]{32}", trace_id):
                    self.send_error(400)
                    return
                status = service.request(probe=probe, caller=caller, trace_id=trace_id, request_id=request_id)
            elif service.upstream:
                status, trace_id = service.request_upstream(probe=probe, request_id=request_id)
            else:
                status = service.request(probe=probe, request_id=request_id)
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
        if request_id is not None:
            self.send_header("X-Lab-Request-Id", request_id)
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


def monitor_alerts(state, stop):
    """Simulate the external log monitor with one event per injected fault."""
    emitted = {}
    pending = {}
    while not stop.wait(.25):
        for service in SERVICES:
            flag = state / f"{service}.fault"
            try:
                marker = flag.stat()
            except FileNotFoundError:
                emitted.pop(service, None)
                pending.pop(service, None)
                continue
            key = marker.st_ino, marker.st_mtime_ns
            if emitted.get(service) == key:
                continue
            try:
                observation = get_snapshot(state, service)
                current = flag.stat()
                if observation["error_rate"] and (current.st_ino, current.st_mtime_ns) == key:
                    if service not in pending or pending[service][0] != key:
                        pending[service] = key, alert_event(observation)
                    publish_alert(state, pending[service][1])
                    emitted[service] = key
                    pending.pop(service, None)
            except (OSError, RuntimeError, ValueError, KeyError):
                # A fault can disappear between the metric read and delivery.
                continue


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
            with change_lock(state):
                write_json(state / "environment-scope.json", {"environment_id": environment_id})
                write_json(manifest, endpoints)
                _append_change(state, {"type": "environment_started", "environment_id": environment_id,
                                       "service": None, "actor": "lab-environment", "before": None,
                                       "after": "running", "summary": "四个本地 HTTP 服务已启动并接收请求"})
            monitor = threading.Thread(target=monitor_alerts, args=(state, stop), daemon=True)
            monitor.start()
            workers.append(monitor)
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
        inject_fault(state, args.service, args.mode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        raise SystemExit(str(exc))
