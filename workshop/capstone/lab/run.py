"""Run the workshop implementation against an existing HTTP environment."""

import argparse
from pathlib import Path
import sys


LAB = Path(__file__).resolve().parent
CAPSTONE = LAB.parent
sys.path.insert(0, str(CAPSTONE))
sys.path.insert(0, str(CAPSTONE / "patrol"))

from implementation import load_implementation
import patrol
from stages import read_patrol_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Patrol records and Hermes Home")
    parser.add_argument("--environment-state", type=Path, help="Existing HTTP environment, defaults to --state")
    parser.add_argument("--implementation", type=Path, default=LAB / "self_improvement_agent.py")
    parser.add_argument("--solution", action="store_true", help="Run the reference implementation")
    parser.add_argument("--stage", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--live", action="store_true", help="Use the configured model endpoint")
    parser.add_argument("--rounds", type=int, help="Stop after a bounded verification run")
    args = parser.parse_args()
    source = LAB / "solutions/self_improvement_agent.py" if args.solution else args.implementation
    implementation = load_implementation(source)
    if hasattr(implementation, "patrol_config"):
        config = read_patrol_config(implementation)
    else:
        config = {"services": ["payments"], "sample": 1, "interval": 3}
    command = ["--state", str(args.state), "run", "--interval", str(config["interval"]),
               "--sample", str(config["sample"])]
    for service in config["services"]:
        command.extend(("--service", service))
    if args.environment_state:
        command.extend(("--environment-state", str(args.environment_state)))
    if not args.live:
        command.append("--offline")
    if args.stage >= 2:
        command.extend(("--oncall", "--page-oncall"))
    if args.rounds is not None:
        command.extend(("--rounds", str(args.rounds)))
    patrol.main(command, oncall_steps=implementation)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Patrol stopped", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        sys.exit(str(exc))
