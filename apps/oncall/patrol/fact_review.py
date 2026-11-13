#!/usr/bin/env python3
"""Propose one historical incident fact through native Hermes background review."""

import argparse
from contextlib import nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from threading import Thread, current_thread
from unittest.mock import patch

from hermes_runtime import ONCALL_SKILL, SOURCE, ensure_loopback_no_proxy
from patrol import decode_report
from shared_facts import validate_fact


FACT_PREFIX = "CAPSTONE_INCIDENT_FACT_V1 "
MEMORY_FILE = Path("hermes/memories/MEMORY.md")
PROMPT = (
    "The incident below has already closed. Treat it as a historical observation only. "
    "Decide whether it belongs in the incident knowledge base for a later handoff. "
    "To propose it, copy the entire single line beginning CAPSTONE_INCIDENT_FACT_V1 exactly "
    "as the content of one native memory call with action add and target memory. "
    "After a successful tool receipt, reply with JSON {\"status\":\"proposed\"}. "
    "If it is unsuitable, write nothing and reply with JSON {\"status\":\"keep\"}. "
    "Do not use other tools, include original messages, infer root cause or infer current status.\n"
)


def evidence_fact(raw):
    fact = json.loads(raw)
    validate_fact(fact)
    return fact


def scripted_review(messages):
    entry = next((line for message in messages if message["role"] == "user"
                  for line in message["content"].splitlines() if line.startswith(FACT_PREFIX)), None)
    if entry is None:
        raise ValueError("Scripted review needs persisted incident evidence")
    if any(message["role"] == "tool" for message in messages):
        return None, {"status": "proposed"}
    return "memory", {"action": "add", "target": "memory", "content": entry}


def review_connection(offline, responder=scripted_review):
    if offline:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from local_model import serve
        return serve(responder)
    return nullcontext(os.environ.get("GLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4")


def native_review(state, entry, options, *, review_prompt=PROMPT, initial_memory=None, initial_skill=None):
    if not (SOURCE / "run_agent.py").is_file():
        raise RuntimeError("Install the pinned course dependencies with bash scripts/setup_deps.sh")
    home = state / "hermes"
    home.mkdir(parents=True, exist_ok=False)
    skills_review = initial_skill is not None
    if skills_review:
        if initial_memory is not None:
            raise ValueError("Skill review requires a separate Home without seeded Memory")
        (home / ".no-bundled-skills").touch()
        skill = home / "skills" / ONCALL_SKILL / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_bytes(initial_skill)
        skill.chmod(0o444)
    if initial_memory is not None:
        memory = state / MEMORY_FILE
        memory.parent.mkdir()
        memory.write_bytes(initial_memory)
    (home / "config.yaml").write_text(json.dumps({
        "memory": {"memory_enabled": not skills_review, "user_profile_enabled": False,
                   "nudge_interval": 0, "write_approval": False},
        "compression": {"enabled": False},
        "skills": {"creation_nudge_interval": 0, "external_dirs": [], "write_approval": False},
        "tools": {"tool_search": {"enabled": False}},
    }), encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(SOURCE))
    import hermes_cli.env_loader
    hermes_cli.env_loader.load_hermes_dotenv = lambda **_kwargs: []
    from agent.background_review import spawn_background_review_thread
    from run_agent import AIAgent
    from tools.thread_context import propagate_context_to_thread

    if skills_review:
        from tools import skill_usage
        skill_usage.set_pinned(ONCALL_SKILL, True)
        if skill_usage.get_record(ONCALL_SKILL).get("pinned") is not True:
            raise RuntimeError("Hermes did not pin the isolated formal Skill")
    toolset, disabled = ("skills", "memory") if skills_review else ("memory", "skills")
    expected_tools = {"skills_list", "skill_view", "skill_manage"} if skills_review else {"memory"}
    parent = AIAgent(**options, provider="custom", api_mode="chat_completions", quiet_mode=True,
                     enabled_toolsets=[toolset], disabled_toolsets=[disabled], skip_memory=True,
                     skip_context_files=True, load_soul_identity=False, save_trajectories=False,
                     max_tokens=1024,
                     request_overrides={"temperature": 0, "extra_body": {"thinking": {"type": "disabled"}}})
    captured = []
    run_conversation = AIAgent.run_conversation

    def observe(agent, *args, **kwargs):
        flags = {"memory_write_origin": agent._memory_write_origin,
                 "persist_disabled": agent._persist_disabled,
                 "memory_manager_is_none": agent._memory_manager is None,
                 "valid_tool_names": sorted(agent.valid_tool_names),
                 "is_parent": agent is parent}
        record = {"schema_version": 1, "session_id": agent.session_id,
                  "worker_thread": current_thread().name, "worker_flags": flags,
                  "status": "started", "result": None}

        def preserve():
            # Native review forks intentionally disable SessionDB persistence.
            # Keep their raw return/transcript separately before any adapter
            # validation, including rejected or interrupted review attempts.
            path = state / "native-review.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)

        preserve()
        if (agent is parent or agent._memory_write_origin != "background_review"
                or not agent._persist_disabled or agent._memory_manager is not None
                or agent.valid_tool_names != expected_tools):
            raise RuntimeError("Hermes did not start a review fork with just the expected native tools")
        try:
            result = run_conversation(agent, *args, **kwargs)
            record.update(status="returned", result=result)
            captured.append((agent.session_id, current_thread().name, result))
            return result
        except BaseException as exc:
            record.update(status="raised", error_type=type(exc).__name__)
            raise
        finally:
            preserve()

    try:
        if not skills_review and (not parent._memory_enabled or parent._memory_store is None):
            raise RuntimeError("Hermes did not initialize the isolated native Memory")
        parent._cached_system_prompt = f"You review saved evidence for scoped {toolset} changes in an isolated workspace."
        prompt_attribute = "_SKILL_REVIEW_PROMPT" if skills_review else "_MEMORY_REVIEW_PROMPT"
        setattr(parent, prompt_attribute, review_prompt + entry)
        target, _ = spawn_background_review_thread(parent, [], review_memory=not skills_review, review_skills=skills_review)
        worker = Thread(target=propagate_context_to_thread(target), daemon=True, name="bg-review")
        with patch.object(AIAgent, "run_conversation", observe):
            worker.start()
            worker.join(timeout=120)
        if worker.is_alive() or len(captured) != 1:
            raise RuntimeError("Native Hermes background review did not finish")
    finally:
        parent.close()
    return captured[0]


def verified_result(state, entry, fact, captured):
    session_id, worker_thread, result = captured
    if (not result.get("completed") or not result.get("final_response")
            or any(result.get(key) for key in ("failed", "error", "interrupted", "partial"))):
        raise RuntimeError("Native Hermes background review did not complete its turn")
    decision = decode_report(result["final_response"])
    if not isinstance(decision, dict) or decision.get("status") not in ("proposed", "keep"):
        raise ValueError("Background review did not return a proposal decision")
    messages = result["messages"]
    calls = [call for message in messages for call in message.get("tool_calls") or []]
    memory = state / MEMORY_FILE
    readback = memory.read_bytes() if memory.exists() else b""
    trace = {"completed": True, "memory_write_verified": False, "memory_item": None,
             "memory_sha256": None, "session_id": session_id, "memory_tool_calls": [],
             "worker_thread": worker_thread}
    if decision["status"] == "keep":
        if calls or readback.strip():
            raise RuntimeError("A keep decision unexpectedly attempted a write")
        return {"status": "keep", "fact": None, "native_review": trace}

    if len(calls) != 1 or calls[0]["function"]["name"] != "memory":
        raise RuntimeError("A proposal requires exactly one native Memory call")
    call = calls[0]
    arguments = call["function"]["arguments"]
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    receipt = next((json.loads(message["content"]) for message in messages
                    if message.get("role") == "tool" and message.get("tool_call_id") == call["id"]), {})
    if (args != {"action": "add", "target": "memory", "content": entry}
            or receipt.get("success") is not True or receipt.get("target") != "memory"
            or readback.decode("utf-8").strip() != entry):
        raise RuntimeError("Native Memory receipt and read-back do not match the source fact")
    trace.update(memory_write_verified=True, memory_item=MEMORY_FILE.as_posix(),
                 memory_sha256=sha256(readback).hexdigest(),
                 memory_tool_calls=[{"id": call["id"], "name": "memory", "action": "add",
                                     "target": "memory", "success": True}])
    return {"status": "proposed", "fact": fact, "native_review": trace}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Use the local scripted model")
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        parser.error("--out must be a new file")
    raw = args.evidence.read_bytes()
    fact = evidence_fact(raw)
    entry = FACT_PREFIX + json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not args.offline and not os.environ.get("GLM_API_KEY", "").strip():
        raise RuntimeError("Set GLM_API_KEY or use --offline")
    ensure_loopback_no_proxy()
    with review_connection(args.offline) as url:
        options = {"base_url": url, "api_key": "local-fixture" if args.offline else os.environ["GLM_API_KEY"],
                   "model": "review-fixture" if args.offline else os.environ.get("GLM_MODEL") or "glm-5.2"}
        state = args.state.resolve()
        proposal = verified_result(state, entry, fact, native_review(state, entry, options))
    proposal.update(source_sha256=sha256(raw).hexdigest(), mode="local-script-model" if args.offline else "live")
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(proposal, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ImportError) as exc:
        sys.exit(f"Native fact review failed: {exc}")
