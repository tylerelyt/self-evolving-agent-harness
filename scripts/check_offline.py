"""Run the course's dependency-free checks in independent Python processes."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "Shared system core": ("-m", "unittest", "discover", "-s", "tests/core", "-p", "test_*.py"),
    "Installer": ("scripts/test_setup_deps.py",),
    # The lesson's full suite also tests notification shapes against the installed SDK.
    "Steer evidence": ("examples/02-instruction-control/test_control_validation.py", "EvidenceTests", "LifecycleTests"),
    "Skill disclosure": ("examples/08-progressive-disclosure/test_check_report.py",),
    "Background review": ("examples/11-background-review/test_offline.py",),
    "Memory revision": ("workshop/oncall/test_acceptance.py",),
    "Curator adoption": ("examples/17-curator/test_risks.py",),
    "Business grading": ("examples/18-business-eval/alert_selftest.py",),
    "Enterprise dataset": ("examples/19-enterprise-dataset/selftest.py",),
    "Lesson 23 candidate handoff": ("examples/23-oncall-agent/test_run23.py",),
    "Lesson 23 stepwise boundaries": ("examples/23-oncall-agent/test_steps.py",),
    "Workbench controls": ("-m", "unittest", "discover", "-s", "workshop/capstone", "-p", "test_workbench*.py"),
    "Capstone evidence": ("-m", "unittest", "discover", "-s", "workshop/capstone/patrol", "-p", "test_*.py"),
}


def main():
    failed = []
    for name, args in SUITES.items():
        print(f"\n== {name} ==", flush=True)
        try:
            result = subprocess.run([sys.executable, "-E", "-S", "-B", *args], cwd=ROOT, timeout=60)
        except subprocess.TimeoutExpired:
            print(f"{name} exceeded 60 seconds", file=sys.stderr, flush=True)
            failed.append(name)
        else:
            if result.returncode:
                failed.append(name)

    print(f"\n{len(SUITES) - len(failed)}/{len(SUITES)} offline suites passed", flush=True)
    if failed:
        print("Failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
