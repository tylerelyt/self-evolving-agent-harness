"""Own the HTTP environments, patrol resources and evidence of an On-call run."""

from contextlib import ExitStack
from pathlib import Path
import sys
from uuid import uuid4


PATROL = Path(__file__).resolve().parent / "patrol"
if str(PATROL) not in sys.path:
    sys.path.insert(0, str(PATROL))

from environment import ALERT_SOURCE, classify, get_snapshot, load_json, write_json
from export_workset import build_workset
from fleet import (
    FAULTS, PATROL_SEED, SEED, TARGETS,
    capture_distractors, collect_patrols, finish_patrols, handoff_facts,
    investigate_round, newer_checkpoint, process, recover, review_facts,
    revise_diagnostic_memory, run_rollback_rehearsal, seal_distractors,
    set_faults, sha256, start_enterprise_faults, start_patrols,
    verify_adoption, wait_for_metrics, wait_for_patrols,
)
from fleet_eval import evaluate, release_allowed
from hermes_runtime import ONCALL_SKILL, ensure_loopback_no_proxy, fork
from investigation import page_operator
from patrol import steer_oncall
from recovery_skill_flow import adopt_recovery_skill, prepare_recovery_skill
from skillclaw_evolve import evolve, release


class Runtime:
    """Own lab resources while the selected implementation runs the workflow."""

    def __init__(self, root, options, live, *, gepa=False, enterprise=False,
                 langfuse=False, openviking=False, rehearse_rollback=False,
                 implementation_file=None):
        if langfuse and not enterprise:
            raise ValueError("Langfuse capture requires enterprise incident snapshots")
        self.root = Path(root).resolve()
        self.options = options
        self.live = live
        self.gepa = gepa
        self.enterprise = enterprise
        self.langfuse = langfuse
        self.openviking = openviking
        self.rehearse_rollback = rehearse_rollback
        self.implementation_file = implementation_file
        self.faults = FAULTS
        self.environments = {alias: self.root / "environments" / alias for alias in TARGETS}
        self.receivers = {alias: self.root / "receivers" / alias for alias in TARGETS}
        self.baseline = SEED
        self.previous = None
        self.prior_facts = None
        self.recovery_skill = None
        self.recovered = None
        self.formal = self.root / "formal"
        self.shared = self.root / "shared-facts.sqlite3"
        self.viking = {"namespace": "capstone-" + uuid4().hex, "runbooks": {}} if openviking else None
        self.report = {
            "mode": "live" if live else "local-script-model",
            "status": "running",
            "rounds": [],
            "alert_source": ALERT_SOURCE,
            "patrol_skill_sha256": sha256(PATROL_SEED),
        }
        if self.viking is not None:
            self.report["openviking"] = self.viking
        if langfuse:
            self.report["langfuse"] = {
                "plugin": "observability/langfuse",
                "scope": "patrol_and_followup",
                "status": "capture_requested",
                "expected_turns": 2 * len(TARGETS) * len(FAULTS),
            }
        self.report_path = self.root / "report.json"
        self.stack = ExitStack()
        self.workers = {}

    def step(self, message):
        print(message, flush=True)

    def __enter__(self):
        ensure_loopback_no_proxy()
        write_json(self.report_path, self.report)
        try:
            if self.viking is not None:
                from openviking_handoff import publish_runbook

                self.step("Publish the scoped course runbooks to OpenViking")
                for alias, service in TARGETS.items():
                    self.viking["runbooks"][service] = publish_runbook(
                        self.viking["namespace"], service, alias,
                    )
                    write_json(self.report_path, self.report)
            seed_sha = sha256(self.baseline)
            self.report["seed_release"] = release(
                self.formal, self.baseline, seed_sha, self.receivers,
            )
            self.baseline = self.formal / "versions" / seed_sha / ONCALL_SKILL / "SKILL.md"
            for alias, state in self.environments.items():
                state.mkdir(parents=True)
                args = ["serve"]
                if self.enterprise:
                    args.extend(("--trace-service", TARGETS[alias]))
                self.stack.enter_context(process(
                    "environment.py", state, state / "environment.log", *args,
                ))
            self.report["healthy_baseline"] = wait_for_metrics(
                self.environments, lambda _alias, value: value["requests"] >= 25
                and classify(value) == "healthy" and value["error_rate"] == 0,
            )
            self.workers = start_patrols(
                self.root, self.environments, self.stack, self.live,
                scoped=self.enterprise, langfuse=self.langfuse,
                implementation_file=self.implementation_file,
            )
            self.report["patrol_workers"] = {
                alias: child.pid for alias, (_, child) in self.workers.items()
            }
            write_json(self.report_path, self.report)
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def finish(self):
        """Export the completed run and execute any requested extension checks."""
        learning_failed = bool(self.report.get("learning_errors"))
        rounds_complete = len(self.report["rounds"]) == len(self.faults)
        if self.rehearse_rollback:
            if learning_failed:
                self.report["rollback_rehearsal"] = {"status": "pending", "reason": "learning_failed"}
            else:
                self.step("Rehearse rollback and verify the restored Skill in fresh patrols")
                run_rollback_rehearsal(
                    self.root, self.report, self.environments, self.receivers,
                    self.recovered, self.options, self.live, implementation_file=self.implementation_file,
                )
        self.stack.close()
        workset_path = self.root / "http-workset.json"
        write_json(workset_path, build_workset(self.root, {**self.report, "status": "complete"}))
        self.report["http_workset"] = workset_path.name
        if self.enterprise and not rounds_complete:
            self.report["enterprise"] = {"status": "pending", "reason": "incident_rounds_incomplete"}
        elif self.enterprise:
            from enterprise_pack import build_pack
            from enterprise_replay import evaluate as evaluate_enterprise
            from enterprise_cases import run as run_enterprise_cases

            enterprise_root = self.root / "enterprise"
            pack = build_pack(enterprise_root)
            evaluation = enterprise_root / "evaluation"
            self.report["enterprise"] = {
                "pack": "enterprise/prepared/pack.json",
                "evaluation": "enterprise/evaluation/report.json",
                "ready": len(pack["ready"]),
                "pending": len(pack["pending"]),
            }
            write_json(self.report_path, self.report)
            assessment = evaluate_enterprise(evaluation, pack, self.options, self.live)
            self.report["enterprise"]["status"] = assessment["status"]
            write_json(self.report_path, self.report)
            self.step("Build and score the enterprise incident challenge sets")
            self.report["enterprise_cases"] = run_enterprise_cases(self.root, self.options, self.live)
        if self.langfuse:
            self.report["langfuse"]["status"] = "awaiting_remote_verification"
        self.report["status"] = "learning_failed" if learning_failed else "complete"

    def learning_error(self, task, exc):
        """Keep patrols alive after a learning failure and report whether to continue.

        A partially published candidate must first be restored on all receivers.
        Returning False asks the driver to stop injecting further incidents and
        finish the bounded run with its existing evidence.
        """
        error = {"round": task.number, "type": type(exc).__name__,
                 "message": str(exc) or type(exc).__name__}
        task.result["decision"] = "learning_failed"
        task.result["learning_error"] = error
        self.report.setdefault("learning_errors", []).append(error)
        self.report["status"] = "learning_failed"
        restored = self._restore_previous()
        if restored:
            task.result["formal_sha256"] = sha256(self.baseline)
            error["retained_sha256"] = task.result["formal_sha256"]
            self.step(f"Round {task.number}: learning failed, patrols retain the approved Skill")
        else:
            error["rollback_error"] = self.report["rollback_error"]
            self.step(f"Round {task.number}: learning failed, stop adding incidents after incomplete Skill recovery")
        write_json(self.report_path, self.report)
        return restored

    def _restore_previous(self):
        if self.previous is None:
            return True
        prior_path, prior_sha = self.previous
        try:
            rollback = release(self.formal, prior_path, prior_sha, self.receivers, restoring=True)
            self.report["rollback"] = rollback
            if rollback["receiver_errors"] or rollback["receivers"].keys() != self.receivers.keys():
                self.report["rollback_error"] = "Some receivers did not restore the approved Skill"
                return False
        except Exception as rollback_exc:
            self.report["rollback_error"] = str(rollback_exc) or type(rollback_exc).__name__
            return False
        self.baseline = prior_path
        self.previous = None
        return True

    def _record_failure(self, exc):
        self.report.update(status="failed", error=str(exc) or type(exc).__name__)
        self._restore_previous()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.stack.close()
        except BaseException as cleanup_error:
            if exc is None:
                self._record_failure(cleanup_error)
            raise
        finally:
            if exc is not None:
                self._record_failure(exc)
            write_json(self.report_path, self.report)
        return False
