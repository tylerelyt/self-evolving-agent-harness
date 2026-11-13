"""Governed personal Skills with real tool subprocesses and native-shaped evidence."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from self_evolving_harness.bundles import snapshot_bundle, verify_bundle
from self_evolving_harness.personal import PersonalHarness


class PersonalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = self.root / "old"
        self.candidate = self.root / "new"
        self.baseline.mkdir()
        (self.baseline / "SKILL.md").write_text("---\nname: repair\ndescription: Check a service.\n---\nUse scripts/check.py.\n")
        (self.baseline / "scripts").mkdir()
        (self.baseline / "scripts/check.py").write_text("print('wrong')\n")
        shutil.copytree(self.baseline, self.candidate)
        (self.candidate / "scripts/check.py").write_text("print('healthy')\n")
        self.harness = PersonalHarness(self.root / "state")
        self.old = self.harness.seed(self.baseline, "repair")["sha256"]
        self.identifier = self.harness.propose(self.candidate, "repair", ["session:prior:42"])
        self.new = self.harness.ledger.get(self.identifier)["spec"]["artifact_sha256"]
        self.evaluator = self.root / "independent_evaluator.py"
        self.evaluator.write_text('''import hashlib,json,pathlib,subprocess,sys
old,new,out=map(pathlib.Path,sys.argv[1:])
def side(root):
 p=subprocess.run([sys.executable,str(root/'scripts/check.py')],capture_output=True,text=True,check=True)
 return {'valid_trace':True,'passed':p.stdout.strip()=='healthy','tool_stdout':p.stdout}
identity=lambda root:hashlib.sha256((root.parent/'manifest.json').read_bytes()).hexdigest()
pathlib.Path(out).write_text(json.dumps({'protocol':'paired-tool-behavior-v1','baseline_sha256':identity(old),'candidate_sha256':identity(new),'cases':[{'baseline':side(old),'candidate':side(new)}]}))
''')

    def evaluate(self):
        return self.harness.evaluate(self.identifier, [sys.executable, str(self.evaluator), "{baseline}", "{candidate}", "{receipt}"])

    def evidence(self, task, *, content=None):
        with sqlite3.connect(task / "home/state.db") as db:
            db.execute("CREATE TABLE messages (id INTEGER,session_id TEXT,role TEXT,tool_name TEXT,content TEXT)")
            result = {"success": True, "name": "repair", "content": content or (self.candidate / "SKILL.md").read_text()}
            db.execute("INSERT INTO messages VALUES (9,'actual-native-session','tool','skill_view',?)", (json.dumps(result),))

    def test_complete_bundle_changes_on_script_only_and_rejects_mutated_assets(self):
        self.assertNotEqual(self.old, self.new)
        self.assertEqual((self.baseline / "SKILL.md").read_bytes(), (self.candidate / "SKILL.md").read_bytes())
        bundle = self.harness.bundle(self.new)
        (bundle / "content/scripts/check.py").write_text("print('tampered')")
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.harness.publish(self.identifier)
        self.assertEqual(self.harness.assignment("repair")["sha256"], self.old)

    def test_real_tool_comparison_publish_and_next_task_session_evidence(self):
        result = self.evaluate()
        self.assertTrue(result["report"]["passed"])
        self.assertEqual(result["report"]["cases"][0]["candidate"]["tool_stdout"], "healthy\n")
        self.harness.publish(self.identifier)
        task = self.harness.prepare_task("repair")
        self.assertEqual((task / "home/skills/repair/scripts/check.py").read_bytes(), (self.candidate / "scripts/check.py").read_bytes())
        self.evidence(task)
        receipt = self.harness.verify_adoption(task)
        self.assertEqual(receipt["session_id"], "actual-native-session")
        self.assertEqual(self.harness.ledger.get(self.identifier)["status"], "adopted")

    def test_learning_changes_do_not_change_formal_or_fresh_task(self):
        source = self.harness.learner_home / "skills/repair/scripts/check.py"
        source.write_text("print('unapproved')")
        task = self.harness.prepare_task("repair")
        self.assertEqual((task / "home/skills/repair/scripts/check.py").read_text(), "print('wrong')\n")
        self.assertEqual(self.harness.assignment("repair")["sha256"], self.old)

    def test_failed_case_stays_rejected_and_cannot_publish(self):
        self.evaluator.write_text(self.evaluator.read_text().replace("=='healthy'", "=='impossible'"))
        self.assertFalse(self.evaluate()["report"]["passed"])
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.harness.publish(self.identifier)
        self.assertEqual(self.harness.assignment("repair")["sha256"], self.old)

    def test_interrupted_assignment_can_reconcile_without_repeating_publication(self):
        self.evaluate()
        original = self.harness.ledger.published
        with patch.object(self.harness.ledger, "published", side_effect=SystemExit("crash after assignment")):
            with self.assertRaises(SystemExit):
                self.harness.publish(self.identifier)
        reopened = PersonalHarness(self.harness.root)
        self.assertEqual(reopened.reconcile(self.identifier), "published")
        self.assertEqual(reopened.assignment("repair")["sha256"], self.new)

    def test_announcement_and_wrong_skill_read_do_not_prove_adoption(self):
        self.evaluate()
        self.harness.publish(self.identifier)
        task = self.harness.prepare_task("repair")
        self.evidence(task, content="I loaded the approved Skill")
        with self.assertRaisesRegex(ValueError, "No matching native"):
            self.harness.verify_adoption(task)
        self.assertEqual(self.harness.ledger.get(self.identifier)["status"], "published")

    def test_candidate_symlink_and_task_mutation_are_rejected(self):
        (self.candidate / "escape").symlink_to(self.evaluator)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            snapshot_bundle(self.harness.store, self.candidate, "repair")
        task = self.harness.prepare_task("repair")
        self.evidence(task)
        (task / "home/skills/repair/scripts/check.py").write_text("new unapproved method")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.harness.verify_adoption(task)

    def test_cli_from_outside_checkout_needs_no_pythonpath(self):
        runner = ROOT / "deploy/personal.py"
        result = subprocess.run([sys.executable, str(runner), "govern", "--state", str(self.root / "cli"),
                                 "seed", "--name", "repair", "--source", str(self.baseline)],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["sha256"])

    def test_rollback_changes_future_tasks_and_reconciles_interruption(self):
        self.evaluate()
        self.harness.publish(self.identifier)
        current = self.harness.prepare_task("repair")
        with patch.object(self.harness.ledger, "recovered", side_effect=SystemExit("rollback interrupted")):
            with self.assertRaises(SystemExit):
                self.harness.rollback(self.identifier)
        reopened = PersonalHarness(self.harness.root)
        self.assertEqual(reopened.reconcile(self.identifier), "rolled_back")
        fresh = reopened.prepare_task("repair")
        self.assertEqual((current / "home/skills/repair/scripts/check.py").read_text(), "print('healthy')\n")
        self.assertEqual((fresh / "home/skills/repair/scripts/check.py").read_text(), "print('wrong')\n")

    def test_candidate_cannot_supply_the_final_evaluator(self):
        candidate_script = self.harness.bundle(self.new) / "content/scripts/check.py"
        with self.assertRaisesRegex(ValueError, "Evaluator code must be outside"):
            self.harness.evaluate(self.identifier, [sys.executable, str(candidate_script),
                "{baseline}", "{candidate}", "{receipt}"])
        self.assertEqual(self.harness.ledger.get(self.identifier)["status"], "proposed")


if __name__ == "__main__":
    unittest.main()
