"""Run the native On-call rehearsal and print its actual results."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def print_results(output):
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    if report.get("mode") != "local-script-model" or report.get("closed_loop_verified") is not True:
        raise ValueError("The scripted rehearsal has not verified the full closed loop; inspect report.json")
    first, second = report["rounds"]
    cases = first["evaluation"]["cases"]
    baseline = sum(row["baseline"]["passed"] is True for row in cases)
    candidate = sum(row["candidate"]["passed"] is True for row in cases)
    proof = json.loads((output / second["patrol_evidence"]).read_text(encoding="utf-8"))
    print("\n--- verified results ---", flush=True)
    print(f"paired replay: baseline {baseline}/{len(cases)} -> candidate {candidate}/{len(cases)}")
    print(f"round 1: {first['decision']}; round 2 adopted: {second['adoption_verified']}")
    for instance, record in sorted(proof.items()):
        print(f"{instance}: {[probe['http_status'] for probe in record['probes']]}")
    print(f"rollback: {report['rollback_rehearsal']['status']}")
    print(f"closed_loop_verified: {report['closed_loop_verified']}")
    print(f"evidence: {output / 'report.json'}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="A new output directory; default is a unique runtime/demo-* directory")
    args = parser.parse_args(argv)
    output = (args.out or ROOT / "workshop/capstone/runtime" / ("demo-" + uuid4().hex[:12])).resolve()
    if output.exists() or output.is_symlink():
        parser.error("Choose a new output directory; existing runs are preserved")
    print("Hermes + SkillClaw + local HTTP; scripted model; remote model API calls: 0", flush=True)
    print("Two fault rounds, paired evaluation, adoption and rollback. Recovery windows take a few minutes.", flush=True)
    result = subprocess.run([
        sys.executable, "-B", str(ROOT / "workshop/capstone/self_improvement_agent.py"),
        "--out", str(output), "--verify-closed-loop", "--rehearse-rollback",
    ], cwd=ROOT)
    if result.returncode:
        detail = f"Preserved evidence: {output}" if output.exists() else "Output was not created; inspect the error above."
        print(f"Rehearsal did not pass. {detail}", file=sys.stderr)
        return result.returncode
    print_results(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
