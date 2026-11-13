"""Adapter to the pinned Hermes agent loop and SessionDB."""

from contextlib import contextmanager
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import getproxies_environment
import uuid

from environment import DIAGNOSTIC_V1, DIAGNOSTIC_V2


ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(os.environ.get("HERMES_SRC", ROOT / ".deps" / "hermes-agent")).expanduser().resolve()
LESSON19 = Path(__file__).resolve().parents[1] / "integrations/dataset"
LANGFUSE_PLUGIN = "observability/langfuse"
ONCALL_SKILL = "oncall-alert-triage"
PATROL_SKILL = "service-patrol"
FOLLOWUP_PREFIX = "ONCALL_FOLLOWUP "
TOOL_DESCRIPTIONS = {
    "patrol_skill_view": "Read the approved service patrol Skill before inspecting the service.",
    "skill_view": "Read the active On-call Skill after receiving an alert.",
    "service_snapshot": "Read fresh HTTP metrics for the assigned service and its current time window.",
    "oncall_probe": "Make one HTTP request to the assigned lab service after an On-call steer.",
    "followup_probe": "Make one read-only HTTP probe of the assigned service in this investigation branch.",
    "search_inspections": "Find the latest patrol records for the same service, environment and region.",
    "memory_lookup": "Look up the assigned peer incident's historical fact, with its source Session and Item.",
    "diagnostic_lookup": "Send one read-only HTTP request to the assigned service's diagnostic route.",
    "recovery_windows": "Read the recorded recovery windows and the clearance time for this assigned incident.",
}


def ensure_loopback_no_proxy():
    effective = getproxies_environment().get("no", "")
    bypass = effective
    if effective != "*":
        exclusions = [item.strip() for item in effective.split(",") if item.strip()]
        bypass = ",".join(dict.fromkeys([*exclusions, "127.0.0.1", "localhost", "::1"]))
    os.environ.update(NO_PROXY=bypass, no_proxy=bypass)


def check_langfuse():
    """Check the explicit project and pinned SDK before starting an observed run."""
    for name, prefix in (("LANGFUSE_PUBLIC_KEY", "pk-lf-"), ("LANGFUSE_SECRET_KEY", "sk-lf-")):
        value = os.environ.get(name, "")
        if not value.startswith(prefix) or len(value) == len(prefix) or any(c.isspace() for c in value):
            raise RuntimeError(f"Set {name} to a valid Langfuse project key")
    url = os.environ.get("LANGFUSE_BASE_URL", "")
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
    except ValueError:
        raise RuntimeError("Set LANGFUSE_BASE_URL to an explicit HTTPS or loopback HTTP endpoint") from None
    if (not endpoint.hostname or endpoint.username is not None or endpoint.password is not None
            or endpoint.query or endpoint.fragment or port == 0 or any(c.isspace() for c in url)
            or not (endpoint.scheme == "https" or endpoint.scheme == "http"
                    and endpoint.hostname in {"localhost", "127.0.0.1", "::1"})):
        raise RuntimeError("Set LANGFUSE_BASE_URL to an explicit HTTPS or loopback HTTP endpoint")
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        override = "HERMES_" + name
        if os.environ.get(override, "").strip() not in ("", os.environ[name]):
            raise RuntimeError(f"{override} conflicts with {name}; configure a single project for collection")
    for name, disabled_value in (("LANGFUSE_TRACING_ENABLED", "false"), ("OTEL_SDK_DISABLED", "true")):
        if os.environ.get(name, "").lower() == disabled_value:
            raise RuntimeError(f"{name} disables tracing for this observed run")
    required = json.loads((LESSON19 / "dependencies.json").read_text())["langfuse"]
    setup = "Install langfuse==4.15.2 into the runtime Python environment"
    try:
        installed = version("langfuse")
    except PackageNotFoundError:
        sdk_python = LESSON19 / ".deps" / "sdk" / "bin" / "python"
        try:
            sdk_site = subprocess.check_output(
                [str(sdk_python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                text=True, timeout=10,
            ).strip()
            if sdk_site and Path(sdk_site).is_dir() and sdk_site not in sys.path:
                sys.path.insert(0, sdk_site)
            installed = version("langfuse")
        except (OSError, subprocess.SubprocessError, PackageNotFoundError):
            raise RuntimeError(setup) from None
    if installed != required:
        raise RuntimeError(f"Langfuse SDK {required} is required; found {installed}. {setup}")
    from langfuse._client.span import LangfuseChain

    if not callable(getattr(LangfuseChain, "set_trace_io", None)):
        raise RuntimeError("Langfuse SDK lacks the trace API required by the native plugin")


@contextmanager
def open_sessions(state, *, fresh=False, memory_text=None, langfuse=False):
    if not (SOURCE / "run_agent.py").is_file():
        raise RuntimeError("Install the pinned course dependencies with bash scripts/setup_deps.sh")
    if memory_text is not None and not fresh:
        raise ValueError("Seeded Memory requires a fresh Hermes Home")
    if langfuse:
        check_langfuse()
    home = state / "hermes"
    home.mkdir(exist_ok=not fresh)
    if memory_text is not None:
        memory = home / "memories" / "MEMORY.md"
        memory.parent.mkdir()
        memory.write_bytes(memory_text.encode("utf-8"))
    (home / "config.yaml").write_text(
        f"memory:\n  memory_enabled: {'true' if memory_text is not None else 'false'}\n"
        "  user_profile_enabled: false\n  nudge_interval: 0\n"
        "compression:\n  enabled: false\n"
        "skills:\n  creation_nudge_interval: 0\n"
        "tools:\n  tool_search:\n    enabled: false\n"
        + (f"plugins:\n  enabled:\n    - {LANGFUSE_PLUGIN}\n" if langfuse else ""),
        encoding="utf-8",
    )
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(SOURCE))
    import hermes_cli.env_loader
    hermes_cli.env_loader.load_hermes_dotenv = lambda **kwargs: []
    from hermes_state import SessionDB

    trace_options = {"HERMES_LANGFUSE_SAMPLE_RATE": "1.0", "HERMES_LANGFUSE_MAX_CHARS": "1000000"} if langfuse else {}
    previous = {name: os.environ.get(name) for name in trace_options}
    os.environ.update(trace_options)
    try:
        if langfuse:
            from hermes_cli.plugins import get_plugin_manager

            manager = get_plugin_manager()
            manager.discover_and_load()
            plugin = next((item for item in manager.list_plugins() if item["key"] == LANGFUSE_PLUGIN), {})
            if (not plugin.get("enabled") or plugin.get("error") or plugin.get("source") != "bundled"
                    or plugin.get("hooks") != 6):
                raise RuntimeError("Hermes did not register all six bundled Langfuse hooks")
        db = SessionDB(home / "state.db")
        try:
            yield db
        finally:
            db.close()
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _replace_skill_file(path, content):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def preserve_skill_version(state, content, *, name=ONCALL_SKILL):
    """Keep immutable content available to tasks pinned before the next release."""
    if name in ("", ".", "..") or Path(name).name != name:
        raise ValueError("Skill name must be a single directory component")
    digest = hashlib.sha256(content).hexdigest()
    snapshot = state / "hermes" / "skills" / name / "references" / "versions" / f"{digest}.md"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists():
        if snapshot.read_bytes() != content:
            raise RuntimeError("Saved Skill version does not match its content hash")
    else:
        _replace_skill_file(snapshot, content)
    return digest


def install_oncall_skill(state, source=None, *, name=ONCALL_SKILL):
    if name in ("", ".", "..") or Path(name).name != name:
        raise ValueError("Skill name must be a single directory component")
    source = source or Path(__file__).with_name("skills") / name / "SKILL.md"
    target = state / "hermes" / "skills" / name / "SKILL.md"
    content = source.read_bytes()
    digest = preserve_skill_version(state, content, name=name)
    if target.is_file():
        preserve_skill_version(state, target.read_bytes(), name=name)
    _replace_skill_file(target, content)
    return digest


def read_oncall_skill(expected_sha, *, name=ONCALL_SKILL):
    from tools.skills_tool import skill_view

    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("Expected Skill version must be a SHA-256 content hash")
    receipt = json.loads(skill_view(name, file_path=f"references/versions/{expected_sha}.md", preprocess=False))
    if not receipt.get("success"):
        # Older patrol states may have only the active file until next install.
        receipt = json.loads(skill_view(name, preprocess=False))
    if not receipt.get("success") or receipt.get("name") != name:
        raise RuntimeError("Hermes could not read the pinned On-call Skill")
    actual_sha = hashlib.sha256(receipt["content"].encode("utf-8")).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError("On-call Skill content does not match the pinned version")
    return {"success": True, "name": name, "sha256": actual_sha, "content": receipt["content"]}


def run_turn(db, session_id, prompt, tools, options, on_tool_complete=None, *,
             capture_session=False, load_memory=False, skill_name=ONCALL_SKILL, task_id=None, max_iterations=12):
    from hermes_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist
    from run_agent import AIAgent
    from tools.registry import registry

    def handle(name):
        def call(args, **_kwargs):
            error = {"error": "Unexpected tool parameters"}
            if name == "diagnostic_lookup":
                valid = (isinstance(args, dict) and args.keys() == {"path"}
                         and args["path"] in (DIAGNOSTIC_V1, DIAGNOSTIC_V2))
                result = tools[name](args["path"]) if valid else error
            else:
                expected = {"name": skill_name} if name == "skill_view" else {}
                result = tools[name]() if args == expected else error
            return json.dumps(result, ensure_ascii=False)
        return call

    for name in tools:
        if name == "skill_view":
            # Import the native module first, then scope the exposed tool to one Skill.
            from tools.skills_tool import SKILL_VIEW_SCHEMA
            schema = {**SKILL_VIEW_SCHEMA, "parameters": {
                "type": "object", "properties": {"name": {"type": "string", "enum": [skill_name]}},
                "required": ["name"], "additionalProperties": False,
            }}
        elif name == "diagnostic_lookup":
            schema = {"name": name, "description": TOOL_DESCRIPTIONS[name], "parameters": {
                "type": "object", "properties": {"path": {"type": "string", "enum": [DIAGNOSTIC_V1, DIAGNOSTIC_V2]}},
                "required": ["path"], "additionalProperties": False,
            }}
        else:
            schema = {"name": name, "description": TOOL_DESCRIPTIONS[name], "parameters": {
                "type": "object", "properties": {}, "additionalProperties": False,
            }}
        registry.register(
            name=name, toolset="course_patrol", handler=handle(name), check_fn=lambda: True,
            schema=schema, override=name == "skill_view",
        )
    # Give the model at most two earlier checks. Keep the complete audit trail
    # in SessionDB and let the agent query additional history through its tool.
    cutoff = db.latest_message_row_id(session_id, role="user", offset=1)
    history = db.get_messages_as_conversation(session_id, include_row_ids=True)
    history = [row for row in history if row.pop("_row_id", 0) >= (cutoff or 0)]
    prior = db.get_session(session_id)
    previous_count = prior["message_count"] if prior else 0
    def tool_complete(_call_id, name, _args, receipt):
        on_tool_complete(agent.steer, name, receipt)

    request_overrides = {"temperature": 0, "extra_body": {"thinking": {"type": "disabled"}}}
    if capture_session:
        request_overrides["extra_headers"] = {"X-Session-Id": session_id, "X-Turn-Type": "main"}
    agent = AIAgent(
        tool_complete_callback=tool_complete if on_tool_complete else None,
        **options, provider="custom", api_mode="chat_completions",
        session_id=session_id, session_db=db, enabled_toolsets=["course_patrol"],
        max_iterations=max_iterations, max_tokens=2048, quiet_mode=True, skip_context_files=True,
        ephemeral_system_prompt=("最终报告格式：仅返回任务要求的一个完整 JSON 对象。"
                                 "不要在 JSON 前后附加解释，不要使用 Markdown 代码围栏。"),
        load_soul_identity=False, skip_memory=not load_memory, save_trajectories=False,
        request_overrides=request_overrides,
    )
    agent.tools = [tool for tool in agent.tools if tool["function"]["name"] in tools]
    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
    try:
        if agent.valid_tool_names != set(tools):
            raise RuntimeError(f"Unexpected Hermes tool set: {agent.valid_tool_names}")
        set_thread_tool_whitelist(set(tools))
        result = agent.run_conversation(user_message=prompt, conversation_history=history, task_id=task_id)
        if not result.get("completed") or result.get("failed") or result.get("interrupted"):
            raise RuntimeError(f"Hermes did not complete this inspection: {result.get('error') or result.get('turn_exit_reason')}")
    finally:
        clear_thread_tool_whitelist()
        agent.close()
    return db.get_messages(session_id, offset=previous_count), result["messages"]


def checkpoint_rows(db, record):
    rows = [row for row in db.get_messages(record["session_id"])
            if row["id"] <= record["report_item_id"]]
    if not rows or rows[-1]["id"] != record["report_item_id"]:
        raise RuntimeError("The saved report item is missing from Hermes SessionDB")
    return rows


def fork(db, record):
    rows = checkpoint_rows(db, record)
    source_id = record["session_id"]
    source = db.get_session(source_id)
    child_id = "patrol-fork-" + uuid.uuid4().hex
    db.create_session(child_id, "cli", model=source["model"], system_prompt=source["system_prompt"],
                      parent_session_id=source_id)
    try:
        db.replace_messages(child_id, rows)
        copied = db.get_messages(child_id)
        if (len(copied) != len(rows)
                or db.get_session(child_id)["parent_session_id"] != source_id
                or any(any(a[key] != b[key] for key in ("role", "content", "tool_calls", "tool_call_id"))
                       for a, b in zip(rows, copied))):
            raise RuntimeError("Fork did not preserve the saved session prefix")
    except Exception:
        db.delete_session(child_id)
        raise
    return child_id, {a["id"]: b for a, b in zip(rows, copied)}
