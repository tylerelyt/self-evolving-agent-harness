"""Cross-process persistence and evidence-boundary checks for the shared core."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from self_evolving_harness.artifacts import snapshot_skill
from self_evolving_harness.evaluation import release_allowed
from self_evolving_harness.learning import LearningLedger


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_concurrent_writers_preserve_one_exact_snapshot(self):
        content = b"a verified method\n"
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: snapshot_skill(self.root, content, "triage"), range(18)))
        self.assertEqual(len(set(results)), 1)
        source, identity = results[0]
        self.assertEqual(identity, sha256(content).hexdigest())
        self.assertEqual((source / "triage/SKILL.md").read_bytes(), content)
        self.assertFalse(list(self.root.rglob(".skill-*")))

    def test_corrupt_existing_version_is_not_overwritten(self):
        source, _ = snapshot_skill(self.root, b"original", "triage")
        entry = source / "triage/SKILL.md"
        entry.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "digest"):
            snapshot_skill(self.root, b"original", "triage")
        self.assertEqual(entry.read_bytes(), b"corrupt")

    def test_names_and_symlinks_cannot_redirect_snapshot_writes(self):
        for name in ("../outside", "/outside", "a/b", "", ".."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                snapshot_skill(self.root, b"content", name)
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "versions").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            snapshot_skill(self.root, b"content", "triage")
        self.assertEqual(list(outside.iterdir()), [])


class LearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "learning.sqlite3"
        self.ledger = LearningLedger(self.path)
        self.parent = sha256(b"old").hexdigest()
        self.candidate = sha256(b"new").hexdigest()
        self.spec = dict(scope="project/environment", artifact_sha256=self.candidate,
                         parent_sha256=self.parent, sources=["session-a:item-4", "session-b:item-9"],
                         generator="skillclaw:pinned")
        self.identifier = self.ledger.register(**self.spec)

    def report(self):
        return {"passed": True, "candidate_sha256": self.candidate, "baseline_sha256": self.parent,
                "cases": [{"case": "independent-failure", "baseline": {"passed": False, "valid_trace": True},
                           "candidate": {"passed": True, "valid_trace": True}}]}

    def publication(self):
        return {"sha256": self.candidate,
                "receivers": {name: {"sha256": self.candidate} for name in ("a", "b")},
                "receiver_errors": {}}

    def prepare_publication(self):
        self.ledger.evaluate(self.identifier, self.report())
        self.ledger.begin_publication(self.identifier, ["a", "b"])

    def test_registration_is_idempotent_and_scope_is_part_of_identity(self):
        self.assertEqual(self.ledger.register(**self.spec), self.identifier)
        other = self.ledger.register(**{**self.spec, "scope": "other-environment"})
        self.assertNotEqual(other, self.identifier)
        self.assertEqual(len(self.ledger.history(self.identifier)), 1)

    def test_concurrent_registrations_do_not_duplicate_history(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: LearningLedger(self.path).register(**self.spec), range(8)))
        self.assertEqual(set(ids), {self.identifier})
        self.assertEqual(len(self.ledger.history(self.identifier)), 1)

    def test_evaluation_cannot_approve_missing_or_forged_evidence(self):
        changes = [{"cases": []}, {"baseline_sha256": "a" * 64}, {"candidate_sha256": "b" * 64}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.ledger.evaluate(self.identifier, {**self.report(), **change})
        for field in ("valid_trace", "passed"):
            report = self.report()
            report["cases"][0]["candidate"][field] = False
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.ledger.evaluate(self.identifier, report)
        self.assertEqual(self.ledger.get(self.identifier)["status"], "proposed")

    def test_report_is_immutable_after_first_valid_decision(self):
        report = self.report()
        self.ledger.evaluate(self.identifier, report)
        self.ledger.evaluate(self.identifier, report)
        changed = {**report, "passed": False}
        with self.assertRaisesRegex(ValueError, "change its report"):
            self.ledger.evaluate(self.identifier, changed)
        self.assertEqual(self.ledger.get(self.identifier)["status"], "eligible")

    def test_rejection_cannot_publish(self):
        self.ledger.evaluate(self.identifier, {**self.report(), "passed": False})
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.ledger.begin_publication(self.identifier, ["a", "b"])

    def test_interrupted_publication_survives_a_new_process(self):
        self.prepare_publication()
        code = ("import sys;sys.path.insert(0,sys.argv[1]);"
                "from self_evolving_harness.learning import LearningLedger;"
                "print(LearningLedger(sys.argv[2]).get(sys.argv[3])['status'])")
        result = subprocess.run([sys.executable, "-E", "-S", "-B", "-c", code,
                                 str(ROOT / "src"), str(self.path), self.identifier],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "publishing")
        with self.assertRaises(ValueError):
            self.ledger.begin_publication(self.identifier, ["a", "b"])

    def test_partial_publication_cannot_be_completed(self):
        self.prepare_publication()
        receipt = self.publication()
        del receipt["receivers"]["b"]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.ledger.published(self.identifier, receipt)
        self.assertEqual(self.ledger.get(self.identifier)["status"], "publishing")

    def test_adoption_requires_each_receiving_instance_and_preserves_history(self):
        self.prepare_publication()
        receipt = self.publication()
        self.ledger.published(self.identifier, receipt)
        self.ledger.published(self.identifier, receipt)
        adoption = {name: {"skill_sha256": self.candidate, "verified": True,
                           "session_id": "new-session-" + name, "report_item_id": 15}
                    for name in ("a", "b")}
        with self.assertRaises(ValueError):
            self.ledger.adopted(self.identifier, {"a": adoption["a"]})
        bad = deepcopy(adoption)
        bad["b"]["skill_sha256"] = self.parent
        with self.assertRaises(ValueError):
            self.ledger.adopted(self.identifier, bad)
        reopened = LearningLedger(self.path)
        reopened.adopted(self.identifier, adoption)
        reopened.adopted(self.identifier, adoption)
        self.assertEqual(reopened.get(self.identifier)["status"], "adopted")
        self.assertEqual([row["kind"] for row in reopened.history(self.identifier)],
                         ["proposed", "eligible", "publication_intent", "published", "adopted"])

    def test_future_schema_is_refused_without_reinterpretation(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ValueError, "schema"):
            LearningLedger(self.path)


class SharedEntryTests(unittest.TestCase):
    def test_workbench_keeps_ownership_of_its_explicit_environment_file(self):
        from types import SimpleNamespace
        sys.path.insert(0, str(ROOT))
        from apps.oncall.__main__ import main
        run = Mock()
        load_environment = Mock()
        with patch.dict(sys.modules, {"app": SimpleNamespace(main=run),
                                      "workbench_config": SimpleNamespace(load_model_environment=load_environment)}):
            main(["workbench", "--live", "--env-file", "/tmp/private-model.env"])
        run.assert_called_once_with()
        load_environment.assert_not_called()

    def test_legacy_service_is_the_application_module_not_a_copy(self):
        sys.path.insert(0, str(ROOT))
        from apps.oncall.paths import prepare
        prepare()
        import oncall_service
        from apps.oncall import service
        self.assertIs(oncall_service, service)

    def test_tutorial_and_application_export_the_same_runtime_functions(self):
        sys.path.insert(0, str(ROOT))
        from apps.oncall import workflow
        from implementation import REFERENCE, load_implementation
        tutorial = load_implementation(REFERENCE)
        self.assertIs(tutorial.run, workflow.run)
        self.assertIs(tutorial.release_policy, release_allowed)
        self.assertIs(workflow.release_policy, release_allowed)

    def test_core_has_no_tutorial_or_application_imports(self):
        import ast
        for path in (ROOT / "src/self_evolving_harness").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("workshop", "apps", "examples")), path)


if __name__ == "__main__":
    unittest.main()
