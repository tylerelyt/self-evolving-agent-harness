"""Run one isolated Hermes task, or resume its persisted Session checkpoint."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from skillclaw_lab.runtime import install_signal_handlers, readonly_skill_toolset, save, sha
from skillclaw_lab.observations import tool_events
from scenario import CONTEXT_TOOL, SKILL, TOOLS, prompt, snapshots, validate_scope, validate_task


def content_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_prompt(request, *, context_required):
    """Expose the task scope and instructions, keeping dataset labels in audit only."""
    return prompt(request["role"], request["case"], scope=validate_scope(request["scope"]),
                  context_required=context_required)


def main() -> None:
    directory = Path(sys.argv[1]).resolve()
    request = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    case, role = request["case"], request["role"]
    if case not in ("drift", "upstream") or role not in ("source", "receiver"):
        raise ValueError("Use a lesson 23 case and source or receiver role")
    scope = validate_scope(request["scope"])
    source = Path(request["skill"])
    if not source.is_absolute() or not source.is_file():
        raise ValueError("Use an existing absolute Skill file path")
    expected_sha = sha(source)
    home = directory / "home"
    resume = request.get("resume") is True
    checkpoint_file = directory / "checkpoint.json"
    if resume:
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        if not (home / "state.db").is_file():
            raise ValueError("Resume requires the copied native SessionDB")
        session_id = request.get("session_id", checkpoint["session_id"])
        if session_id != checkpoint["session_id"] or checkpoint["skill_sha256"] != expected_sha:
            raise ValueError("Resume must keep the checkpoint Session and pinned Skill")
    else:
        if home.exists() or checkpoint_file.exists():
            raise ValueError("A new task requires a fresh home; use resume for its checkpoint")
        home.mkdir()
        checkpoint = None
        session_id = request.get("session_id") or "lesson23-" + uuid.uuid4().hex
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Use a nonempty Session ID")

    os.environ.update(HERMES_HOME=str(home), XDG_CACHE_HOME=str(home / "cache"),
                      PYTHON_DOTENV_DISABLED="1")
    config = home / "config.yaml"
    if not resume:
        config.write_text("""tools:
  tool_search:
    enabled: false
memory:
  memory_enabled: false
  user_profile_enabled: false
skills:
  creation_nudge_interval: 0
agent:
  environment_probe: false
sessions:
  write_json_snapshots: false
curator:
  enabled: false
""", encoding="utf-8")
    skill = home / "skills" / SKILL / "SKILL.md"
    if not resume:
        skill.parent.mkdir(parents=True)
        shutil.copyfile(source, skill)
    if not skill.is_file() or sha(skill) != expected_sha:
        raise ValueError("The task's fixed Skill differs from the checkpoint version")

    from hermes_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.registry import registry

    snapshot = snapshots(case, request.get("snapshot"))
    if set(snapshot) != set(TOOLS):
        raise ValueError("The scenario snapshot must define exactly this lesson's tools")
    save(directory / "snapshot.json", snapshot)
    calls = []
    calls_lock = threading.Lock()
    context = None
    bundle = None
    context_mode = request.get("context_mode", "frozen")
    if context_mode not in {"live", "frozen"}:
        raise ValueError("Use live online retrieval or frozen evaluation context")
    if request.get("context_file"):
        from external_context import load_frozen_context
        context_file = Path(request["context_file"])
        bundle = load_frozen_context(context_file)

    def handler(name):
        def query(args, **_context):
            receipt = (dict(snapshot[name]) if args == scope
                       else {"error": "Query scope must match payment-api/lab/local"})
            with calls_lock:
                calls.append({"tool": name, "arguments": args, "result": receipt})
                save(directory / "calls.json", calls)
            return json.dumps(receipt, ensure_ascii=False)
        return query

    for name in TOOLS:
        registry.register(
            name=name, toolset="lesson23_snapshot", description="Read this lesson's frozen On-call snapshot",
            schema={"name": name, "description": "Read only; no service changes or production access.",
                    "parameters": {"type": "object", "properties": {
                        "service": {"type": "string"}, "environment": {"type": "string"},
                        "region": {"type": "string"}},
                        "required": ["service", "environment", "region"], "additionalProperties": False}},
            handler=handler(name), override=True,
        )
    if bundle is not None:
        def context_handler(args, **_context):
            nonlocal context
            if args != scope:
                return json.dumps({"error": "Context lookup scope mismatch"})
            from external_context import live_read_context, read_context
            returned = (live_read_context(bundle, scope["service"], scope["environment"])
                        if context_mode == "live" else read_context(bundle, scope["service"], scope["environment"]))
            context = {"backend": "openviking_live" if context_mode == "live" else "openviking_frozen_replay",
                       "scope": scope, "bundle_sha256": sha(context_file), "context": returned}
            save(directory / "context-receipt.json", context)
            return json.dumps(context, ensure_ascii=False)

        registry.register(
            name=CONTEXT_TOOL, toolset="lesson23_snapshot", description="Retrieve the scope-matched external runbook; online uses native OpenViking and evaluation uses its frozen result",
            schema={"name": CONTEXT_TOOL, "description": "Read an external runbook; not live telemetry.",
                    "parameters": {"type": "object", "properties": {key: {"type": "string"} for key in scope},
                                   "required": list(scope), "additionalProperties": False}},
            handler=context_handler,
            override=True,
        )
    key = os.environ.get("GLM_API_KEY", "").strip()
    if not key:
        raise ValueError("Configure the task's explicit model connection before starting Hermes")
    database = SessionDB(home / "state.db")
    agent = None
    try:
        before = database.get_messages(session_id)
        previous_last_id = max((row["id"] for row in before), default=0)
        history = None
        if resume:
            stored_report = next((row for row in before if row["id"] == checkpoint["report_item_id"]), None)
            if (database.get_session(session_id) is None or stored_report is None
                    or stored_report["role"] != "assistant" or stored_report.get("tool_calls")
                    or content_sha(stored_report["content"]) != checkpoint["report_sha256"]):
                raise ValueError("The saved native report Item is missing or has changed")
            history = database.get_messages_as_conversation(session_id, repair_alternation=True)
        elif before or database.get_session(session_id) is not None:
            raise ValueError("New tasks cannot append to a previously used Session")

        allowed = {"skill_view", "skills_list", *TOOLS, *([CONTEXT_TOOL] if bundle is not None else [])}
        # Dataset identifiers include split/case names; keep them in audit metadata,
        # rather than revealing a scoring label to the Agent as part of its input.
        task_prompt = build_prompt(request, context_required=bundle is not None)
        agent = AIAgent(
            model=request["model"], provider="custom", api_key=key, base_url=request["base_url"],
            api_mode="chat_completions", session_id=session_id, session_db=database,
            max_iterations=12, max_tokens=2400,
            enabled_toolsets=[readonly_skill_toolset(), "lesson23_snapshot"],
            skip_memory=True, skip_context_files=True, save_trajectories=False, quiet_mode=True,
            load_soul_identity=False,
            request_overrides={"temperature": 0, "extra_body": {"thinking": {"type": "disabled"}},
                               "extra_headers": {"X-Session-Id": session_id, "X-Turn-Type": "main"}},
            ephemeral_system_prompt="Run only this lesson's read-only task. Read the pinned Skill with skill_view before querying the frozen scene. Do not edit Skills, memory, or services.",
        )
        agent.skip_background_review = True
        agent.tools = [tool for tool in agent.tools if tool["function"]["name"] in allowed]
        agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
        if agent.valid_tool_names != allowed:
            raise RuntimeError("Hermes did not register this lesson's complete read-only tool set")
        set_thread_tool_whitelist(allowed)
        result = agent.run_conversation(user_message=task_prompt, conversation_history=history)
        messages = agent._session_messages
        starts = [index for index, row in enumerate(messages)
                  if row.get("role") == "user" and row.get("content") == task_prompt]
        if not starts:
            raise RuntimeError("Hermes did not retain the native task input")
        current_messages = messages[starts[-1]:]
        save(directory / "messages.json", messages)
        save(directory / "messages_current.json", current_messages)
        current_rows = [row for row in database.get_messages(session_id) if row["id"] > previous_last_id]
        final = current_rows[-1] if current_rows else None
        events = tool_events(current_messages)
        skill_reads = [event for event in events if event["tool"] == "skill_view"
                       and event["arguments"].get("name") == SKILL]
        loaded = any(isinstance(event["result"], dict) and event["result"].get("success") is True
                     and content_sha(event["result"].get("raw_content", event["result"].get("content", ""))) == expected_sha
                     for event in skill_reads)
        evidence = validate_task(current_messages, calls, case, expected_sha, scope=scope,
                                 frozen_snapshot=snapshot, expected_context=context)
        if bundle is not None and context is None:
            raise ValueError("The Agent did not retrieve the required external context")
        completed = bool(result.get("completed") and not result.get("failed") and not result.get("interrupted")
                         and loaded and sha(skill) == expected_sha and final and final["role"] == "assistant"
                         and not final.get("tool_calls") and final["content"] == result.get("final_response"))
        report_item_id = final["id"] if final and final["role"] == "assistant" else None
        if completed and not resume:
            checkpoint = {"session_id": session_id, "report_item_id": report_item_id,
                          "report_sha256": content_sha(final["content"]), "skill_sha256": expected_sha}
            save(checkpoint_file, checkpoint)
        save(directory / "result.json", {
            "session_id": session_id, "case": case, "role": role, "resumed": resume,
            "completed": completed, "loaded_skill_sha256": sha(skill), "native_skill_view_verified": loaded,
            "report_item_id": report_item_id, "answer": result.get("final_response"),
            "calls": calls, "available_tools": list(TOOLS), "evidence": evidence,
            "checkpoint": checkpoint, "prior_message_count": len(before),
            "new_item_ids": [row["id"] for row in current_rows],
            "task_id": request.get("task_id"), "split": request.get("split"), "context": context,
        })
        if not completed:
            raise RuntimeError("The native task did not complete with its pinned Skill and persisted report Item")
    finally:
        clear_thread_tool_whitelist()
        if agent is not None:
            agent.close()
        database.close()


if __name__ == "__main__":
    install_signal_handlers()
    main()
