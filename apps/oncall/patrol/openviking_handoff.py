"""Publish scoped exercise evidence and recall it through native Hermes OpenViking tools."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
import re
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from environment import SERVICES
from hermes_runtime import SOURCE, ensure_loopback_no_proxy
from shared_facts import validate_fact, validate_scope


PEERS = {"instance-a": "instance-b", "instance-b": "instance-a"}


def settings():
    endpoint = os.environ.get("OPENVIKING_ENDPOINT", "").rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or parsed.port == 0
            or any(char.isspace() for char in endpoint)):
        raise ValueError("Set OPENVIKING_ENDPOINT to the lesson's explicit loopback HTTP root")
    identity = {
        "account": os.environ.get("OPENVIKING_ACCOUNT", "default"),
        "user": os.environ.get("OPENVIKING_USER", "default"),
    }
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in identity.values()):
        raise ValueError("Use alphanumeric OpenViking account and user identifiers")
    return {"endpoint": endpoint, **identity}


def native_module():
    if not (SOURCE / "plugins/memory/openviking/__init__.py").is_file():
        raise RuntimeError("Install the pinned course Hermes dependencies")
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from plugins.memory import openviking

    return openviking


def client(identity, actor):
    ensure_loopback_no_proxy()
    return native_module()._VikingClient(
        **identity, api_key=os.environ.get("OPENVIKING_API_KEY", ""), agent=actor,
    )


def check_openviking():
    identity = settings()
    if not client(identity, "capstone-preflight").health():
        raise RuntimeError("Start the lesson's OpenViking server before the fleet")
    return identity


def namespace_uri(namespace):
    if not re.fullmatch(r"capstone-[0-9a-f]{32}", namespace):
        raise ValueError("Invalid per-run OpenViking namespace")
    return f"viking://resources/{namespace}"


def runbook_uri(namespace, service):
    if service not in SERVICES:
        raise ValueError("Unknown runbook service")
    return f"{namespace_uri(namespace)}/runbook-{service}.md"


def fact_uri(namespace, incident_id, environment_id):
    service = incident_id.split("-")[1] if isinstance(incident_id, str) and "-" in incident_id else ""
    validate_scope(incident_id, service, environment_id)
    return f"{namespace_uri(namespace)}/fact-{incident_id}-{environment_id}.md"


def fact_body(fact):
    return json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def runbook_body(service, publisher):
    return (
        f"# {service} 值班交接手册\n\n发布实例：{publisher}\n\n"
        "先核对事故范围、历史事实来源的 Session 与报告 Item，再查询同一环境的当前 HTTP 窗口。"
        "请求数少于 10 时记为采样不足，5xx 比例达到 5% 或 P95 达到 500 ms 时记为异常。"
        "一次探测只说明该请求的结果。处置后需要两个独立的健康窗口才能确认恢复。"
    )


def digest(body):
    return sha256(body.encode("utf-8")).hexdigest()


def publish(namespace, uri, body, publisher):
    if publisher not in PEERS:
        raise ValueError("Unknown OpenViking publisher")
    root = namespace_uri(namespace)
    remote = client(check_openviking(), publisher)
    conflict = native_module()._OpenVikingHTTPError
    try:
        remote.post("/api/v1/fs/mkdir", {"uri": root})
    except conflict as error:
        if error.status_code != 409:
            raise
    try:
        written = remote.post("/api/v1/content/batch-write", {
            "root_uri": root,
            "operations": [{"uri": uri, "content": body, "precondition": {"kind": "create_if_absent"}}],
            "wait": True,
            "timeout": 90,
        }, timeout=100)["result"]
        if (written["root_uri"] != root or written["updated"]
                or uri not in written["created"] + written["unchanged"]
                or any(queue["error_count"] for queue in written["queue_status"].values())):
            raise RuntimeError("OpenViking did not confirm the scoped indexed publication")
    except conflict as error:
        if error.status_code != 409:
            raise
    saved = remote.get("/api/v1/content/read", params={"uri": uri, "raw": True}).get("result")
    if saved != body:
        raise ValueError("OpenViking resource bytes differ from the approved publication")
    return {"uri": uri, "sha256": digest(body), "publisher": publisher}


def publish_runbook(namespace, service, publisher):
    return publish(namespace, runbook_uri(namespace, service), runbook_body(service, publisher), publisher)


def publish_fact(namespace, fact):
    validate_fact(fact)
    return publish(namespace, fact_uri(namespace, fact["incident_id"], fact["environment_id"]),
                   fact_body(fact), fact["instance"])


@contextmanager
def read_only_manager(receiver, session_id):
    identity = check_openviking()
    native = native_module()
    from agent.memory_manager import MemoryManager

    env = {
        "OPENVIKING_ENDPOINT": identity["endpoint"],
        "OPENVIKING_ACCOUNT": identity["account"],
        "OPENVIKING_USER": identity["user"],
        "OPENVIKING_AGENT": receiver,
    }
    manager = MemoryManager()
    provider = native.OpenVikingMemoryProvider()
    manager.add_provider(provider)
    # A fresh provider directory cannot recover or commit a prior session.
    with (TemporaryDirectory(prefix="capstone-viking-reader-") as home, patch.dict(os.environ, env),
          patch.object(native, "_load_hermes_openviking_config", return_value={}),
          patch.object(native, "_start_local_openviking_server", return_value=("failed", "Start OpenViking explicitly"))):
        try:
            manager.initialize_all(session_id=session_id, hermes_home=home, platform="capstone")
            if provider._client is None:
                raise RuntimeError("Hermes could not connect its native OpenViking MemoryProvider")
            yield manager
        finally:
            manager.shutdown_all()


def native_call(manager, name, args):
    value = json.loads(manager.handle_tool_call(name, args))
    if not isinstance(value, dict) or "error" in value:
        raise RuntimeError(f"Native OpenViking {name} failed for the assigned resource")
    return value


def search_read(manager, root, query, expected_uri):
    result = native_call(manager, "viking_search", {"query": query, "mode": "fast", "scope": root, "limit": 12})
    if not any(hit.get("type") == "resource" and hit.get("uri") == expected_uri for hit in result["results"]):
        return None
    record = native_call(manager, "viking_read", {"uri": expected_uri, "level": "full"})
    if (record.get("uri"), record.get("resolved_uri"), record.get("level")) != (expected_uri, expected_uri, "full"):
        raise ValueError("OpenViking read returned a different resource")
    return record["content"]


def make_openviking_lookup(namespace, incident_id, service, environment_id, *, receiver, session_id):
    validate_scope(incident_id, service, environment_id)
    if receiver not in PEERS or not incident_id.startswith(f"lab-{service}-"):
        raise ValueError("Invalid OpenViking handoff scope")
    root = namespace_uri(namespace)
    historical_uri = fact_uri(namespace, incident_id, environment_id)
    guide_uri = runbook_uri(namespace, service)
    publisher = PEERS[receiver]

    def lookup():
        with read_only_manager(receiver, session_id) as manager:
            body = search_read(manager, root, f"{incident_id} {environment_id} {service} 历史事故", historical_uri)
            guide = search_read(manager, root, f"{service} 值班交接手册 runbook", guide_uri)
        if guide != runbook_body(service, publisher):
            raise RuntimeError("OpenViking did not return the published peer runbook")
        fact = None
        if body is not None:
            fact = json.loads(body)
            validate_fact(fact)
            if (body != fact_body(fact) or (fact["incident_id"], fact["service"], fact["environment_id"], fact["instance"])
                    != (incident_id, service, environment_id, publisher)):
                raise ValueError("OpenViking historical fact differs from the requested peer scope")
        return {
            "historical_only": True, "backend": "openviking", "fact": fact,
            "fact_uri": historical_uri if fact is not None else None,
            "fact_sha256": digest(body) if fact is not None else None,
            "runbook_uri": guide_uri, "runbook_sha256": digest(guide), "runbook": guide,
        }

    return lookup
