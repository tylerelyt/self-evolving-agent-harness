"""无模型自检：完全构造的调用与故障，不依赖历史回放，不生成 live 结果。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common import HERE, save, tool_calls
from package_integrity import snapshot, package_hash, install_candidate
from native_trace import candidate_check


def fixture():
    """明确构造的校验器输入，不是模型运行或历史回放。"""
    before = {"existing/SKILL.md": "---\nname: existing\n---\n已有容量观察方法。\n"}
    name, directory, reference = "fixture-method", "fixture-method", "references/query.md"
    main_path, ref_path = directory + "/SKILL.md", directory + "/" + reference
    main = ("---\nname: fixture-method\ndescription: 仅供离线安全自检\n---\n"
            "## 步骤\n1. 对照指标、日志和代码，再重放正常与异常分支。\n"
            "2. 只保存重放支持的判断，未验证原因保留未知。\n"
            "详细工具字段见 references/query.md。\n")
    ref = "构造工具说明：metrics、log、code、test 都只读取本次现场。\n"
    after = {**before, main_path: main, ref_path: ref}
    messages = []

    def call(tool, args, receipt):
        ident = "constructed-" + str(len(messages))
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": ident, "type": "function",
             "function": {"name": tool, "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": ident, "content": json.dumps(receipt)},
        ])

    call("skill_manage", {"name": name, "action": "create", "content": main},
         {"success": True, "path": directory})
    call("skill_manage", {"name": name, "action": "write_file",
         "file_path": reference, "file_content": ref}, {"success": True})
    for path, relative in ((main_path, None), (ref_path, reference)):
        call("skill_view", {"name": name, **({"file_path": relative} if relative else {})},
             {"success": True, "content": after[path]})
    before_usage = {"existing": {"created_by": None, "created_at": "constructed"}}
    after_usage = {**before_usage, name: {"created_by": "agent", "created_at": "constructed"}}
    review = {
        "constructed": True, "actual_model_requests": 0,
        "candidate_name": name, "candidate_directory": directory,
        "before": before, "after": after, "inherited_call_ids": [],
        # 仅模拟原生返回字段来检验校验器，从不保存成 live 或 replay 结果。
        "completed": True, "thread": "bg-review", "model_requests": 1,
        "result": {"completed": True, "final_response": "构造测试输入", "messages": messages},
        "calls": tool_calls(messages),
        "before_package": {**before, ".usage.json": json.dumps(before_usage, ensure_ascii=False, sort_keys=True)},
        "after_package": {**after, ".usage.json": json.dumps(after_usage, ensure_ascii=False, sort_keys=True)},
    }
    review["baseline_sha256"] = package_hash(review["before_package"])
    return review

def put(root, files):
    for name, text in files.items():
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class SafetyChecks(unittest.TestCase):
    def test_native_review_receives_this_lessons_policy(self):
        import native_workshop as workshop
        from agent import background_review
        (HERE / 'runtime').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=HERE / 'runtime') as directory:
            root = Path(directory)
            (root / 'candidate/skills').mkdir(parents=True)
            job = {'snapshot': [{'role': 'user', 'content': '构造调查记录'}],
                   'cached_prompt': '构造前台提示', 'baseline_sha256': package_hash({})}
            save(root / 'job.json', job)
            observer = SimpleNamespace(install=lambda: None, restore=lambda: None, result={})
            parent = SimpleNamespace(close=lambda: None)
            with patch.dict(os.environ, {'HERMES_HOME': str(root / 'candidate')}), \
                 patch.object(workshop, 'model_options', return_value={}), \
                 patch.object(workshop, 'register_query'), \
                 patch('run_agent.AIAgent', return_value=parent), \
                 patch('review_observer.ReviewObserver', return_value=observer), \
                 patch.object(workshop, 'candidate_check', return_value={}), \
                 patch.object(background_review, '_run_review_in_thread') as run_review:
                workshop.review_job(root / 'job.json')
            policy = workshop.review_policy()
            called_parent, snapshot, prompt = run_review.call_args.args
            self.assertIs(called_parent, parent)
            self.assertEqual(snapshot, job['snapshot'])
            self.assertEqual(parent._SKILL_REVIEW_PROMPT, policy)
            self.assertTrue(prompt.startswith(policy))
            self.assertIn('没有形成可靠流程时', policy)
            self.assertIn('references/', policy)
            saved = json.loads((root / 'review.json').read_text())
            self.assertEqual(saved['prompt'], prompt)
            self.assertEqual(saved['policy_source'], 'config/native-skill-review-policy.md')

    def test_error_receipts_preserve_original_result(self):
        import scenario
        from native_workshop import check_foreground, loaded_before_query
        error = "HTTP 429: code1113 余额不足或无资源包"
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "constructed-error", "type": "function",
             "function": {"name": "investigate", "arguments": json.dumps(
                 {"case": scenario.INITIAL_CASE, "action": "metrics"})}}]},
            {"role": "tool", "tool_call_id": "constructed-error", "content": error},
        ]
        raw = {"completed": False, "failed": True, "error": error,
               "final_response": error, "messages": messages}
        task = {"result": copy.deepcopy(raw), "persisted": copy.deepcopy(messages)}
        target = self.root/"error-result.json"
        with patch("scenario.verify", side_effect=AssertionError("未完成时不做业务验收")):
            checked = check_foreground(task, scenario.INITIAL_CASE, target)
        saved = json.loads(target.read_text())
        self.assertEqual(saved["result"], raw)
        self.assertEqual(saved["persisted"], messages)
        self.assertEqual(saved["calls"], saved["persisted_calls"])
        self.assertEqual(saved["calls"][0]["receipt"], error)
        self.assertFalse(checked["business_checks"])
        self.assertNotIn("validation_error", checked)
        self.assertFalse(scenario.verify(saved["calls"], scenario.INITIAL_CASE))
        self.assertFalse(loaded_before_query(
            [{"name": "skill_view", "args": {"name": "method"}, "receipt": error}],
            "正文", ("method",)))
        task["result"].update(completed=True, failed=False, error=None, final_response="{}")
        def fail_check(*args):
            on_disk = json.loads(target.read_text())
            self.assertEqual(on_disk["calls"][0]["receipt"], error)
            self.assertEqual(on_disk["persisted"], messages)
            raise ValueError("构造验收器异常")
        with patch("scenario.verify", side_effect=fail_check):
            checked = check_foreground(task, scenario.INITIAL_CASE, target)
        self.assertIn("构造验收器异常", checked["validation_error"])
        self.assertFalse(checked["business_checks"])

    def test_answer_format_is_explicit_and_unambiguous(self):
        from native_workshop import parse_answer
        value = {"branch": "exception", "leaked_connections": 4}
        raw = json.dumps(value)
        block = "```json\n" + raw + "\n```"
        self.assertEqual(parse_answer({"final_response": raw}), (value, "pure_json"))
        self.assertEqual(parse_answer({"final_response": "说明\n```python\npass\n```\n" + block}),
                         (value, "json_block"))
        for text in (raw + raw, "说明 " + raw, "```\n" + raw + "\n```",
                     "```json\n{broken}\n```", "```json\n" + raw, "[]"):
            self.assertEqual(parse_answer({"final_response": text}), ({}, "invalid"))
        self.assertEqual(parse_answer({"final_response": block + "\n" + block}), ({}, "ambiguous"))

    def test_future_session_reads_exact_method_before_query(self):
        from native_workshop import loaded_before_query
        view = {"name": "skill_view", "args": {"name": "current"},
                "receipt": {"success": True, "content": "保存的正文"}}
        query = {"name": "investigate", "args": {"action": "metrics"}}
        self.assertTrue(loaded_before_query([view, query], "保存的正文", ("current",)))
        self.assertFalse(loaded_before_query([query, view], "保存的正文", ("current",)))
        self.assertFalse(loaded_before_query([view, query], "旧正文", ("current",)))
        self.assertFalse(loaded_before_query([view, query], "保存的正文", ("other",)))

    def test_completed_no_update_is_not_creation(self):
        from native_workshop import review_outcome
        review = fixture()
        review["after"] = dict(review["before"])
        review["after_package"] = dict(review["before_package"])
        review["calls"] = []
        review["result"]["messages"] = []
        self.assertEqual(review_outcome(review), "no-update")
        review["result"]["completed"] = False
        with self.assertRaises(RuntimeError):
            review_outcome(review)

    def test_public_model_configuration(self):
        from native_workshop import model_options
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "缺少 GLM_API_KEY"):
                model_options()
            os.environ["GLM_API_KEY"] = "constructed-not-a-credential"
            options = model_options()
            self.assertEqual(options["model"], "glm-5.2")
            self.assertEqual(options["base_url"], "https://open.bigmodel.cn/api/paas/v4")
            os.environ.update(GLM_MODEL="example-model", GLM_BASE_URL="https://example.invalid/v1")
            options = model_options()
            self.assertEqual(options["model"], "example-model")
            self.assertEqual(options["base_url"], "https://example.invalid/v1")

    def setUp(self):
        (HERE/"runtime").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="selfcheck-", dir=HERE/"runtime")
        self.root = Path(self.temporary.name)
        self.review = fixture()

    def tearDown(self):
        self.temporary.cleanup()

    def test_trace_and_completion(self):
        self.assertTrue(candidate_check(self.review)["candidate_complete"])
        for key in ("completed", "failed", "interrupted", "partial"):
            review = copy.deepcopy(self.review)
            review["result"][key] = key != "completed"
            self.assertFalse(candidate_check(review)["candidate_complete"], key)
        review = copy.deepcopy(self.review)
        review["calls"] = [c for c in review["calls"] if not
            (c["name"] == "skill_manage" and c["args"].get("action") == "write_file")]
        self.assertFalse(candidate_check(review)["candidate_complete"])
        # 同时从完整消息移除写入，排除仅检查两份记录一致却未重建文件的情况。
        ids = {c["tool_call_id"] for c in self.review["calls"]
               if c["name"] == "skill_manage" and c["args"].get("action") == "write_file"}
        for message in review["result"]["messages"]:
            if message.get("tool_calls"):
                message["tool_calls"] = [c for c in message["tool_calls"] if c["id"] not in ids]
        review["result"]["messages"] = [m for m in review["result"]["messages"] if m.get("tool_call_id") not in ids]
        self.assertFalse(candidate_check(review)["candidate_complete"])
        for path, content in (("untracked.txt", "脚本补写"), (next(iter(self.review["before"])), None)):
            review = copy.deepcopy(self.review)
            if content is None:
                review["after"].pop(path)
                review["after_package"].pop(path)
            else:
                review["after"][path] = content
                review["after_package"][path] = content
            self.assertFalse(candidate_check(review)["candidate_complete"])

    def test_package_adoption_binds_files_and_origin(self):
        official, candidate = self.root/"official", self.root/"candidate"
        put(official, self.review["before_package"])
        put(candidate, self.review["after_package"])
        baseline = package_hash(snapshot(official))
        digest = package_hash(snapshot(candidate))
        usage = json.loads((candidate/".usage.json").read_text())
        usage[self.review["candidate_name"]]["view_count"] = 999
        (candidate/".usage.json").write_text(json.dumps(usage))
        self.assertEqual(package_hash(snapshot(candidate)), digest)
        usage[self.review["candidate_name"]]["created_by"] = "installed"
        (candidate/".usage.json").write_text(json.dumps(usage))
        with self.assertRaises(ValueError):
            install_candidate(candidate, official, digest, baseline)
        put(candidate, self.review["after_package"])
        (candidate/".unexpected").write_text("未审阅")
        with self.assertRaises(ValueError):
            install_candidate(candidate, official, digest, baseline)
        (candidate/".unexpected").unlink()
        (official/"concurrent.txt").write_text("其他维护者的新版本")
        with self.assertRaisesRegex(ValueError, "基线哈希"):
            install_candidate(candidate, official, digest, baseline)
        (official/"concurrent.txt").unlink()
        installed = install_candidate(candidate, official, digest, baseline)
        self.assertEqual(package_hash(snapshot(official)), digest)
        self.assertEqual(snapshot(self.root/installed["backup"]), self.review["before_package"])

    def test_copy_recheck_and_rollback(self):
        official, candidate = self.root/"official", self.root/"candidate"
        put(official, self.review["before_package"])
        put(candidate, self.review["after_package"])
        baseline, digest = package_hash(snapshot(official)), package_hash(snapshot(candidate))
        import shutil
        original = shutil.copytree
        def altered(*args, **kwargs):
            result = original(*args, **kwargs)
            (Path(args[1])/".extra").write_text("复制期间新增")
            return result
        with patch("package_integrity.shutil.copytree", side_effect=altered):
            with self.assertRaises(ValueError):
                install_candidate(candidate, official, digest, baseline)
        self.assertEqual(package_hash(snapshot(official)), baseline)
        original_rename = Path.rename
        def fail_stage(path, target):
            if path.name.startswith("skills-stage-"):
                raise OSError("构造替换失败")
            return original_rename(path, target)
        with patch.object(Path, "rename", fail_stage):
            with self.assertRaises(OSError):
                install_candidate(candidate, official, digest, baseline)
        self.assertEqual(package_hash(snapshot(official)), baseline)
        (candidate/"link").symlink_to(official/"missing")
        with self.assertRaises(ValueError):
            snapshot(candidate)

    def test_atomic_records_and_owned_process_cleanup(self):
        from concurrent.futures import ThreadPoolExecutor
        target = self.root/"record.json"
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda i: save(target, {"writer": i}), range(40)))
        self.assertIn(json.loads(target.read_text())["writer"], range(40))
        self.assertFalse(list(self.root.glob("*.tmp")))
        from native_workshop import terminate_owned
        command = [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"]
        owned = subprocess.Popen(command, start_new_session=True, env={})
        other = subprocess.Popen(command, start_new_session=True, env={})
        try:
            terminate_owned(owned)
            self.assertIsNotNone(owned.poll())
            self.assertIsNone(other.poll())
            terminate_owned(owned)
        finally:
            for process in (owned, other):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
