"""只检查确认、解析失败和范围约束；不启动模型、不写业务文件。"""
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY.parent))
from notebook09_story import IsolationApprovalStory
from notebook_support import read_json_answer


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.story = IsolationApprovalStory.__new__(IsolationApprovalStory)
        self.story.closed, self.story.offline = False, False
        self.story.runtime = Path("offline-unit-check")
        self.story.permission = self.story.confirmed_plan = None
        self.story.confirmations = []
        self.records = {}
        self.story.save = lambda name, value: self.records.update({name: deepcopy(value)})
        self.config = {"cwd": "/fixture/adapter", "approvalPolicy": "never", "sandboxPolicy": {
            "type": "workspaceWrite", "writableRoots": ["/fixture/tests"], "networkAccess": False,
            "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}}
        self.story.expected_config = deepcopy(self.config)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_both_confirmations_are_required_and_record_exact_content(self):
        self.assertEqual(self.story.review_scope(self.config), self.config)
        self.assertIsNone(self.story.permission)
        with self.assertRaises(ValueError):
            self.story.confirm_scope(self.config)
        with patch("builtins.input", side_effect=["确认计划", "应用配置"]):
            self.story.confirm_plan("本轮只读计划")
            self.assertIsNone(self.story.permission)
            approved = self.story.confirm_scope(self.config)
        self.assertEqual(self.story.require_permission(self.config), self.config)
        self.assertEqual(self.story.confirmations[1]["content"],
                         {"plan": "本轮只读计划", "config": self.config})
        approved["sandboxPolicy"]["writableRoots"].append("/fixture/lib")
        self.assertEqual(self.story.permission, self.config)
        with self.assertRaises(ValueError):
            self.story.require_permission(approved)

    def test_empty_or_denied_input_never_applies(self):
        for answer in ("", "no", "yes"):
            with self.subTest(answer=answer), patch("builtins.input", return_value=answer):
                with self.assertRaises(ValueError):
                    self.story.confirm_plan("本轮计划")
                self.assertIsNone(self.story.confirmed_plan)
                self.story.confirmed_plan = "已确认的本轮计划"
                with self.assertRaises(ValueError):
                    self.story.confirm_scope(self.config)
                self.assertIsNone(self.story.permission)

    def test_interrupted_confirmation_clears_old_permission(self):
        self.story.permission = deepcopy(self.config)
        self.story.confirmed_plan = "本轮计划"
        with patch("builtins.input", side_effect=EOFError), self.assertRaises(EOFError):
            self.story.confirm_scope(self.config)
        self.assertIsNone(self.story.permission)
        self.assertFalse(self.story.confirmations[-1]["accepted"])
        self.assertIsNone(self.records["reviewed-config.json"]["approved"])

    def test_scope_cannot_be_widened_by_confirmation(self):
        for field, value in (("writableRoots", ["/fixture/tests", "/fixture/lib"]),
                             ("networkAccess", True), ("networkAccess", 0)):
            widened = deepcopy(self.config)
            widened["sandboxPolicy"][field] = value
            self.story.confirmed_plan = "本轮计划"
            with self.subTest(field=field, value=value), patch("builtins.input", return_value="应用配置") as prompt:
                with self.assertRaises(ValueError):
                    self.story.confirm_scope(widened)
                prompt.assert_not_called()
                self.assertIsNone(self.story.permission)

    def test_failed_generation_does_not_fall_back_or_reuse_approval(self):
        cells = json.loads((DIRECTORY / "workshop.ipynb").read_text())["cells"]
        source = {c["id"]: "".join(c["source"]) for c in cells}
        self.story.proposal_task = "只读生成配置"
        self.story.config_from_materials = Mock(side_effect=AssertionError("不得回退预置配置"))
        for text in ("", "not json", '{"cwd": "x"}', '说明：{"cwd":"x","approvalPolicy":"never","sandboxPolicy":{}}'):
            self.story.confirmed_plan, self.story.permission = "本轮计划", deepcopy(self.config)
            client = Mock()
            client.turn_start.return_value.turn.id = "fake-turn"
            turn = SimpleNamespace(run=lambda: SimpleNamespace(final_response=text))
            namespace = {"story": self.story, "lab": SimpleNamespace(watch=lambda _: nullcontext(), record_result=Mock()),
                "client": client, "TurnHandle": lambda *args: turn, "planner_id": "fake-plan",
                "readonly": {"sandboxPolicy": {"type": "readOnly"}}, "record_policy": Mock(),
                "read_json_answer": read_json_answer, "proposal": deepcopy(self.config), "permission": deepcopy(self.config)}
            with self.subTest(text=text), self.assertRaises(AssertionError):
                exec(compile(source["lab09-generate-config"], "generate-config", "exec"), namespace)
            self.assertIsNone(namespace["proposal"])
            self.assertIsNone(namespace["permission"])
            self.assertIsNone(self.story.permission)
            self.story.config_from_materials.assert_not_called()
            with self.assertRaises(ValueError):
                exec(compile(source["lab09-07"], "execute-hotfix", "exec"), namespace)
            client.thread_start.assert_not_called()

    def test_live_material_fallback_is_forbidden(self):
        with self.assertRaises(ValueError):
            self.story.config_from_materials()


if __name__ == "__main__":
    unittest.main()
