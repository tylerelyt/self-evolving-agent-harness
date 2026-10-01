"""不导入模型 SDK、不联网：验证现场、记录归属及等待语义。"""
import copy
import json
import logging
import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from observer import Observer, tool_calls
from scene import CURRENT_ENTRY, FIRST_WINDOW, MIGRATION, NEXT_TASK, NEXT_WINDOW, OLD_ENTRY, adoption, query
import run_background_review as runner


def messages(call_id, name="memory", args=None, receipt=None):
    return [{"role": "assistant", "tool_calls": [{"id": call_id,
             "function": {"name": name, "arguments": json.dumps(args or {"action": "add"})}}]},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(receipt or {"success": True})}]


class SceneTests(unittest.TestCase):
    def test_archive_and_current_have_different_time_coverage(self):
        args = {"action": "read", "entry": OLD_ENTRY, "since": FIRST_WINDOW[0], "until": FIRST_WINDOW[1]}
        self.assertEqual(query(args)["count"], 0)
        args["entry"] = CURRENT_ENTRY
        self.assertEqual(query(args)["count"], 2)
        args.update(since=NEXT_WINDOW[0], until=NEXT_WINDOW[1])
        self.assertEqual(query(args)["count"], 1)
        self.assertEqual(query({"action": "describe"})["deployment"], MIGRATION)

    def test_wrong_window_and_reply_without_query_do_not_count_as_use(self):
        args = {"action": "read", "entry": CURRENT_ENTRY, "since": FIRST_WINDOW[0], "until": FIRST_WINDOW[1]}
        calls = tool_calls(messages("query", "lesson11_logs", args, query(args)))
        self.assertFalse(adoption(calls, NEXT_WINDOW)["current_entry_used"])
        self.assertTrue(adoption(calls, FIRST_WINDOW)["current_entry_used"])
        self.assertFalse(adoption([], NEXT_WINDOW)["current_entry_used"])
        self.assertNotIn(CURRENT_ENTRY, NEXT_TASK)
        self.assertNotIn("迁移", NEXT_TASK)

    def test_only_supported_read_actions_and_zoned_windows(self):
        self.assertFalse(query({"action": "write"})["success"])
        self.assertFalse(query({"action": "read", "entry": "../MEMORY.md"})["success"])
        self.assertFalse(query({"action": "read", "entry": CURRENT_ENTRY,
                                "since": "2026-10-20T10:00:00", "until": FIRST_WINDOW[1]})["success"])

    def test_inherited_tool_receipts_are_not_background_operations(self):
        inherited = messages("foreground")
        current = inherited + messages("background", receipt={"success": False})
        calls = tool_calls(current, inherited)
        self.assertEqual([c["tool_call_id"] for c in calls], ["background"])
        self.assertIs(calls[0]["receipt"]["success"], False)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        # 全部临时写入都留在第11讲目录，并在测试结束时清理。
        self.temp = tempfile.TemporaryDirectory(prefix="offline-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        self.release = threading.Event()
        self.outputs = {}
        outer = self
        class Agent:
            def run_conversation(self, **kwargs):
                return {"completed": True, "final_response": "没有新内容", "messages": kwargs.get("history", [])}
            def _spawn_background_review(self, **kwargs):
                def target():
                    outer.background._run_review_in_thread(self, kwargs["messages_snapshot"], "原生提示")
                threading.Thread(target=target, name="bg-review", daemon=True).start()
        def worker(parent, history, prompt):
            outer.release.wait()
            Agent().run_conversation(history=history)
        self.Agent = Agent
        self.background = SimpleNamespace(_run_review_in_thread=worker, logger=logging.getLogger("lesson11-offline"))
        self.observer = Observer(Agent, self.background, home,
                                 lambda name, value: self.outputs.update({name: copy.deepcopy(value)}))
        self.observer.install()
        self.agent = Agent()
        self.observer.watch(self.agent)
        self.agent._lesson11_turn = "sample"
        self.addCleanup(self.observer.restore)
        self.addCleanup(self.release.set)

    def test_no_spawn_is_an_observation(self):
        self.assertEqual(self.observer.wait("sample", 0.01)["state"], "not_triggered")

    def test_timeout_does_not_mean_finished_and_no_write_is_valid(self):
        self.agent._spawn_background_review(messages_snapshot=messages("old"))
        self.assertEqual(self.observer.wait("sample", 0.02)["state"], "pending")
        self.release.set()
        self.assertEqual(self.observer.wait("sample", 1)["state"], "ended")
        event = self.outputs["reviews/sample.json"]
        self.assertEqual(event["calls"], [])
        self.assertEqual(event["memory_before"], event["memory_after"])
        self.assertTrue(event["daemon"])
        self.assertTrue(runner.completed(event["result"]))

    def test_clean_rejects_arbitrary_path(self):
        with self.assertRaises(ValueError):
            runner.clean("../other")


class WorkflowTests(unittest.TestCase):
    def test_no_write_and_no_adoption_still_finish_with_fresh_history(self):
        instances = []
        class Agent:
            def __init__(self, **kwargs):
                self.session_id, self.model = kwargs["session_id"], kwargs["model"]
                self._memory_nudge_interval, self._skill_nudge_interval = 2, 0
                self._turns_since_memory = 0
                self.valid_tool_names = {"memory", "lesson11_logs"}
                self._memory_store = SimpleNamespace(format_for_system_prompt=lambda target: "")
                instances.append(self)
            def run_conversation(self, user_message, system_message=None, conversation_history=None):
                history = list(conversation_history or [])
                history += [{"role": "user", "content": user_message}, {"role": "assistant", "content": "未取得日志"}]
                self._turns_since_memory += 1
                if self._memory_nudge_interval and self._turns_since_memory >= self._memory_nudge_interval:
                    self._turns_since_memory = 0
                    self._spawn_background_review(messages_snapshot=history)
                return {"completed": True, "final_response": "未取得日志", "messages": history}
            def _spawn_background_review(self, messages_snapshot):
                threading.Thread(target=lambda: background._run_review_in_thread(self, messages_snapshot, "测试提示"),
                                 daemon=True, name="bg-review").start()
            def close(self):
                pass
        def worker(parent, history, prompt):
            fork = Agent(session_id="review", model="offline")
            fork._memory_nudge_interval = 0
            fork.run_conversation(prompt, conversation_history=history)
        background = SimpleNamespace(_run_review_in_thread=worker, logger=logging.getLogger("workflow-offline"))
        modules = {"run_agent": SimpleNamespace(AIAgent=Agent),
                   "agent": SimpleNamespace(background_review=background),
                   "tools.registry": SimpleNamespace(registry=SimpleNamespace(register=lambda **kw: None))}
        args = SimpleNamespace(nudge_interval=2, review_timeout=1, model="offline", base_url="http://unused", controls=False)
        revision = (runner.ROOT / "scripts/hermes-revision.txt").read_text().strip()
        with tempfile.TemporaryDirectory(prefix="offline-flow-", dir=Path(__file__).parent) as directory:
            with patch.object(runner, "RUNTIME", Path(directory)), \
                    patch.dict("sys.modules", modules), \
                    patch.dict(os.environ, {"GLM_API_KEY": "offline-placeholder", "HERMES_SRC": directory}), \
                    patch.object(runner.subprocess, "check_output", side_effect=[revision, ""]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.run_live(args), 0)
            report = json.loads(next(Path(directory).glob("run-*/observation.json")).read_text())
            self.assertEqual(report["status"], "observed")
            self.assertEqual(len(report["turns"]), 3)
            first, corrected, fresh = report["turns"]
            self.assertTrue(corrected["input_history"])
            self.assertEqual(fresh["input_history"], [])
            self.assertNotEqual(first["session_id"], fresh["session_id"])
            self.assertEqual(corrected["review_wait"]["state"], "ended")
            self.assertEqual(corrected["background_memory_calls"], [])
            self.assertFalse(report["new_session_behavior"]["current_entry_used"])


if __name__ == "__main__":
    unittest.main()
