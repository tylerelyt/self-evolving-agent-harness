"""Governed personal Skills: learn privately, evaluate, publish, use afresh.

This is single-user process isolation and provenance, not an OS sandbox. A
trusted external evaluator owns its scorer and checks actual task behavior;
the candidate is never its own evaluator. Use separate service identities for
adversarial or multi-tenant workloads.
"""

import argparse
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from .bundles import safe_name, snapshot_bundle, verify_bundle
from .coordinator import ImprovementCoordinator
from .evaluation import release_allowed
from .learning import LearningLedger, canonical


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + uuid4().hex)
    try:
        temporary.write_text(canonical(value) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class PersonalHarness:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = self.root / "bundles"
        self.learner_home = self.root / "learner"
        (self.learner_home / "skills").mkdir(parents=True, exist_ok=True)
        self.ledger = LearningLedger(self.root / "learning.sqlite3")
        self.coordinator = ImprovementCoordinator(self.ledger)

    @contextmanager
    def owned(self):
        with (self.root / ".personal.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Personal workspace already has an owner") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def bundle(self, identity):
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Invalid bundle identity")
        path = self.store / identity
        verify_bundle(path)
        return path

    def assignment(self, name):
        return json.loads((self.root / "assignments" / (safe_name(name) + ".json")).read_text())

    def seed(self, source, name):
        with self.owned():
            path = snapshot_bundle(self.store, source, name)
            assigned = self.root / "assignments" / (name + ".json")
            if assigned.exists():
                raise ValueError("A formal Skill already exists; submit a candidate")
            write(assigned, {"name": name, "sha256": path.name, "candidate_id": None})
            # Only the learning copy is writable during native self-updates.
            learning = self.learner_home / "skills" / name
            if not learning.exists():
                shutil.copytree(path / "content", learning)
            return {"sha256": path.name, "learner_home": str(self.learner_home)}

    def propose(self, source, name, sources):
        with self.owned():
            baseline = self.assignment(name)
            candidate = snapshot_bundle(self.store, source, name)
            return self.ledger.register(scope=self.ledger.namespace,
                artifact_sha256=candidate.name, parent_sha256=baseline["sha256"],
                sources=sources, generator="personal-native-learning",
                generation={"skill_name": name, "bundle_schema": 1})

    def evaluate(self, identifier, command, *, timeout=300):
        """Operator-supplied argv writes a paired behavioral report to {receipt}.

        Command must explicitly use {baseline}, {candidate}, {receipt}; paths
        identify complete content trees. The report must bind both hashes and
        contain per-case baseline/candidate valid_trace + passed booleans.
        """
        with self.owned():
            if not command or not all(isinstance(x, str) and x for x in command):
                raise ValueError("Evaluator must be an argument array")
            for token in ("{baseline}", "{candidate}", "{receipt}"):
                if not any(token in x for x in command):
                    raise ValueError("Evaluator must explicitly bind " + token)
            # Evaluator code must come from the operator, not a candidate or
            # native learning/task workspace. Placeholder data paths are the
            # only allowed references into those untrusted trees.
            untrusted = (self.store.resolve(), self.learner_home.resolve(), (self.root / "tasks").resolve())
            for argument in command:
                if any(token in argument for token in ("{baseline}", "{candidate}", "{receipt}")):
                    continue
                path = Path(argument).expanduser().resolve()
                if any(path == base or base in path.parents for base in untrusted):
                    raise ValueError("Evaluator code must be outside candidate and learning workspaces")
            spec = self.ledger.get(identifier)["spec"]
            baseline, candidate = self.bundle(spec["parent_sha256"]), self.bundle(spec["artifact_sha256"])
            output = self.root / "evaluations" / uuid4().hex
            output.mkdir(parents=True)
            receipt = output / "report.json"
            mapping = {"{baseline}": str(baseline / "content"),
                       "{candidate}": str(candidate / "content"), "{receipt}": str(receipt)}
            argv = list(command)
            for key, value in mapping.items():
                argv = [x.replace(key, value) for x in argv]
            write(output / "invocation.json", {"command": command, "candidate_id": identifier,
                  "started_at": time.time(), "protocol": "paired-tool-behavior-v1"})

            def evaluator():
                with (output / "evaluator.log").open("w") as log:
                    subprocess.run(argv, check=True, cwd=output, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, timeout=timeout)
                # Both full trees must survive evaluation unchanged.
                verify_bundle(baseline)
                verify_bundle(candidate)
                report = json.loads(receipt.read_text())
                if report.get("protocol") != "paired-tool-behavior-v1":
                    raise ValueError("Evaluator must attest paired tool behavior")
                report["passed"] = release_allowed(report["cases"], spec["parent_sha256"], spec["artifact_sha256"])
                return report

            report, digest = self.coordinator.evaluate(identifier, candidate / "manifest.json", evaluator)
            return {"report": report, "report_sha256": digest, "evidence": str(output)}

    def publish(self, identifier):
        with self.owned():
            row = self.ledger.get(identifier)
            spec = row["spec"]
            candidate = self.bundle(spec["artifact_sha256"])
            name = spec["generation"]["skill_name"]
            if self.assignment(name)["sha256"] != spec["parent_sha256"]:
                raise ValueError("Candidate parent is no longer the active assignment")

            def publisher():
                verify_bundle(candidate)
                write(self.root / "assignments" / (name + ".json"),
                      {"name": name, "sha256": candidate.name, "candidate_id": identifier})
                verify_bundle(candidate)
                return {"sha256": candidate.name, "receivers": {"personal": {"sha256": candidate.name}}}

            return self.coordinator.publish(identifier, candidate / "manifest.json", ["personal"], publisher)

    def reconcile(self, identifier):
        """Resolve assignment-after-intent interruption without replaying publication."""
        with self.owned():
            row = self.ledger.get(identifier)
            rollback_path = self.root / "rollback-intents" / (identifier + ".json")
            if rollback_path.exists() and row["status"] in ("published", "adopted"):
                spec = row["spec"]
                assigned = self.assignment(spec["generation"]["skill_name"])
                self.bundle(assigned["sha256"])
                if assigned["sha256"] != spec["parent_sha256"]:
                    raise RuntimeError("Rollback assignment has not been restored; retry rollback")
                self.ledger.recovered(identifier, assigned["sha256"], {"verified": True,
                    "receivers": {"personal": {"sha256": assigned["sha256"]}}})
                return "rolled_back"
            if row["status"] != "publishing":
                return row["status"]
            spec = row["spec"]
            name = spec["generation"]["skill_name"]
            assigned = self.assignment(name)
            self.bundle(assigned["sha256"])
            if assigned["sha256"] == spec["artifact_sha256"] and assigned["candidate_id"] == identifier:
                self.ledger.published(identifier, {"sha256": assigned["sha256"],
                    "receivers": {"personal": {"sha256": assigned["sha256"]}}})
                return "published"
            if assigned["sha256"] == spec["parent_sha256"]:
                self.ledger.recovered(identifier, assigned["sha256"], {"verified": True,
                    "receivers": {"personal": {"sha256": assigned["sha256"]}}})
                return "rolled_back"
            raise RuntimeError("Unresolved assignment requires operator reconciliation")

    def rollback(self, identifier):
        """Restore future task selection; existing tasks retain their pinned copy."""
        with self.owned():
            row = self.ledger.get(identifier)
            if row["status"] == "rolled_back":
                return {"status": "rolled_back"}
            if row["status"] not in ("published", "adopted"):
                raise ValueError("Only a published candidate can be rolled back")
            spec = row["spec"]
            name = spec["generation"]["skill_name"]
            assigned = self.assignment(name)
            if assigned["sha256"] not in (spec["artifact_sha256"], spec["parent_sha256"]):
                raise ValueError("Another release owns the active assignment")
            self.bundle(spec["parent_sha256"])
            write(self.root / "rollback-intents" / (identifier + ".json"),
                  {"candidate_id": identifier, "parent_sha256": spec["parent_sha256"]})
            write(self.root / "assignments" / (name + ".json"),
                  {"name": name, "sha256": spec["parent_sha256"], "candidate_id": None})
            self.ledger.recovered(identifier, spec["parent_sha256"], {"verified": True,
                "receivers": {"personal": {"sha256": spec["parent_sha256"]}}})
            return {"status": "rolled_back", "sha256": spec["parent_sha256"]}

    def prepare_task(self, name):
        with self.owned():
            assigned = self.assignment(name)
            source = self.bundle(assigned["sha256"])
            task = self.root / "tasks" / uuid4().hex
            (task / "home").mkdir(parents=True, mode=0o700)
            shutil.copytree(source / "content", task / "home/skills" / name)
            write(task / "assignment.json", {**assigned, "created_at": time.time()})
            return task

    def verify_adoption(self, task):
        """Read actual task SessionDB Skill tool output, never a model summary."""
        task = Path(task).resolve()
        if task.parent != self.root / "tasks":
            raise ValueError("Task must belong to this workspace")
        assigned = json.loads((task / "assignment.json").read_text())
        source = self.bundle(assigned["sha256"])
        from .bundles import describe
        expected = json.loads((source / "manifest.json").read_text())
        if describe(task / "home/skills" / assigned["name"], assigned["name"]) != expected:
            raise ValueError("Task modified its assigned Skill; cannot claim adoption")
        db_path = task / "home/state.db"
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT id,session_id,content FROM messages WHERE role='tool' AND tool_name='skill_view' ORDER BY id").fetchall()
        expected_skill = (source / "content/SKILL.md").read_text(encoding="utf-8")
        found = None
        for item, session, content in rows:
            try:
                result = json.loads(content)
            except (ValueError, TypeError):
                continue
            if (result.get("success") is True and result.get("name") == assigned["name"]
                    and result.get("content") == expected_skill):
                found = {"skill_sha256": assigned["sha256"], "verified": True,
                         "session_id": session, "report_item_id": item, "session_db": str(db_path)}
                break
        if found is None:
            raise ValueError("No matching native Skill read in task SessionDB")
        identifier = assigned.get("candidate_id")
        if identifier and self.ledger.get(identifier)["status"] == "published":
            self.ledger.adopted(identifier, {"personal": found})
        write(task / "adoption.json", found)
        return found

    def run_task(self, name, prompt, *, python=sys.executable, timeout=600, toolsets="skills,terminal"):
        task = self.prepare_task(name)
        # Setup/model config stays in the private learner profile; each task gets
        # a fresh home, independent native Skill writes and its own SessionDB.
        for filename in ("config.yaml", ".env"):
            source = self.learner_home / filename
            if source.is_file():
                shutil.copy2(source, task / "home" / filename)
                (task / "home" / filename).chmod(0o600)
        env = {**os.environ, "HERMES_HOME": str(task / "home")}
        query = f"Read the {safe_name(name)} Skill with skill_view before working.\n{prompt}"
        command = [str(python), "-m", "hermes_cli.main", "--toolsets", toolsets,
                   "--usage-file", str(task / "usage.json"), "--oneshot", query]
        with (task / "runtime.log").open("w") as log:
            result = subprocess.run(command, cwd=task, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        write(task / "execution.json", {"exit_code": result.returncode, "completed_at": time.time()})
        if result.returncode:
            raise RuntimeError("Hermes task failed; inspect private task runtime.log")
        result = {"task": str(task), "adoption": self.verify_adoption(task)}
        if (task / "usage.json").exists():
            result["usage"] = json.loads((task / "usage.json").read_text())
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Govern personal Hermes Skill learning")
    parser.add_argument("--state", required=True, type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("seed", "propose"):
        child = sub.add_parser(action)
        child.add_argument("--name", required=True)
        child.add_argument("--source", required=True, type=Path)
        if action == "propose":
            child.add_argument("--source-session", action="append", required=True)
    child = sub.add_parser("evaluate")
    child.add_argument("candidate")
    child.add_argument("--evaluator", required=True, type=Path, help="Operator-owned JSON argv file")
    for action in ("publish", "reconcile", "rollback"):
        child = sub.add_parser(action)
        child.add_argument("candidate")
    child = sub.add_parser("task")
    child.add_argument("--name", required=True)
    child.add_argument("--prompt", required=True)
    child.add_argument("--python", default=sys.executable)
    child.add_argument("--toolsets", default="skills,terminal")
    args = parser.parse_args(argv)
    harness = PersonalHarness(args.state)
    if args.action == "seed":
        result = harness.seed(args.source, args.name)
    elif args.action == "propose":
        result = {"candidate_id": harness.propose(args.source, args.name, args.source_session)}
    elif args.action == "evaluate":
        result = harness.evaluate(args.candidate, json.loads(args.evaluator.read_text()))
    elif args.action == "task":
        result = harness.run_task(args.name, args.prompt, python=args.python, toolsets=args.toolsets)
    else:
        result = getattr(harness, args.action)(args.candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
