"""Archive patrol sessions through the course-pinned SkillClaw Client Proxy."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sys
import time


EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


def _component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"Invalid SkillClaw path component: {value!r}")
    return value


def _group(root: Path, group_id: str | None) -> str:
    if group_id is None:
        group_id = json.loads((root / "run.json").read_text(encoding="utf-8"))["group_id"]
    return _component(group_id)


def _bound_port(proxy) -> int:
    # Uvicorn runs the lifespan startup hook before opening its listening socket.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and proxy._thread.is_alive():
        server = proxy._server
        if server.started:
            return server.servers[0].sockets[0].getsockname()[1]
        time.sleep(0.05)
    raise RuntimeError("SkillClaw Client Proxy did not open its listening socket")


@contextmanager
def capture(root: Path, alias: str, skills_dir: Path, upstream_url: str,
            api_key: str, model: str, *, group_id: str | None = None):
    """Run a loopback Client Proxy and drain its native archives at shutdown.

    Overlapping instances use nested contexts (for example, an ExitStack), so
    the course's process-wide tool-status hook is restored in reverse order.
    The proxy's skill directory is distinct from the Hermes task's fixed copy.
    """
    if str(EXAMPLES) not in sys.path:
        sys.path.insert(0, str(EXAMPLES))
    from skillclaw_lab.compat import structured_tool_status_compat
    from skillclaw.api_server import SkillClawAPIServer
    from skillclaw.config import SkillClawConfig

    record_dir = root / "proxy" / _component(alias)
    record_dir.mkdir(parents=True)
    proxy = SkillClawAPIServer(SkillClawConfig(
        proxy_host="127.0.0.1", proxy_port=0, proxy_api_key=api_key,
        claw_type="hermes", configure_openclaw=False, configure_hermes=False,
        llm_api_base=upstream_url, llm_api_key=api_key,
        llm_model_id=model, served_model_name=model,
        use_prm=False, use_skills=False, skills_dir=str(skills_dir),
        record_enabled=True, record_dir=str(record_dir),
        sharing_enabled=True, sharing_upload_sessions=True, sharing_backend="local",
        sharing_local_root=str(root / "shared"), sharing_group_id=_group(root, group_id),
        sharing_user_alias=alias, sharing_skill_reload_mode="off",
        validation_enabled=False, dashboard_enabled=False, max_context_tokens=64000,
    ))
    with structured_tool_status_compat() as compatibility:
        (record_dir / "compatibility.json").write_text(
            json.dumps(compatibility, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        proxy.start()
        try:
            yield f"http://127.0.0.1:{_bound_port(proxy)}/v1"
        finally:
            proxy.stop()
            proxy._thread.join(timeout=65)
            if proxy._thread.is_alive():
                raise RuntimeError(f"{alias}: SkillClaw Client Proxy did not finish draining")


def _report(content):
    if not isinstance(content, str):
        return {}
    text = content.strip()
    if text.startswith("```") and text.endswith("```") and "\n" in text:
        text = text.split("\n", 1)[1][:-3].strip()
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def incident_archive(root: Path, alias: str, session_id: str, *, group_id: str | None = None,
                     incident_id: str | None = None, skill_sha256: str | None = None) -> Path:
    """Locate a native archive and verify its provenance and incident receipts."""
    path = root / "shared" / _group(root, group_id) / "sessions" / f"{_component(session_id)}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    turns = record["turns"]
    if (record["session_id"] != session_id or record["user_alias"] != alias
            or not turns or record["num_turns"] != len(turns)
            or turns[-1].get("tool_calls") or not turns[-1].get("response_text", "").strip()):
        raise ValueError(f"{path}: incomplete archive or mismatched source")

    expected = {"service_snapshot", "skill_view", "oncall_probe"}
    receipts = defaultdict(lambda: defaultdict(list))
    reports = []
    for turn in turns:
        prompt = turn["prompt_text"]
        calls = {call["id"]: call["function"]["name"] for call in turn.get("tool_calls", [])}
        raw = turn.get("tool_results_raw", [])
        if len(raw) != len(calls) or {receipt["tool_call_id"]: receipt["name"] for receipt in raw} != calls:
            raise ValueError(f"{path}: tool calls and native receipts do not match")
        for receipt in raw:
            tool = receipt["name"]
            if tool not in expected:
                continue
            # The original metrics receipt may be followed by the native steer marker.
            result, _ = json.JSONDecoder().raw_decode(receipt["content"])
            if not isinstance(result, dict) or result.get("error"):
                continue
            receipts[prompt][tool].append(result)
        report = _report(turn.get("response_text"))
        if report.get("incident_id"):
            reports.append((prompt, report))

    for prompt, report in reports:
        evidence = receipts[prompt]
        if ((incident_id is not None and report["incident_id"] != incident_id)
                or not expected <= evidence.keys()):
            continue
        statuses = [probe.get("http_status") for probe in evidence["oncall_probe"]]
        if (any(type(status) is not int for status in statuses)
                or report.get("probe_http_status") != statuses[0]
                or report.get("probe_http_statuses", statuses) != statuses):
            continue
        for skill in evidence["skill_view"]:
            body = skill.get("content")
            if not isinstance(body, str):
                continue
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if digest == skill.get("sha256") and (skill_sha256 is None or digest == skill_sha256):
                return path
    raise ValueError(f"{path}: incident report or required native receipts are missing")
